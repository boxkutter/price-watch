package app.pricewatch.shared.api

import app.pricewatch.shared.session.Session
import io.ktor.client.HttpClient
import io.ktor.client.engine.HttpClientEngine
import io.ktor.client.plugins.HttpTimeout
import io.ktor.client.plugins.contentnegotiation.ContentNegotiation
import io.ktor.client.request.header
import io.ktor.client.request.request
import io.ktor.client.request.setBody
import io.ktor.client.request.url
import io.ktor.client.statement.HttpResponse
import io.ktor.client.statement.bodyAsText
import io.ktor.http.ContentType
import io.ktor.http.HttpMethod
import io.ktor.http.contentType
import io.ktor.serialization.kotlinx.json.json
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.decodeFromJsonElement
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject
import kotlin.time.TimeSource
import kotlin.uuid.Uuid

/** The service answered with an error (its one envelope), or could not be reached at all. */
class ApiException(
    val status: Int,
    val code: String?,
    override val message: String,
    val retryAfterSeconds: Int? = null,
    val unreachable: Boolean = false,
    val details: JsonElement? = null,
) : Exception(message) {
    val unauthorized get() = status == 401
    val notFound get() = status == 404
    val conflict get() = status == 409
    val rateLimited get() = status == 429
}

val ApiJson = Json {
    ignoreUnknownKeys = true
    isLenient = true
    coerceInputValues = true
    explicitNulls = false
}

/**
 * The Price Watch REST API (docs/api.md): every call sends the user's token and the app's own
 * client token. Anything that contacts a store is asynchronous, so those calls return a job to
 * [awaitJob]. Plain Kotlin on Ktor, so the same code serves Android and, later, iOS.
 */
class PriceWatchApi(
    engine: HttpClientEngine? = null,
    /** The signed-in session, read on every call so signing in or out takes effect at once. */
    private val session: () -> Session? = { null },
    /** Called when the server refuses the token (401) of the current session. */
    private val onUnauthorized: () -> Unit = {},
) {
    private val http = (if (engine != null) HttpClient(engine) { configure() } else HttpClient { configure() })

    private fun io.ktor.client.HttpClientConfig<*>.configure() {
        install(ContentNegotiation) { json(ApiJson) }
        install(HttpTimeout) {
            requestTimeoutMillis = 30_000
            connectTimeoutMillis = 10_000
        }
        expectSuccess = false
    }

    // ---- plumbing ------------------------------------------------------------------------------

    private suspend fun send(
        method: HttpMethod,
        baseUrl: String,
        path: String,
        userToken: String?,
        clientToken: String?,
        body: JsonObject? = null,
        query: Map<String, String?> = emptyMap(),
        idempotent: Boolean = false,
    ): HttpResponse {
        val response = try {
            http.request {
                url(baseUrl.trimEnd('/') + path)
                query.forEach { (k, v) -> if (v != null) url.parameters.append(k, v) }
                this.method = method
                userToken?.let { header("Authorization", "Bearer $it") }
                clientToken?.let { header("X-Client-Token", it) }
                if (idempotent) header("Idempotency-Key", Uuid.random().toString())
                if (body != null) {
                    contentType(ContentType.Application.Json)
                    setBody(body)
                }
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            throw ApiException(0, null, "Could not reach the server. Check the address and your connection.", unreachable = true)
        }
        if (response.status.value >= 400) throw errorOf(response, signedIn = userToken != null)
        return response
    }

    private suspend fun errorOf(response: HttpResponse, signedIn: Boolean): ApiException {
        val text = runCatching { response.bodyAsText() }.getOrDefault("")
        val body = runCatching { ApiJson.decodeFromString(ErrorBody.serializer(), text) }.getOrNull()
        val status = response.status.value
        if (status == 401 && signedIn) onUnauthorized()
        return ApiException(
            status = status,
            code = body?.code,
            message = body?.message ?: fallbackMessage(status),
            retryAfterSeconds = response.headers["Retry-After"]?.toIntOrNull(),
            details = body?.details,
        )
    }

    private fun fallbackMessage(status: Int) = when (status) {
        401 -> "Please sign in again."
        403 -> "This app is not allowed to do that."
        404 -> "Not found."
        429 -> "Too many requests. Try again in a moment."
        in 500..599 -> "The server had a problem. Try again later."
        else -> "Something went wrong (HTTP $status)."
    }

    private fun requireSession(): Session =
        session() ?: throw ApiException(401, "unauthorized", "Please sign in.")

    private suspend fun call(
        method: HttpMethod,
        path: String,
        body: JsonObject? = null,
        query: Map<String, String?> = emptyMap(),
        idempotent: Boolean = false,
    ): HttpResponse {
        val s = requireSession()
        return send(method, s.baseUrl, path, s.userToken, s.clientToken, body, query, idempotent)
    }

    private suspend inline fun <reified T> get(path: String, query: Map<String, String?> = emptyMap()): T =
        ApiJson.decodeFromString(call(HttpMethod.Get, path, query = query).bodyAsText())

    private suspend inline fun <reified T> post(
        path: String,
        body: JsonObject? = null,
        idempotent: Boolean = false,
    ): T = ApiJson.decodeFromString(call(HttpMethod.Post, path, body, idempotent = idempotent).bodyAsText())

    // ---- signing in (no session yet) -------------------------------------------------------------

    /** Signs in with email and password; returns the user token. */
    suspend fun login(baseUrl: String, email: String, password: String): LoginResponse =
        ApiJson.decodeFromString(
            send(
                HttpMethod.Post, baseUrl, "/v1/auth/login", null, null,
                buildJsonObject { put("email", email); put("password", password) },
            ).bodyAsText(),
        )

    /** Accepts an invitation (creating the account); the person then signs in with the same password. */
    suspend fun acceptInvitation(baseUrl: String, token: String, password: String): String {
        val text = send(
            HttpMethod.Post, baseUrl, "/v1/auth/accept-invitation", null, null,
            buildJsonObject { put("token", token); put("password", password) },
        ).bodyAsText()
        return ApiJson.parseToJsonElement(text).let { (it as JsonObject)["email"]?.toString()?.trim('"') ?: "" }
    }

    /**
     * Signs this app in as the person: the server makes the app's own client credential and grants
     * it the app scopes in one call, so the person needs nothing but their email and password.
     */
    suspend fun createAppCredential(baseUrl: String, userToken: String, name: String): AppCredential =
        ApiJson.decodeFromString(
            send(HttpMethod.Post, baseUrl, "/v1/me/app-credentials", userToken, null, buildJsonObject { put("name", name) }).bodyAsText(),
        )

    /** Signs an app out: revokes a credential made by [createAppCredential]. Best effort. */
    suspend fun revokeAppCredential(baseUrl: String, userToken: String, clientId: String) {
        try {
            send(HttpMethod.Delete, baseUrl, "/v1/me/app-credentials/$clientId", userToken, null)
        } catch (e: ApiException) {
            if (!e.notFound && !e.unauthorized) throw e
        }
    }

    /** Checks both tokens work together; used right after signing in. */
    suspend fun whoAmI(baseUrl: String, userToken: String, clientToken: String): Me =
        ApiJson.decodeFromString(
            send(HttpMethod.Get, baseUrl, "/v1/me", userToken, clientToken).bodyAsText(),
        )

    /** Signs the person out of the server: revokes this app's credential and the user token. */
    suspend fun logout() {
        session()?.let { revokeAppCredential(it.baseUrl, it.userToken, it.clientId) }
        runCatching { call(HttpMethod.Post, "/v1/auth/logout") }
    }

    // ---- updates --------------------------------------------------------------------------------

    /** The newest app the server offers, or null when none is published. Needs no sign-in. */
    suspend fun latestRelease(baseUrl: String): ReleaseInfo? = try {
        ApiJson.decodeFromString(send(HttpMethod.Get, baseUrl, "/download/latest.json", null, null).bodyAsText())
    } catch (e: ApiException) {
        if (e.notFound) null else throw e
    }

    // ---- stores ---------------------------------------------------------------------------------

    @kotlinx.serialization.Serializable
    private data class Items<T>(val items: List<T> = emptyList())

    suspend fun stores(): List<Store> = get<Items<Store>>("/v1/stores").items
    suspend fun myStores(): List<MyStore> = get<Items<MyStore>>("/v1/me/stores").items
    suspend fun connectors(): List<Connector> = get<Items<Connector>>("/v1/connectors").items
    /**
     * Asks a retailer for the stores near a place (a postcode, say) and gives back their ids; the
     * stores then also show in [stores]. One small request goes to the retailer, a few a person per hour.
     */
    suspend fun findStores(retailerId: String, inputs: Map<String, String>): List<String> {
        val accepted: JobAccepted = post(
            "/v1/retailers/$retailerId/stores/discover",
            buildJsonObject { inputs.forEach { (k, v) -> put(k, v) } },
        )
        val job = awaitJob(accepted.jobId)
        if (!job.succeeded) {
            // a mistyped postcode comes back as the retailer's own words, which say what to fix
            val detail = (job.result as? JsonObject)?.get("detail")?.toString()?.trim('"')
            throw ApiException(0, job.state, detail?.takeIf { it.isNotBlank() } ?: describeJobFailure(job.state, null))
        }
        return ApiJson.decodeFromJsonElement<FoundStores>(job.result ?: JsonObject(emptyMap())).storeIds
    }

    /** The chains the person is a member of (retailer ids). */
    suspend fun memberships(): Set<String> =
        get<Items<RetailerRef>>("/v1/me/memberships").items.map { it.retailerId }.toSet()

    @kotlinx.serialization.Serializable
    private data class RetailerRef(@kotlinx.serialization.SerialName("retailer_id") val retailerId: String)

    /** Says whether the person is a member of a chain (ICA, City Gross); member prices then count for them. */
    suspend fun setMember(retailerId: String, member: Boolean) {
        call(if (member) HttpMethod.Put else HttpMethod.Delete, "/v1/me/memberships/$retailerId")
    }

    suspend fun addMyStore(storeId: String) { call(HttpMethod.Put, "/v1/me/stores/$storeId") }
    suspend fun removeMyStore(storeId: String) { call(HttpMethod.Delete, "/v1/me/stores/$storeId") }

    // ---- jobs -----------------------------------------------------------------------------------

    suspend fun job(jobId: String): Job = get("/v1/jobs/$jobId")

    /**
     * Polls until the job stops (1 to 2 seconds apart, as the guide asks) and gives it back.
     * Only `state == "succeeded"` means the whole request was answered.
     */
    suspend fun awaitJob(
        jobId: String,
        timeoutSeconds: Int = 75,
        onProgress: (JobProgress) -> Unit = {},
    ): Job {
        val started = TimeSource.Monotonic.markNow()
        var wait = 800L
        while (true) {
            val job = job(jobId)
            job.progress?.let(onProgress)
            if (!job.running) return job
            if (started.elapsedNow().inWholeSeconds > timeoutSeconds) {
                throw ApiException(0, "timeout", "The stores are taking too long to answer. Try again in a moment.")
            }
            delay(wait)
            wait = 1500L
        }
    }

    // ---- looking things up ----------------------------------------------------------------------

    /** Searches the stores by name (or by barcode when [gtin] is given). At most 5 stores per job. */
    suspend fun search(
        query: String? = null,
        gtin: String? = null,
        storeIds: List<String>,
        fresh: Boolean = false,
        onProgress: (JobProgress) -> Unit = {},
    ): SearchResult {
        val accepted: JobAccepted = post(
            "/v1/search/jobs",
            buildJsonObject {
                if (gtin != null) put("gtin", gtin) else put("query", query ?: "")
                put("store_ids", JsonArray(storeIds.map { JsonPrimitive(it) }))
                put("channel", "online")
                if (fresh) put("fresh", true)
            },
            idempotent = true,
        )
        return finished(awaitJob(accepted.jobId, onProgress = onProgress))
    }

    /** Turns a stopped job into its result, or an error saying why it did not succeed. */
    private inline fun <reified T> finished(job: Job): T {
        val result = job.result
        if (!job.succeeded && job.state != "partial") {
            val detail = (result as? JsonObject)?.get("detail")?.toString()?.trim('"')
            throw ApiException(0, job.state, describeJobFailure(job.state, detail))
        }
        return ApiJson.decodeFromJsonElement(result ?: JsonObject(emptyMap()))
    }

    // ---- watching -------------------------------------------------------------------------------

    suspend fun monitors(): List<Monitor> = get<MonitorList>("/v1/monitors").items

    suspend fun watch(storeId: String, listingId: String, name: String, maximumPrice: String?): Monitor =
        post(
            "/v1/monitors",
            buildJsonObject {
                put("name", name)
                put("type", "exact_product")
                put("store_id", storeId)
                putJsonObject("conditions") {
                    put("listing_id", listingId)
                    if (maximumPrice != null) put("maximum_price", maximumPrice)
                }
                put("interval_minutes", 60)
            },
            idempotent = true,
        )

    /** Adds a product from its page link; returns the monitor once the store has answered. */
    suspend fun watchLink(url: String, storeId: String?, maximumPrice: String?): Monitor {
        val answer: FromUrlAnswer = post(
            "/v1/monitors/from-url",
            buildJsonObject {
                put("url", url)
                if (storeId != null) put("store_id", storeId)
                if (maximumPrice != null) put("maximum_price", maximumPrice)
            },
            idempotent = true,
        )
        if (answer.alreadyMonitored && answer.monitor != null) return answer.monitor
        val job = awaitJob(answer.jobId ?: throw ApiException(0, null, "The server gave no job to follow."))
        val result = job.result as? JsonObject
        val monitorId = result?.get("monitor_id")?.toString()?.trim('"')
        if (!job.succeeded || monitorId == null) {
            val detail = result?.get("detail")?.toString()?.trim('"')
            throw ApiException(0, job.state, describeJobFailure(job.state, detail))
        }
        return monitors().firstOrNull { it.id == monitorId }
            ?: throw ApiException(404, "not_found", "The product was added but could not be loaded.")
    }

    /** Sets the alert price ("17.50"), or removes it when [maximumPrice] is null. Other fields stay as they are. */
    suspend fun setAlertPrice(monitorId: String, maximumPrice: String?): Monitor {
        val body = buildJsonObject { if (maximumPrice == null) put("maximum_price", JsonNull) else put("maximum_price", maximumPrice) }
        return ApiJson.decodeFromString(call(HttpMethod.Patch, "/v1/monitors/$monitorId", body).bodyAsText())
    }

    suspend fun removeMonitor(id: String) { call(HttpMethod.Delete, "/v1/monitors/$id") }

    /** Asks the store about one watched product now (once a minute at most). */
    suspend fun checkNow(monitorId: String) {
        val accepted: JobAccepted = post("/v1/monitors/$monitorId/check", idempotent = true)
        awaitJob(accepted.jobId)
    }

    /**
     * Links a scanned barcode to a product. Throws [BarcodeBelongsToAnotherProduct] when the barcode is
     * already another product's: ask the person, and if they are the same item call again with merge.
     */
    suspend fun linkBarcode(productId: String, gtin: String, merge: Boolean = false) {
        try {
            call(HttpMethod.Post, "/v1/products/$productId/barcode", buildJsonObject {
                put("gtin", gtin)
                if (merge) put("merge", true)
            })
        } catch (e: ApiException) {
            val existing = if (e.conflict) existingProductOf(e) else null
            if (existing != null) throw BarcodeBelongsToAnotherProduct(existing, e)
            throw e
        }
    }

    /** Takes back a barcode a person linked (never one the store published). */
    suspend fun unlinkBarcode(productId: String) { call(HttpMethod.Delete, "/v1/products/$productId/barcode") }

    private fun existingProductOf(e: ApiException): ExistingProduct? = runCatching {
        val first = (e.details as? JsonArray)?.firstOrNull() as? JsonObject ?: return null
        ApiJson.decodeFromJsonElement<ExistingProduct>(first["existing_product"] as JsonObject)
    }.getOrNull()

    suspend fun priceHistory(listingId: String): List<PricePoint> =
        get<PriceHistory>("/v1/listings/$listingId/prices").items

    suspend fun monitorEvents(monitorId: String): List<ApiEvent> =
        get<EventPage>("/v1/monitors/$monitorId/events").items

    /** Events oldest first, after [after]; `nextCursor` is set only when another page exists. */
    suspend fun events(after: String? = null, limit: Int = 100): EventPage =
        get("/v1/events", mapOf("after" to after, "limit" to limit.toString()))

    // ---- comparing ------------------------------------------------------------------------------

    /** What the catalogue already knows about a barcode in the person's stores, at once (no store is asked). */
    suspend fun knownPrices(gtin: String, hereStoreId: String?): CompareAnswer =
        post(
            "/v1/compare/barcode",
            buildJsonObject {
                put("gtin", gtin)
                if (hereStoreId != null) put("here_store_id", hereStoreId)
                put("refresh", false)
            },
        )

    /** Checks the barcode in each store live. */
    suspend fun compareLive(
        gtin: String,
        hereStoreId: String?,
        onProgress: (JobProgress) -> Unit = {},
    ): CompareResult {
        val answer: CompareAnswer = post(
            "/v1/compare/barcode",
            buildJsonObject {
                put("gtin", gtin)
                if (hereStoreId != null) put("here_store_id", hereStoreId)
            },
            idempotent = true,
        )
        val jobId = answer.jobId ?: return CompareResult(
            product = answer.product, rows = answer.known, cheapest = answer.cheapest, savingVsHere = answer.savingVsHere,
        )
        return finished(awaitJob(jobId, onProgress = onProgress))
    }

    /** An image is served by the app's own server without a login; the link is relative to it. */
    fun imageUrl(ref: ImageRef?): String? = ref?.let { absolute(it.url) }

    fun absolute(path: String?): String? {
        if (path == null) return null
        if (path.startsWith("http")) return path
        return session()?.baseUrl?.trimEnd('/')?.plus(path)
    }
}

/** The barcode belongs to another product: a question for the person, not an error to retry. */
class BarcodeBelongsToAnotherProduct(val existing: ExistingProduct, cause: ApiException) :
    Exception("That barcode already belongs to \"${existing.name}\".", cause)

/** Plain words for a job that stopped without succeeding (the statuses of docs/api.md, "Asynchronous jobs"). */
fun describeJobFailure(state: String, detail: String?): String = when (state) {
    "not_found" -> "The store does not list that product."
    "paused", "disabled" -> "That store is switched off for now. Its connector is under the administrator's control."
    "rate_limited" -> "The store asked us to slow down. Try again later."
    "access_blocked" -> "The store refused the request."
    "unsupported" -> "That store cannot do this."
    "auth_required" -> "That store needs a setting that is missing on the server."
    "extraction_failed", "invalid_data" -> "The store's answer could not be read."
    "rejected" -> detail ?: "The request was rejected."
    else -> detail ?: "The store could not be reached ($state)."
}

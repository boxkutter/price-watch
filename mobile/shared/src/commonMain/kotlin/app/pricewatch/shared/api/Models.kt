package app.pricewatch.shared.api

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

// The wire format of the Price Watch API (docs/api.md). Money is a decimal string and unknown is
// null, never zero: the types keep both, and `format/Money.kt` turns them into text for a person.

@Serializable
data class ErrorBody(
    val code: String? = null,
    val message: String? = null,
    val details: JsonElement? = null,
    @SerialName("request_id") val requestId: String? = null,
)

@Serializable
data class LoginResponse(
    @SerialName("access_token") val accessToken: String,
    @SerialName("expires_in") val expiresIn: Long = 0,
    @SerialName("user_id") val userId: String? = null,
)

@Serializable
data class Me(
    val id: String,
    val email: String,
    @SerialName("is_admin") val isAdmin: Boolean = false,
    @SerialName("client_id") val clientId: String? = null,
    val scopes: List<String> = emptyList(),
)

@Serializable
data class Store(
    val id: String,
    val name: String,
    @SerialName("retailer_id") val retailerId: String,
    val country: String? = null,
)

/** A store in "my stores": the ones prices are compared across. */
@Serializable
data class MyStore(
    @SerialName("store_id") val storeId: String,
    val name: String,
    @SerialName("retailer_id") val retailerId: String,
    /** Whether the person is a member of this store's chain (membership is per chain, not per shop). */
    val member: Boolean = false,
)

/** One thing a retailer needs to find its stores (a postcode, or a town). */
@Serializable
data class DiscoveryInput(
    val name: String,
    val label: String = "",
    val required: Boolean = false,
    val pattern: String? = null,
)

@Serializable
data class Connector(
    val id: String,
    val name: String,
    val enabled: Boolean = true,
    val status: String? = null,
    @SerialName("logo_url") val logoUrl: String? = null,
    val capabilities: Map<String, String> = emptyMap(),
    @SerialName("store_discovery_inputs") val storeDiscoveryInputs: List<DiscoveryInput> = emptyList(),
) {
    /** Whether this retailer can look its stores up for a person. */
    val canFindStores: Boolean get() = enabled && capabilities["store_discovery"] == "supported"
}

/** The app's own sign-in, made by the server when a person logs in from the app. */
@Serializable
data class AppCredential(
    @SerialName("client_id") val clientId: String,
    @SerialName("client_token") val clientToken: String,
)

@Serializable
data class FoundStores(
    val status: String? = null,
    @SerialName("store_ids") val storeIds: List<String> = emptyList(),
)

@Serializable
data class ExistingProduct(val id: String, val name: String = "")

@Serializable
data class UnitPrice(val amount: String, val unit: String)

@Serializable
data class Barcode(val gtin14: String, val source: String? = null)

@Serializable
data class ImageRef(val url: String, val source: String? = null, val credit: String? = null)

@Serializable
data class Offer(
    val terms: String? = null,
    val title: String? = null,
    val price: String? = null,
    val eligibility: String? = null,
    @SerialName("valid_to") val validTo: String? = null,
    /** For a multi-buy ("2 för 35 kr"): how many, and what one costs when you buy them (17.50). */
    val quantity: Int? = null,
    @SerialName("unit_price") val unitPrice: String? = null,
)

@Serializable
data class SearchItem(
    @SerialName("product_id") val productId: String,
    @SerialName("listing_id") val listingId: String,
    @SerialName("store_id") val storeId: String,
    val name: String,
    @SerialName("display_name") val displayName: String? = null,
    val brand: String? = null,
    val size: String? = null,
    val pack: String? = null,
    @SerialName("current_price") val currentPrice: String? = null,
    val currency: String = "SEK",
    @SerialName("like_key") val likeKey: String? = null,
    val barcode: Barcode? = null,
    val image: ImageRef? = null,
    val url: String? = null,
    val availability: String? = null,
    val offers: List<Offer> = emptyList(),
    /** The best price per item for this person at this store, when it beats the shelf price. */
    val best: BestOffer? = null,
) {
    /** What one item costs this person: the best deal, or the shelf price. */
    val effectivePrice: String? get() = best?.unitPrice ?: currentPrice
}

@Serializable
data class Coverage(
    @SerialName("store_id") val storeId: String,
    val status: String,
    val detail: String? = null,
    val truncated: Boolean = false,
    @SerialName("next_skip") val nextSkip: Int? = null,
)

@Serializable
data class SearchResult(
    val status: String? = null,
    val complete: Boolean = true,
    val items: List<SearchItem> = emptyList(),
    val coverage: List<Coverage> = emptyList(),
)

@Serializable
data class JobAccepted(
    @SerialName("job_id") val jobId: String,
    val status: String? = null,
)

@Serializable
data class JobProgress(val text: String? = null, val done: Int? = null, val total: Int? = null)

@Serializable
data class Job(
    val id: String,
    val state: String,
    val result: JsonElement? = null,
    val progress: JobProgress? = null,
) {
    /** Still working: the job has not stopped, so keep polling. */
    val running: Boolean get() = state == "queued" || state == "running" || state == "retry_wait"
    val succeeded: Boolean get() = state == "succeeded"
}

@Serializable
data class Latest(
    @SerialName("current_price") val currentPrice: String? = null,
    @SerialName("previous_price") val previousPrice: String? = null,
    val currency: String = "SEK",
    @SerialName("unit_price") val unitPrice: UnitPrice? = null,
    val availability: String? = null,
    @SerialName("observed_at") val observedAt: String? = null,
)

/**
 * The cheapest way the person can buy an item: a multi-buy, a percentage off, a member price. [unitPrice]
 * is what one costs that way, [quantity] how many to buy to get it (1 for a member price).
 */
@Serializable
data class BestOffer(
    val terms: String? = null,
    val quantity: Int = 1,
    @SerialName("unit_price") val unitPrice: String,
    val member: Boolean = false,
    @SerialName("valid_to") val validTo: String? = null,
)

@Serializable
data class Conditions(
    @SerialName("listing_id") val listingId: String? = null,
    @SerialName("maximum_price") val maximumPrice: String? = null,
    val query: String? = null,
)

@Serializable
data class Monitor(
    val id: String,
    val name: String,
    val type: String = "exact_product",
    @SerialName("store_id") val storeId: String? = null,
    @SerialName("store_name") val storeName: String? = null,
    @SerialName("retailer_id") val retailerId: String? = null,
    @SerialName("product_id") val productId: String? = null,
    @SerialName("product_name") val productName: String? = null,
    val image: ImageRef? = null,
    val barcode: Barcode? = null,
    val latest: Latest? = null,
    val offer: BestOffer? = null,
    val conditions: Conditions = Conditions(),
    @SerialName("interval_minutes") val intervalMinutes: Int = 60,
    @SerialName("last_success_at") val lastSuccessAt: String? = null,
    val enabled: Boolean = true,
    @SerialName("removed_at") val removedAt: String? = null,
)

@Serializable
data class MonitorList(val items: List<Monitor> = emptyList())

/** Adding by link answers at once when the product is already watched, else with a job. */
@Serializable
data class FromUrlAnswer(
    @SerialName("job_id") val jobId: String? = null,
    @SerialName("already_monitored") val alreadyMonitored: Boolean = false,
    val monitor: Monitor? = null,
)

@Serializable
data class PricePoint(
    @SerialName("current_price") val currentPrice: String? = null,
    @SerialName("regular_price") val regularPrice: String? = null,
    val currency: String = "SEK",
    @SerialName("unit_price") val unitPrice: UnitPrice? = null,
    val availability: String? = null,
    @SerialName("observed_at") val observedAt: String,
    @SerialName("source_url") val sourceUrl: String? = null,
)

@Serializable
data class PriceHistory(val items: List<PricePoint> = emptyList())

@Serializable
data class ApiEvent(
    val id: String,
    val type: String,
    @SerialName("monitor_id") val monitorId: String? = null,
    val payload: JsonObject = JsonObject(emptyMap()),
    @SerialName("created_at") val createdAt: String = "",
)

@Serializable
data class EventPage(
    val items: List<ApiEvent> = emptyList(),
    @SerialName("next_cursor") val nextCursor: String? = null,
)

@Serializable
data class VsCheapest(val amount: String? = null, val percent: String? = null)

@Serializable
data class CompareRow(
    @SerialName("store_id") val storeId: String,
    @SerialName("store_name") val storeName: String,
    @SerialName("retailer_id") val retailerId: String? = null,
    val status: String,
    val detail: String? = null,
    @SerialName("product_name") val productName: String? = null,
    @SerialName("current_price") val currentPrice: String? = null,
    @SerialName("regular_price") val regularPrice: String? = null,
    val currency: String = "SEK",
    /** The best price per item for this person (what stores are ranked by), and how it comes about. */
    @SerialName("effective_price") val effectivePrice: String? = null,
    val best: BestOffer? = null,
    @SerialName("unit_price") val unitPrice: UnitPrice? = null,
    val availability: String? = null,
    @SerialName("age_seconds") val ageSeconds: Long? = null,
    val source: String? = null,
    @SerialName("source_url") val sourceUrl: String? = null,
    @SerialName("is_cheapest") val isCheapest: Boolean = false,
    @SerialName("vs_cheapest") val vsCheapest: VsCheapest? = null,
)

@Serializable
data class CompareProduct(
    val id: String,
    val name: String,
    val brand: String? = null,
    val barcode: Barcode? = null,
)

@Serializable
data class CheapestRef(
    @SerialName("store_id") val storeId: String? = null,
    @SerialName("store_name") val storeName: String? = null,
    @SerialName("current_price") val currentPrice: String? = null,
    @SerialName("effective_price") val effectivePrice: String? = null,
)

@Serializable
data class Saving(val amount: String? = null, val percent: String? = null)

/** What the catalogue already knows (`known`), with a job that then checks each store live. */
@Serializable
data class CompareAnswer(
    @SerialName("job_id") val jobId: String? = null,
    val gtin14: String,
    @SerialName("here_store_id") val hereStoreId: String? = null,
    val product: CompareProduct? = null,
    val known: List<CompareRow> = emptyList(),
    val cheapest: CheapestRef? = null,
    @SerialName("saving_vs_here") val savingVsHere: Saving? = null,
)

/** The finished comparison job's result: the same rows, now checked live. */
@Serializable
data class CompareResult(
    val status: String? = null,
    val complete: Boolean = true,
    val product: CompareProduct? = null,
    val rows: List<CompareRow> = emptyList(),
    val cheapest: CheapestRef? = null,
    @SerialName("saving_vs_here") val savingVsHere: Saving? = null,
)

/** The newest app the server offers at /download (`latest.json`). */
@Serializable
data class ReleaseInfo(
    @SerialName("version_code") val versionCode: Int,
    @SerialName("version_name") val versionName: String,
    val notes: String = "",
    @SerialName("size_bytes") val sizeBytes: Long = 0,
    val sha256: String = "",
    val url: String = "/download/pricewatch.apk",
)

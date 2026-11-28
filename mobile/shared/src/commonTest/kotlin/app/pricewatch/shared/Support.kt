package app.pricewatch.shared

import app.pricewatch.shared.session.MemoryKeyValueStore
import app.pricewatch.shared.session.Session
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.MockRequestHandleScope
import io.ktor.client.engine.mock.respond
import io.ktor.client.request.HttpRequestData
import io.ktor.client.request.HttpResponseData
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf

val signedIn = Session("http://server:8085", "user-token", "client-token", "client-id", "me@example.org")

/** A fake server: [routes] maps "METHOD /path" to a JSON body (or a status and body). Every request is recorded. */
class FakeServer(private val routes: MutableMap<String, Pair<Int, String>> = mutableMapOf()) {
    val requests = mutableListOf<HttpRequestData>()
    private val bodies = mutableListOf<String>()

    /** The JSON bodies of the requests that had one, in order. */
    fun sentBodies(): List<String> = bodies.toList()

    fun on(route: String, body: String, status: Int = 200) { routes[route] = status to body }

    private val dynamic = mutableMapOf<String, (HttpRequestData) -> String>()

    /** A route whose answer depends on the request (for example a feed that honours `after`). */
    fun onRequest(route: String, answer: (HttpRequestData) -> String) { dynamic[route] = answer }

    val engine = MockEngine { request ->
        requests += request
        (request.body as? io.ktor.http.content.TextContent)?.let { bodies += it.text }
        val key = "${request.method.value} ${request.url.encodedPath}"
        dynamic[key]?.let { return@MockEngine json(200, it(request)) }
        val (status, body) = routes[key] ?: (404 to """{"code":"not_found","message":"No route $key"}""")
        json(status, body)
    }

    private fun MockRequestHandleScope.json(status: Int, body: String): HttpResponseData =
        respond(body, HttpStatusCode.fromValue(status), headersOf(HttpHeaders.ContentType, "application/json"))
}

fun graphWith(server: FakeServer, signedInSession: Boolean = true): AppGraph {
    val graph = AppGraph(MemoryKeyValueStore(), server.engine)
    if (signedInSession) graph.sessions.signIn(signedIn)
    return graph
}

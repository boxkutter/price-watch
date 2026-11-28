package app.pricewatch.shared.notify

import app.pricewatch.shared.api.ApiEvent
import app.pricewatch.shared.api.ApiException
import app.pricewatch.shared.api.Monitor
import app.pricewatch.shared.api.PriceWatchApi
import app.pricewatch.shared.format.formatMoney
import app.pricewatch.shared.format.shortDate
import app.pricewatch.shared.session.NotificationPrefs
import app.pricewatch.shared.session.SessionRepository
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

enum class NoticeKind { PriceDrop, TargetReached, PriceIncrease, Offer, Restocked, Unavailable, NewProduct }

/** One thing worth telling a person: shown in the activity list and, when it is new, as a notification. */
data class Notice(
    val eventId: String,
    val monitorId: String?,
    val kind: NoticeKind,
    val title: String,
    val text: String,
    val createdAt: String,
    /** The target was reached by a multi-buy ("2 för 35 kr"), not by the price of one item. */
    val viaOffer: Boolean = false,
)

private fun JsonObject.str(key: String): String? = (this[key] as? JsonPrimitive)?.content?.takeIf { it != "null" }

/** Turns the API's events into plain sentences. Events of a kind the app does not know are left out. */
object EventDescriber {
    fun describe(event: ApiEvent, monitor: Monitor?): Notice? {
        val p = event.payload
        val currency = monitor?.latest?.currency ?: "SEK"
        val title = monitor?.name ?: monitor?.productName ?: "A watched product"
        val where = monitor?.storeName?.let { " at $it" } ?: ""
        fun money(key: String) = p.str(key)?.let { formatMoney(it, currency) }
        val (kind, text) = when (event.type) {
            "price.decreased" -> NoticeKind.PriceDrop to
                "Now ${money("new") ?: "cheaper"}$where" + (money("old")?.let { ", was $it" } ?: "")
            "price.increased" -> NoticeKind.PriceIncrease to
                "Now ${money("new") ?: "higher"}$where" + (money("old")?.let { ", was $it" } ?: "")
            "price.threshold_reached" -> NoticeKind.TargetReached to run {
                val target = money("threshold")?.let { " (target $it)" } ?: ""
                val terms = p.str("terms")?.let { " with $it" } ?: ""
                when {
                    p.str("member") == "true" -> "Reached your target with your member price: ${money("price") ?: "now"}$terms$where$target"
                    p.str("multibuy") == "true" -> "Reached your target: ${money("price") ?: "now"} each$terms$where$target"
                    p.str("terms") != null -> "Reached your target: ${money("price") ?: "now"}$terms$where$target"
                    else -> "Reached your target: ${money("price") ?: "now"}$where$target"
                }
            }
            "offer.detected", "offer.changed" -> NoticeKind.Offer to run {
                val terms = p.str("terms") ?: p.str("title") ?: "A new offer"
                val until = p.str("valid_to")?.let { " until ${shortDate(it)}" } ?: ""
                val member = if (p.str("eligibility") == "member") " (members)" else ""
                "$terms$member$where$until"
            }
            "product.restocked" -> NoticeKind.Restocked to "Back in stock$where"
            "product.unavailable" -> NoticeKind.Unavailable to "No longer available$where"
            "product.discovered" -> NoticeKind.NewProduct to "${p.str("name") ?: "A new product"} matches your watch$where"
            else -> return null
        }
        return Notice(event.id, event.monitorId, kind, title, text, event.createdAt, viaOffer = p.str("terms") != null && event.type == "price.threshold_reached")
    }

    /** Whether the person asked to hear about this kind of event. */
    fun wanted(kind: NoticeKind, prefs: NotificationPrefs): Boolean = when (kind) {
        NoticeKind.PriceDrop, NoticeKind.TargetReached -> prefs.priceDrops
        NoticeKind.PriceIncrease -> prefs.priceIncreases
        NoticeKind.Offer, NoticeKind.NewProduct -> prefs.offers
        NoticeKind.Restocked, NoticeKind.Unavailable -> prefs.stock
    }

    /** When a check both lowers the price and reaches the target, say it once: the target is the news. */
    fun withoutDuplicates(notices: List<Notice>): List<Notice> {
        val reached = notices.filter { it.kind == NoticeKind.TargetReached }.mapNotNull { it.monitorId }.toSet()
        // a multi-buy that reached the target says more than the plain "new offer" notice for the same product
        val reachedByOffer = notices.filter { it.kind == NoticeKind.TargetReached && it.viaOffer }.mapNotNull { it.monitorId }.toSet()
        return notices.filterNot {
            (it.kind == NoticeKind.PriceDrop && it.monitorId in reached) ||
                (it.kind == NoticeKind.Offer && it.monitorId in reachedByOffer)
        }
    }
}

/**
 * Looks for events since the last look, and keeps its place in the feed. The first look after
 * signing in only notes where the feed stands, so old events never arrive as a flood.
 */
class EventNotifier(private val api: PriceWatchApi, private val sessions: SessionRepository) {

    // The background worker and the app's own first look can run together (right after signing in); they
    // share one place in the feed, so they take turns, or the second would announce what the first skipped.
    private val turn = Mutex()

    /** New notices the person wants to hear about, oldest first. Advances the saved place. */
    suspend fun poll(): List<Notice> = turn.withLock { look() }

    private suspend fun look(): List<Notice> {
        if (sessions.current == null) return emptyList()
        val events = try {
            fetchNewEvents()
        } catch (e: ApiException) {
            if (e.notFound && sessions.eventCursor != null) {
                // The saved place is gone (events were deleted): start again from the end, quietly.
                sessions.eventCursor = null
                sessions.baselined = false
                fetchNewEvents()
            } else throw e
        }
        if (events == null) return emptyList()
        if (events.isEmpty()) return emptyList()

        val monitors = api.monitors().associateBy { it.id }
        val prefs = sessions.prefs.value
        val notices = events.mapNotNull { EventDescriber.describe(it, monitors[it.monitorId]) }
        return EventDescriber.withoutDuplicates(notices).filter { EventDescriber.wanted(it.kind, prefs) }
    }

    /** The events after the saved place, or null when this was only the first look. */
    private suspend fun fetchNewEvents(): List<ApiEvent>? {
        val found = mutableListOf<ApiEvent>()
        var cursor = sessions.eventCursor
        repeat(MAX_PAGES) {
            val page = api.events(after = cursor, limit = PAGE_SIZE)
            found += page.items
            page.items.lastOrNull()?.let {
                cursor = it.id
                sessions.eventCursor = it.id
            }
            if (page.nextCursor == null) return finish(found)
        }
        return finish(found)
    }

    private fun finish(found: List<ApiEvent>): List<ApiEvent>? {
        if (!sessions.baselined) {
            sessions.baselined = true
            return null
        }
        return found
    }

    private companion object {
        const val PAGE_SIZE = 200
        const val MAX_PAGES = 25
    }
}

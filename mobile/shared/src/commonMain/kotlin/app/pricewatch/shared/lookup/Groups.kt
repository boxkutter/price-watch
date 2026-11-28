package app.pricewatch.shared.lookup

import app.pricewatch.shared.api.ImageRef
import app.pricewatch.shared.api.SearchItem
import app.pricewatch.shared.format.toAmount

/** The same product in several stores, with the cheapest first. A product with no known twin stands alone. */
data class ProductGroup(
    val key: String,
    val title: String,
    val subtitle: String,
    val image: ImageRef?,
    /** One entry per store, cheapest first; stores with no price come last. */
    val offers: List<SearchItem>,
) {
    val cheapest: SearchItem get() = offers.first()
    val storeCount: Int get() = offers.map { it.storeId }.distinct().size

    /** How much the dearest store costs over the cheapest one, when more than one store has a price. */
    val spread: Double?
        get() {
            val prices = offers.mapNotNull { it.currentPrice.toAmount() }
            return if (prices.size > 1) prices.max() - prices.min() else null
        }
}

/**
 * Lines up search results from several stores: items with the same `like_key` are the same item
 * (a barcode, or a strong match of brand, size and name), so they share one card.
 */
fun groupResults(items: List<SearchItem>): List<ProductGroup> {
    val order = LinkedHashMap<String, MutableList<SearchItem>>()
    for (item in items) {
        order.getOrPut(item.likeKey ?: "listing:${item.listingId}") { mutableListOf() } += item
    }
    return order.map { (key, group) ->
        val sorted = group
            .distinctBy { it.storeId to it.listingId }
            .sortedWith(compareBy({ it.currentPrice.toAmount() == null }, { it.currentPrice.toAmount() ?: 0.0 }))
        val lead = sorted.firstOrNull { it.displayName != null } ?: sorted.first()
        ProductGroup(
            key = key,
            title = lead.displayName ?: lead.name,
            subtitle = listOfNotNull(lead.brand, lead.size, lead.pack).joinToString(" · "),
            image = sorted.firstNotNullOfOrNull { it.image },
            offers = sorted,
        )
    }
}

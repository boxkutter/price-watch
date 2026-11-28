package app.pricewatch.shared.format

import kotlin.math.abs
import kotlin.math.roundToLong
import kotlin.time.Clock
import kotlin.time.Instant

/** Prices arrive as decimal strings; this is the only place they become numbers, and only to show or compare. */
fun String?.toAmount(): Double? = this?.trim()?.toDoubleOrNull()

private fun twoDecimals(value: Double): String {
    val cents = (abs(value) * 100).roundToLong()
    val sign = if (value < 0 && cents != 0L) "-" else ""
    return "$sign${cents / 100},${(cents % 100).toString().padStart(2, '0')}"
}

/** "19,95 kr" for kronor, "12,50 EUR" otherwise. Unknown is a dash, never 0. */
fun formatMoney(amount: String?, currency: String = "SEK"): String {
    val v = amount.toAmount() ?: return "–"
    return formatMoney(v, currency)
}

fun formatMoney(amount: Double, currency: String = "SEK"): String =
    if (currency == "SEK") "${twoDecimals(amount)} kr" else "${twoDecimals(amount)} $currency"

/** "13,30 kr/kg". */
fun formatUnitPrice(amount: String?, unit: String?, currency: String = "SEK"): String? {
    if (amount.toAmount() == null || unit.isNullOrBlank()) return null
    return "${formatMoney(amount, currency)}/$unit"
}

/** The change between two prices as a signed amount: negative is cheaper. Null when either is unknown. */
fun priceChange(now: String?, before: String?): Double? {
    val a = now.toAmount() ?: return null
    val b = before.toAmount() ?: return null
    val d = a - b
    return if (abs(d) < 0.005) 0.0 else d
}

/** "5 minutes ago", "2 hours ago", "yesterday", or the date. */
fun relativeTime(iso: String?, now: Instant = Clock.System.now()): String {
    val then = iso?.let { runCatching { Instant.parse(it) }.getOrNull() } ?: return ""
    val seconds = (now - then).inWholeSeconds
    return when {
        seconds < 45 -> "just now"
        seconds < 90 -> "1 minute ago"
        seconds < 3_600 -> "${seconds / 60} minutes ago"
        seconds < 5_400 -> "1 hour ago"
        seconds < 86_400 -> "${seconds / 3_600} hours ago"
        seconds < 172_800 -> "yesterday"
        seconds < 14 * 86_400 -> "${seconds / 86_400} days ago"
        else -> iso!!.take(10)
    }
}

/** "29 Nov" style date for a short label, from an ISO timestamp. */
fun shortDate(iso: String?): String {
    val date = iso?.take(10)?.split("-") ?: return ""
    if (date.size != 3) return ""
    val months = listOf("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    val m = date[1].toIntOrNull()?.let { months.getOrNull(it - 1) } ?: return ""
    return "${date[2].trimStart('0').ifEmpty { "0" }} $m"
}

/**
 * Cleans what a person typed or scanned into a barcode, or null when it is not one. Barcodes are 8,
 * 12, 13 or 14 digits (EAN-8, UPC-A, EAN-13, GTIN-14).
 */
fun asBarcode(text: String): String? {
    val digits = text.filter { it.isDigit() }
    if (digits.length != text.trim().length) return null
    return digits.takeIf { it.length in setOf(8, 12, 13, 14) }
}

package app.pricewatch.shared.notify

import app.pricewatch.shared.session.NotificationPrefs
import kotlinx.datetime.TimeZone
import kotlinx.datetime.toLocalDateTime
import kotlin.time.Clock
import kotlin.time.Instant

/**
 * Quiet hours: while they last the phone does not even ask the server for new events, so nothing is
 * lost: the first look after they end announces what happened overnight.
 */
object QuietHours {
    /** Whether [minuteOfDay] (0 to 1439) falls in the quiet period; a period like 22:00 to 07:00 runs past midnight. */
    fun contains(start: Int, end: Int, minuteOfDay: Int): Boolean = when {
        start == end -> false
        start < end -> minuteOfDay in start until end
        else -> minuteOfDay >= start || minuteOfDay < end
    }

    fun isQuiet(prefs: NotificationPrefs, now: Instant = Clock.System.now(), zone: TimeZone = TimeZone.currentSystemDefault()): Boolean {
        if (!prefs.quietEnabled) return false
        val t = now.toLocalDateTime(zone)
        return contains(prefs.quietStart, prefs.quietEnd, t.hour * 60 + t.minute)
    }

    /** "22:30" from minutes after midnight. */
    fun label(minuteOfDay: Int): String = "${(minuteOfDay / 60).toString().padStart(2, '0')}:${(minuteOfDay % 60).toString().padStart(2, '0')}"
}

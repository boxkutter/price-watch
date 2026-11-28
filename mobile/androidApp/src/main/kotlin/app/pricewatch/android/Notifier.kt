package app.pricewatch.android

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import app.pricewatch.shared.notify.Notice
import app.pricewatch.shared.session.AppUpdate

/** Shows what the background check found as notifications, grouped so a busy day is one tidy stack. */
object Notifier {
    const val CHANNEL_ALERTS = "price_alerts"
    const val CHANNEL_APP = "app_updates"
    const val EXTRA_OPEN = "open"
    const val OPEN_ACTIVITY = "activity"
    const val OPEN_SETTINGS = "settings"
    private const val GROUP = "price_watch_alerts"
    private const val SUMMARY_ID = 1
    private const val UPDATE_ID = 2
    private const val EXPIRED_ID = 3

    fun createChannels(context: Context) {
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ALERTS, context.getString(R.string.channel_alerts), NotificationManager.IMPORTANCE_HIGH)
                .apply { description = context.getString(R.string.channel_alerts_description) },
        )
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_APP, context.getString(R.string.channel_app), NotificationManager.IMPORTANCE_LOW)
                .apply { description = context.getString(R.string.channel_app_description) },
        )
    }

    fun canNotify(context: Context): Boolean =
        (Build.VERSION.SDK_INT < 33 || ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) &&
            NotificationManagerCompat.from(context).areNotificationsEnabled()

    private fun open(context: Context, what: String, code: Int): PendingIntent = PendingIntent.getActivity(
        context, code,
        Intent(context, MainActivity::class.java).putExtra(EXTRA_OPEN, what).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
    )

    private fun post(context: Context, id: Int, notification: android.app.Notification) {
        if (!canNotify(context)) return
        try {
            NotificationManagerCompat.from(context).notify(id, notification)
        } catch (_: SecurityException) {
            // the permission was withdrawn between the check and the post
        }
    }

    fun showNotices(context: Context, notices: List<Notice>) {
        if (notices.isEmpty()) return
        val pending = open(context, OPEN_ACTIVITY, 10)
        notices.forEach { n ->
            post(
                context, n.eventId.hashCode(),
                NotificationCompat.Builder(context, CHANNEL_ALERTS)
                    .setSmallIcon(R.drawable.ic_stat_price)
                    .setContentTitle(n.title).setContentText(n.text)
                    .setStyle(NotificationCompat.BigTextStyle().bigText(n.text))
                    .setGroup(GROUP).setAutoCancel(true).setContentIntent(pending)
                    .setCategory(NotificationCompat.CATEGORY_RECOMMENDATION)
                    .build(),
            )
        }
        if (notices.size > 1) {
            val inbox = NotificationCompat.InboxStyle().setBigContentTitle("${notices.size} price alerts")
            notices.take(6).forEach { inbox.addLine("${it.title}: ${it.text}") }
            post(
                context, SUMMARY_ID,
                NotificationCompat.Builder(context, CHANNEL_ALERTS)
                    .setSmallIcon(R.drawable.ic_stat_price)
                    .setContentTitle("${notices.size} price alerts").setContentText(notices.first().title)
                    .setStyle(inbox).setGroup(GROUP).setGroupSummary(true).setAutoCancel(true).setContentIntent(pending)
                    .build(),
            )
        }
    }

    fun showUpdate(context: Context, update: AppUpdate) = post(
        context, UPDATE_ID,
        NotificationCompat.Builder(context, CHANNEL_APP)
            .setSmallIcon(R.drawable.ic_stat_price)
            .setContentTitle("Price Watch ${update.release.versionName} is available")
            .setContentText("Tap to open Settings and install it.")
            .setAutoCancel(true).setContentIntent(open(context, OPEN_SETTINGS, 11))
            .build(),
    )

    fun showSignInExpired(context: Context) = post(
        context, EXPIRED_ID,
        NotificationCompat.Builder(context, CHANNEL_APP)
            .setSmallIcon(R.drawable.ic_stat_price)
            .setContentTitle("Sign in to Price Watch again")
            .setContentText("Your sign-in has expired, so price alerts have stopped.")
            .setAutoCancel(true).setContentIntent(open(context, OPEN_SETTINGS, 12))
            .build(),
    )
}

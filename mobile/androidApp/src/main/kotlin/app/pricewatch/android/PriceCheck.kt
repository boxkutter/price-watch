package app.pricewatch.android

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import app.pricewatch.shared.AppGraph
import app.pricewatch.shared.api.ApiException
import app.pricewatch.shared.notify.QuietHours
import app.pricewatch.shared.session.NotificationPrefs
import java.util.concurrent.TimeUnit

/** One look at the server: new alerts become notifications, and a new app version is announced once. */
object PriceCheck {
    /** Returns how many alerts were found. Throws [ApiException] when the server cannot be reached or refuses. */
    suspend fun run(context: Context, graph: AppGraph, announceUpdates: Boolean, respectQuietHours: Boolean = true): Int {
        val sessions = graph.sessions
        if (sessions.current == null) return 0
        // Quiet hours: do not even look, so nothing is lost; the first look afterwards announces it all.
        if (respectQuietHours && QuietHours.isQuiet(sessions.prefs.value)) return 0
        val notices = try {
            graph.notifier.poll()
        } catch (e: ApiException) {
            if (e.unauthorized && !sessions.expiryAnnounced) {
                sessions.expiryAnnounced = true
                Notifier.showSignInExpired(context)
            }
            throw e
        }
        if (sessions.prefs.value.enabled) Notifier.showNotices(context, notices)
        if (announceUpdates) {
            graph.updates.check(BuildConfig.VERSION_CODE)?.let { update ->
                if (update.release.versionCode > sessions.announcedUpdate) {
                    sessions.announcedUpdate = update.release.versionCode
                    Notifier.showUpdate(context, update)
                }
            }
        }
        return notices.size
    }

    private const val WORK_NAME = "price-check"

    /** Runs the check in the background as often as the person chose (Android's minimum is 15 minutes). */
    fun schedule(context: Context, prefs: NotificationPrefs, signedIn: Boolean) {
        val work = WorkManager.getInstance(context)
        if (!signedIn || !prefs.enabled) {
            work.cancelUniqueWork(WORK_NAME)
            return
        }
        val request = PeriodicWorkRequestBuilder<PriceCheckWorker>(maxOf(15, prefs.intervalMinutes).toLong(), TimeUnit.MINUTES)
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 5, TimeUnit.MINUTES)
            .build()
        work.enqueueUniquePeriodicWork(WORK_NAME, ExistingPeriodicWorkPolicy.UPDATE, request)
    }
}

class PriceCheckWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val graph = (applicationContext as PriceWatchApplication).graph
        return try {
            PriceCheck.run(applicationContext, graph, announceUpdates = true)
            Result.success()
        } catch (e: ApiException) {
            // A refused sign-in will not fix itself by retrying; an unreachable server might.
            if (e.unauthorized) Result.success() else Result.retry()
        }
    }
}

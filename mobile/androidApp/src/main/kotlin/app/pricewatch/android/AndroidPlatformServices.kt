package app.pricewatch.android

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import app.pricewatch.shared.AppGraph
import app.pricewatch.shared.api.ApiException
import app.pricewatch.shared.session.AppUpdate
import app.pricewatch.shared.session.NotificationPrefs
import app.pricewatch.ui.InstallState
import app.pricewatch.ui.PlatformServices
import androidx.core.content.FileProvider
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

/** Android's side of what the shared app cannot do itself. */
class AndroidPlatformServices(
    private val context: Context,
    private val graph: AppGraph,
    private val scope: CoroutineScope,
) : PlatformServices {

    override val appVersionName: String = BuildConfig.VERSION_NAME.removeSuffix("-debug")
    override val appVersionCode: Int = BuildConfig.VERSION_CODE
    override val deviceName: String = "Android (${android.os.Build.MODEL})"

    // ---- notifications ------------------------------------------------------------------------

    /** Set by the activity: shows the system's permission prompt. */
    var permissionRequester: (() -> Unit)? = null
    private var permissionCallback: ((Boolean) -> Unit)? = null

    override fun notificationsAllowed() = Notifier.canNotify(context)

    override fun requestNotificationPermission(onResult: (Boolean) -> Unit) {
        if (notificationsAllowed()) { onResult(true); return }
        val requester = permissionRequester
        if (requester == null || android.os.Build.VERSION.SDK_INT < 33) { onResult(false); return }
        permissionCallback = onResult
        requester()
    }

    fun permissionResult(granted: Boolean) {
        permissionCallback?.invoke(granted)
        permissionCallback = null
        if (granted) applyNotificationSchedule(graph.sessions.prefs.value, graph.sessions.current != null)
    }

    override fun openNotificationSettings() {
        context.startActivity(
            Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        )
    }

    override fun applyNotificationSchedule(prefs: NotificationPrefs, signedIn: Boolean) = PriceCheck.schedule(context, prefs, signedIn)

    override suspend fun checkForEventsNow(): Int? = try {
        PriceCheck.run(context, graph, announceUpdates = false, respectQuietHours = false)
    } catch (e: ApiException) {
        null
    }

    // ---- links, updates --------------------------------------------------------------------------

    override fun openUrl(url: String) {
        runCatching { context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
    }

    /** Downloads the APK, checks it is the file the server described, and opens the system installer. */
    override fun installUpdate(update: AppUpdate, onState: (InstallState) -> Unit) {
        scope.launch {
            val result = withContext(Dispatchers.IO) { runCatching { download(update, onState) } }
            val file = result.getOrElse { e ->
                onState(InstallState.Failed(e.message ?: "The download failed."))
                return@launch
            }
            if (!context.packageManager.canRequestPackageInstalls()) {
                onState(InstallState.Failed("Allow Price Watch to install apps, then tap Try again."))
                context.startActivity(
                    Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${context.packageName}")).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                )
                return@launch
            }
            val uri = FileProvider.getUriForFile(context, "${context.packageName}.updates", file)
            context.startActivity(
                Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/vnd.android.package-archive")
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK),
            )
            onState(InstallState.Idle)
        }
    }

    private fun download(update: AppUpdate, onState: (InstallState) -> Unit): File {
        val dir = File(context.cacheDir, "updates").apply { mkdirs(); listFiles()?.forEach { it.delete() } }
        val file = File(dir, "pricewatch-${update.release.versionCode}.apk")
        val connection = (URL(update.downloadUrl).openConnection() as HttpURLConnection).apply { connectTimeout = 15_000; readTimeout = 30_000 }
        try {
            if (connection.responseCode != 200) error("The server answered ${connection.responseCode} for the download.")
            val total = connection.contentLengthLong.takeIf { it > 0 } ?: update.release.sizeBytes
            val digest = MessageDigest.getInstance("SHA-256")
            var done = 0L
            var lastPercent = -1
            connection.inputStream.use { input ->
                file.outputStream().use { output ->
                    val buffer = ByteArray(64 * 1024)
                    while (true) {
                        val n = input.read(buffer)
                        if (n < 0) break
                        output.write(buffer, 0, n); digest.update(buffer, 0, n); done += n
                        val percent = if (total > 0) (done * 100 / total).toInt() else -1
                        if (percent != lastPercent) { lastPercent = percent; onState(InstallState.Downloading(percent.takeIf { it >= 0 })) }
                    }
                }
            }
            val sha = digest.digest().joinToString("") { "%02x".format(it) }
            if (update.release.sha256.isNotEmpty() && !sha.equals(update.release.sha256, ignoreCase = true)) {
                file.delete()
                error("The download is damaged (its checksum does not match). Try again.")
            }
            return file
        } finally {
            connection.disconnect()
        }
    }

    // ---- camera ----------------------------------------------------------------------------------

    @Composable
    override fun BarcodeScanner(modifier: Modifier, onCode: (String) -> Unit) = CameraScanner(modifier, onCode)
}

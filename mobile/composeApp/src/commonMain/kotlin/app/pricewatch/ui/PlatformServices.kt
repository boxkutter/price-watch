package app.pricewatch.ui

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import app.pricewatch.shared.session.AppUpdate
import app.pricewatch.shared.session.NotificationPrefs

/** Where an app update has got to. */
sealed interface InstallState {
    data object Idle : InstallState
    data class Downloading(val percent: Int?) : InstallState
    data class Failed(val message: String) : InstallState
}

/** What only the phone can do. Android implements this in `androidApp`; the iPhone version will too. */
interface PlatformServices {
    val appVersionName: String
    val appVersionCode: Int

    /** What this device is called in the server's list of signed-in apps, like "Android (Pixel 9a)". */
    val deviceName: String

    /** Whether the system lets the app show notifications right now. */
    fun notificationsAllowed(): Boolean
    fun requestNotificationPermission(onResult: (Boolean) -> Unit)
    fun openNotificationSettings()

    /** Starts (or stops) looking for new events in the background, as often as the preferences say. */
    fun applyNotificationSchedule(prefs: NotificationPrefs, signedIn: Boolean)

    /** Looks for new events right now and reports how many notices were found, or null if it failed. */
    suspend fun checkForEventsNow(): Int?

    fun openUrl(url: String)

    /** Downloads the update (checking its checksum) and hands it to the system installer. */
    fun installUpdate(update: AppUpdate, onState: (InstallState) -> Unit)

    /** A camera view that reads barcodes and calls [onCode] with each new code it sees. */
    @Composable
    fun BarcodeScanner(modifier: Modifier, onCode: (String) -> Unit)
}

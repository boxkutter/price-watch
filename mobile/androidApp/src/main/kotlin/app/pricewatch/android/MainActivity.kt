package app.pricewatch.android

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import app.pricewatch.ui.App
import app.pricewatch.ui.Tab

class MainActivity : ComponentActivity() {
    private val app get() = application as PriceWatchApplication

    private val notificationPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { app.platform.permissionResult(it) }

    private val localNetwork = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        installSplashScreen()
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        app.platform.permissionRequester = { notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS) }
        // Android 17+ blocks connections to addresses on the home network without this permission.
        if (Build.VERSION.SDK_INT >= 37 && checkSelfPermission(Manifest.permission.ACCESS_LOCAL_NETWORK) != PackageManager.PERMISSION_GRANTED) {
            localNetwork.launch(Manifest.permission.ACCESS_LOCAL_NETWORK)
        }
        handle(intent)
        setContent { App(app.model) }
    }

    override fun onStart() {
        super.onStart()
        app.model.onForeground()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handle(intent)
    }

    /** A tapped notification says where to go. */
    private fun handle(intent: Intent?) {
        when (intent?.getStringExtra(Notifier.EXTRA_OPEN)) {
            Notifier.OPEN_ACTIVITY -> app.model.openActivity()
            Notifier.OPEN_SETTINGS -> app.model.select(Tab.Settings)
        }
        intent?.removeExtra(Notifier.EXTRA_OPEN)
    }
}

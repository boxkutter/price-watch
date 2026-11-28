package app.pricewatch.android

import android.app.Application
import app.pricewatch.shared.AppGraph
import app.pricewatch.ui.AppModel
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob

/** Builds the app's long-lived parts once, so the activity, the background check and rotation all share them. */
class PriceWatchApplication : Application() {
    lateinit var graph: AppGraph
        private set
    lateinit var platform: AndroidPlatformServices
        private set
    lateinit var model: AppModel
        private set

    override fun onCreate() {
        super.onCreate()
        Notifier.createChannels(this)
        graph = AppGraph(SecureKeyValueStore(this))
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
        platform = AndroidPlatformServices(this, graph, scope)
        model = AppModel(graph, platform, scope)
    }
}

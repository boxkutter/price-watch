package app.pricewatch.ui

import app.pricewatch.shared.AppGraph
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlin.time.Clock
import kotlin.time.Duration.Companion.hours
import kotlin.time.Instant

enum class Tab(val label: String) { Lookup("Lookup"), Monitor("Monitor"), Compare("Compare"), Settings("Settings") }

enum class MonitorView { Watching, Activity }

/**
 * Everything the screens show, held for the life of the process so a rotation or a trip to another
 * tab loses nothing. The platform shell makes one and passes it to [App].
 */
class AppModel(val graph: AppGraph, val platform: PlatformServices, private val scope: CoroutineScope) {
    val messages = Messages()
    val directory = Directory(graph.api)
    val monitors = MonitorModel(graph, scope, directory, messages)
    val lookup = LookupModel(graph, scope, directory, monitors, messages)
    val compare = CompareModel(graph, scope, messages)
    val settings = SettingsModel(graph, scope, directory, messages, platform)
    val barcodes = BarcodeModel(graph, scope, monitors, messages)

    private val _tab = MutableStateFlow(if (graph.sessions.current == null) Tab.Settings else Tab.Lookup)
    val tab: StateFlow<Tab> = _tab.asStateFlow()
    private val _monitorView = MutableStateFlow(MonitorView.Watching)
    val monitorView: StateFlow<MonitorView> = _monitorView.asStateFlow()

    private val _scan = MutableStateFlow<ScanPurpose?>(null)

    /** The camera is open for this purpose, or null when it is closed. */
    val scan: StateFlow<ScanPurpose?> = _scan.asStateFlow()

    private var lastUpdateCheck: Instant? = null

    init {
        // a linked or merged barcode regroups the same search (answered from the cache, no store is asked)
        // what a deal costs depends on membership: show current prices again
        directory.onMembershipChanged = {
            monitors.refresh()
            if (lookup.results.value != null && lookup.query.value.isNotBlank()) lookup.search()
            compare.state.value.gtin?.let { compare.lookup(it) }
        }
        barcodes.onChanged = { if (lookup.results.value != null && lookup.query.value.isNotBlank()) lookup.search() }
        scope.launch {
            graph.sessions.session.collect { session ->
                if (session != null) bootstrap() else signedOut()
            }
        }
    }

    fun startScan(purpose: ScanPurpose) { _scan.value = purpose }
    fun stopScan() { _scan.value = null }

    /** The camera read a barcode. */
    fun scanned(code: String) {
        val purpose = _scan.value ?: return
        _scan.value = null
        when (purpose) {
            ScanPurpose.Compare -> compare.scanned(code)
            is ScanPurpose.LinkBarcode -> {
                val gtin = app.pricewatch.shared.format.asBarcode(code)
                if (gtin == null) messages.say("That is not a product barcode")
                else barcodes.link(purpose.productId, purpose.productName, gtin)
            }
        }
    }

    fun select(tab: Tab) { _tab.value = tab }
    fun setMonitorView(view: MonitorView) { _monitorView.value = view }

    /** A notification was tapped: show what changed. */
    fun openActivity() {
        _tab.value = Tab.Monitor
        _monitorView.value = MonitorView.Activity
        monitors.close()
        if (graph.sessions.current != null) {
            monitors.refresh()
            monitors.refreshActivity()
        }
    }

    /** The app came to the front: show current prices, and look for an update about once an hour. */
    fun onForeground() {
        if (graph.sessions.current == null) return
        monitors.refresh()
        val last = lastUpdateCheck
        if (last == null || Clock.System.now() - last > 1.hours) {
            lastUpdateCheck = Clock.System.now()
            settings.checkForUpdate()
        }
    }

    private suspend fun bootstrap() {
        scope.launch { runCatching { directory.refresh() }.onFailure { messages.say(it.friendly()) } }
        monitors.refresh()
        // Note where the event feed stands, so nothing from before signing in arrives as an alert.
        scope.launch { runCatching { graph.notifier.poll() } }
        val prefs = graph.sessions.prefs.value
        platform.applyNotificationSchedule(prefs, true)
        if (prefs.enabled && !platform.notificationsAllowed()) platform.requestNotificationPermission { }
        lastUpdateCheck = Clock.System.now()
        settings.checkForUpdate()
    }

    private fun signedOut() {
        directory.clear(); monitors.clear(); lookup.clear(); compare.clear(); _scan.value = null
        platform.applyNotificationSchedule(graph.sessions.prefs.value, false)
    }
}

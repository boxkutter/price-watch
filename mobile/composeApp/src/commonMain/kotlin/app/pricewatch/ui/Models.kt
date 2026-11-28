package app.pricewatch.ui

import app.pricewatch.shared.AppGraph
import app.pricewatch.shared.api.ApiException
import app.pricewatch.shared.api.BarcodeBelongsToAnotherProduct
import app.pricewatch.shared.api.CompareProduct
import app.pricewatch.shared.api.CompareRow
import app.pricewatch.shared.api.Monitor
import app.pricewatch.shared.api.PricePoint
import app.pricewatch.shared.api.Saving
import app.pricewatch.shared.api.CheapestRef
import app.pricewatch.shared.format.asBarcode
import app.pricewatch.shared.format.toAmount
import app.pricewatch.shared.lookup.ProductGroup
import app.pricewatch.shared.lookup.groupResults
import app.pricewatch.shared.api.SearchItem
import app.pricewatch.shared.notify.EventDescriber
import app.pricewatch.shared.notify.Notice
import app.pricewatch.shared.session.AppUpdate
import app.pricewatch.shared.session.NotificationPrefs
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/** Words for a person, from anything that went wrong. */
fun Throwable.friendly(): String = when (this) {
    is ApiException -> when {
        rateLimited && retryAfterSeconds != null -> "That is too often. Try again in ${retryAfterSeconds} seconds."
        else -> message
    }
    else -> "Something went wrong. Try again."
}

/** A one-line message to show briefly (a snackbar). */
class Messages {
    private val flow = MutableSharedFlow<String>(extraBufferCapacity = 8, onBufferOverflow = BufferOverflow.DROP_OLDEST)
    val messages = flow.asSharedFlow()
    fun say(text: String) { flow.tryEmit(text) }
}

class MonitorModel(private val graph: AppGraph, private val scope: CoroutineScope, private val directory: Directory, private val messages: Messages) {
    private val api get() = graph.api

    private val _items = MutableStateFlow<List<Monitor>>(emptyList())
    val items: StateFlow<List<Monitor>> = _items.asStateFlow()
    private val _loading = MutableStateFlow(false)
    val loading: StateFlow<Boolean> = _loading.asStateFlow()
    private val _loadedOnce = MutableStateFlow(false)
    val loadedOnce: StateFlow<Boolean> = _loadedOnce.asStateFlow()
    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error.asStateFlow()

    private val _activity = MutableStateFlow<List<Notice>>(emptyList())
    val activity: StateFlow<List<Notice>> = _activity.asStateFlow()
    private val _activityLoading = MutableStateFlow(false)
    val activityLoading: StateFlow<Boolean> = _activityLoading.asStateFlow()

    private val _selected = MutableStateFlow<String?>(null)
    val selectedId: StateFlow<String?> = _selected.asStateFlow()
    private val _history = MutableStateFlow<List<PricePoint>?>(null)
    val history: StateFlow<List<PricePoint>?> = _history.asStateFlow()
    private val _monitorNotices = MutableStateFlow<List<Notice>>(emptyList())
    val monitorNotices: StateFlow<List<Notice>> = _monitorNotices.asStateFlow()
    private val _checking = MutableStateFlow(false)
    val checking: StateFlow<Boolean> = _checking.asStateFlow()

    /** Listing ids already being watched, so Lookup can show "Watching". */
    fun watchedListingIds(): Set<String> = _items.value.mapNotNull { it.conditions.listingId }.toSet()

    fun refresh() {
        scope.launch {
            _loading.value = true
            try {
                _items.value = api.monitors()
                _error.value = null
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _error.value = e.friendly()
            } finally {
                _loading.value = false
                _loadedOnce.value = true
            }
        }
    }

    fun refreshActivity() {
        scope.launch {
            _activityLoading.value = true
            try {
                val names = _items.value.associateBy { it.id }.ifEmpty { api.monitors().associateBy { it.id } }
                val all = mutableListOf<app.pricewatch.shared.api.ApiEvent>()
                var cursor: String? = null
                var pages = 0
                do {
                    val page = api.events(after = cursor, limit = 200)
                    all += page.items
                    cursor = page.nextCursor
                    pages++
                } while (cursor != null && pages < 25)
                _activity.value = all.takeLast(80).reversed().mapNotNull { EventDescriber.describe(it, names[it.monitorId]) }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                messages.say(e.friendly())
            } finally {
                _activityLoading.value = false
            }
        }
    }

    fun open(id: String) {
        _selected.value = id
        _history.value = null
        _monitorNotices.value = emptyList()
        val monitor = _items.value.firstOrNull { it.id == id } ?: return
        scope.launch {
            try {
                monitor.conditions.listingId?.let { _history.value = api.priceHistory(it) } ?: run { _history.value = emptyList() }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _history.value = emptyList()
                messages.say(e.friendly())
            }
        }
        scope.launch {
            runCatching { api.monitorEvents(id) }.onSuccess { events ->
                _monitorNotices.value = events.reversed().mapNotNull { EventDescriber.describe(it, monitor) }
            }
        }
    }

    fun close() { _selected.value = null }

    fun checkNow(id: String) {
        scope.launch {
            _checking.value = true
            try {
                api.checkNow(id)
                _items.value = api.monitors()
                if (_selected.value == id) open(id)
                messages.say("Prices checked")
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                messages.say(e.friendly())
            } finally {
                _checking.value = false
            }
        }
    }

    /** Changes the alert price of a watched product; null removes it. */
    fun setAlertPrice(id: String, price: String?, onDone: () -> Unit = {}) {
        scope.launch {
            try {
                api.setAlertPrice(id, price)
                _items.value = api.monitors()
                messages.say(if (price == null) "Alert price removed" else "Alert set to ${app.pricewatch.shared.format.formatMoney(price)}")
                onDone()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                messages.say(e.friendly())
            }
        }
    }

    fun remove(id: String) {
        scope.launch {
            try {
                api.removeMonitor(id)
                _items.update { list -> list.filterNot { it.id == id } }
                if (_selected.value == id) close()
                messages.say("Removed from your watchlist")
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                messages.say(e.friendly())
            }
        }
    }

    fun clear() {
        _items.value = emptyList(); _activity.value = emptyList(); _selected.value = null
        _error.value = null; _loadedOnce.value = false
    }
}

class LookupModel(private val graph: AppGraph, private val scope: CoroutineScope, private val directory: Directory, private val monitors: MonitorModel, private val messages: Messages) {
    private val api get() = graph.api

    private val _query = MutableStateFlow("")
    val query: StateFlow<String> = _query.asStateFlow()
    fun setQuery(text: String) { _query.value = text }

    /** Store ids to search; empty means all of "my stores". */
    private val _scope = MutableStateFlow<Set<String>>(emptySet())
    val storeScope: StateFlow<Set<String>> = _scope.asStateFlow()
    fun setScope(ids: Set<String>) { _scope.value = ids }

    private val _busy = MutableStateFlow(false)
    val busy: StateFlow<Boolean> = _busy.asStateFlow()
    private val _progress = MutableStateFlow<String?>(null)
    val progress: StateFlow<String?> = _progress.asStateFlow()
    private val _results = MutableStateFlow<List<ProductGroup>?>(null)
    val results: StateFlow<List<ProductGroup>?> = _results.asStateFlow()
    private val _problems = MutableStateFlow<List<String>>(emptyList())
    val problems: StateFlow<List<String>> = _problems.asStateFlow()
    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error.asStateFlow()
    private val _linkUrl = MutableStateFlow<String?>(null)
    /** A pasted product link waiting to be added; the person picks the store. */
    val linkUrl: StateFlow<String?> = _linkUrl.asStateFlow()

    private var job: Job? = null

    fun dismissError() { _error.value = null }

    fun clearQuery() {
        job?.cancel()
        _query.value = ""; _results.value = null; _problems.value = emptyList(); _error.value = null
        _linkUrl.value = null; _busy.value = false; _progress.value = null
    }

    fun search(fresh: Boolean = false) {
        val text = _query.value.trim()
        if (text.isEmpty()) return
        _error.value = null
        if (text.startsWith("http://") || text.startsWith("https://")) {
            _linkUrl.value = text; _results.value = null
            return
        }
        _linkUrl.value = null
        val gtin = asBarcode(text)
        if (gtin == null && text.length < 2) { _error.value = "Type at least two letters."; return }
        val mine = directory.mine.value
        val chosen = _scope.value.ifEmpty { mine.map { it.storeId }.toSet() }.filter { id -> mine.any { it.storeId == id } }
        if (chosen.isEmpty()) { _error.value = "Choose the stores to search in Settings first."; return }

        job?.cancel()
        job = scope.launch {
            _busy.value = true
            _results.value = emptyList()
            _problems.value = emptyList()
            _progress.value = "Searching ${chosen.size} ${if (chosen.size == 1) "store" else "stores"}…"
            val found = mutableListOf<SearchItem>()
            val problems = mutableListOf<String>()
            var failures = 0
            val chunks = chosen.chunked(5)
            try {
                chunks.map { ids ->
                    async {
                        try {
                            val r = api.search(query = text.takeIf { gtin == null }, gtin = gtin, storeIds = ids, fresh = fresh,
                                onProgress = { p -> p.text?.let { _progress.value = it } })
                            synchronized(found) { found += r.items; _results.value = groupResults(found.toList()) }
                            r.coverage.filter { it.status !in setOf("complete", "not_found") }.forEach {
                                val name = mine.firstOrNull { s -> s.storeId == it.storeId }?.name ?: "A store"
                                problems += "$name: ${coverageWords(it.status)}"
                            }
                        } catch (e: CancellationException) {
                            throw e
                        } catch (e: Exception) {
                            failures++
                            _error.value = e.friendly()
                        }
                    }
                }.awaitAll()
                _results.value = groupResults(found)
                _problems.value = problems
                if (failures < chunks.size) _error.value = null
            } finally {
                _busy.value = false
                _progress.value = null
            }
        }
    }

    private fun coverageWords(status: String) = when (status) {
        "paused", "disabled" -> "switched off for now"
        "rate_limited" -> "asked us to slow down"
        "timed_out" -> "took too long"
        "unsupported" -> "cannot be searched by name"
        else -> "could not be searched"
    }

    fun watch(item: SearchItem, maximumPrice: String?, onDone: () -> Unit = {}) {
        scope.launch {
            try {
                api.watch(item.storeId, item.listingId, item.displayName ?: item.name, maximumPrice)
                monitors.refresh()
                messages.say("Now watching ${item.displayName ?: item.name}")
                onDone()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                messages.say(e.friendly())
            }
        }
    }

    fun watchLink(storeId: String?, maximumPrice: String?) {
        val url = _linkUrl.value ?: return
        scope.launch {
            _busy.value = true
            _progress.value = "Asking the store about that product…"
            try {
                val m = api.watchLink(url, storeId, maximumPrice)
                monitors.refresh()
                messages.say("Now watching ${m.name}")
                _linkUrl.value = null; _query.value = ""
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _error.value = e.friendly()
            } finally {
                _busy.value = false; _progress.value = null
            }
        }
    }

    fun clear() = clearQuery()
}

class CompareModel(private val graph: AppGraph, private val scope: CoroutineScope, private val messages: Messages) {
    private val api get() = graph.api

    data class State(
        val gtin: String? = null,
        val product: CompareProduct? = null,
        val rows: List<CompareRow> = emptyList(),
        val cheapest: CheapestRef? = null,
        val saving: Saving? = null,
        val checkingLive: Boolean = false,
        val progress: String? = null,
        val loading: Boolean = false,
        val error: String? = null,
    )

    private val _state = MutableStateFlow(State())
    val state: StateFlow<State> = _state.asStateFlow()
    private val _here = MutableStateFlow<String?>(null)
    val hereStoreId: StateFlow<String?> = _here.asStateFlow()
    private val _typed = MutableStateFlow("")
    val typed: StateFlow<String> = _typed.asStateFlow()

    fun setTyped(text: String) { _typed.value = text.filter { it.isDigit() }.take(14) }
    fun setHere(id: String?) { _here.value = id; _state.value.gtin?.let { lookup(it) } }

    private var job: Job? = null

    fun lookupTyped() {
        val code = asBarcode(_typed.value)
        if (code == null) { _state.update { it.copy(error = "A barcode has 8, 12, 13 or 14 digits.") }; return }
        lookup(code)
    }

    fun scanned(code: String) {
        val gtin = asBarcode(code)
        if (gtin == null) { messages.say("That is not a product barcode"); return }
        _typed.value = gtin
        lookup(gtin)
    }

    fun lookup(gtin: String) {
        job?.cancel()
        val here = _here.value
        job = scope.launch {
            _state.value = State(gtin = gtin, loading = true)
            try {
                val known = api.knownPrices(gtin, here)
                _state.value = State(gtin = gtin, product = known.product, rows = known.known, cheapest = known.cheapest,
                    saving = known.savingVsHere, checkingLive = true, progress = "Checking the stores now…")
                val live = api.compareLive(gtin, here, onProgress = { p -> _state.update { it.copy(progress = p.text ?: it.progress) } })
                _state.update {
                    it.copy(product = live.product ?: it.product, rows = live.rows.ifEmpty { it.rows },
                        cheapest = live.cheapest ?: it.cheapest, saving = live.savingVsHere ?: it.saving,
                        checkingLive = false, progress = null)
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _state.update { it.copy(loading = false, checkingLive = false, progress = null, error = e.friendly()) }
            } finally {
                _state.update { it.copy(loading = false) }
            }
        }
    }

    fun reset() {
        job?.cancel(); _state.value = State(); _typed.value = ""
    }

    fun clear() { reset(); _here.value = null }
}

sealed interface ConnectState {
    data object Idle : ConnectState
    data object Busy : ConnectState
    data class Failed(val message: String) : ConnectState
}

class SettingsModel(private val graph: AppGraph, private val scope: CoroutineScope, private val directory: Directory, private val messages: Messages, private val platform: PlatformServices) {
    private val _connect = MutableStateFlow<ConnectState>(ConnectState.Idle)
    val connect: StateFlow<ConnectState> = _connect.asStateFlow()

    private val _update = MutableStateFlow<AppUpdate?>(null)
    val update: StateFlow<AppUpdate?> = _update.asStateFlow()
    private val _install = MutableStateFlow<InstallState>(InstallState.Idle)
    val install: StateFlow<InstallState> = _install.asStateFlow()
    private val _checkingUpdate = MutableStateFlow(false)
    val checkingUpdate: StateFlow<Boolean> = _checkingUpdate.asStateFlow()

    fun connect(server: String, email: String, password: String, onConnected: () -> Unit) = connecting(onConnected) {
        graph.signIn.connect(server, email, password, platform.deviceName)
    }

    /** Creates the account from an invitation, then signs in. */
    fun join(server: String, invitation: String, password: String, onConnected: () -> Unit) = connecting(onConnected) {
        graph.signIn.join(server, invitation, password, platform.deviceName)
    }

    private fun connecting(onConnected: () -> Unit, work: suspend () -> Unit) {
        scope.launch {
            _connect.value = ConnectState.Busy
            try {
                work()
                _connect.value = ConnectState.Idle
                onConnected()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _connect.value = ConnectState.Failed(e.friendly())
            }
        }
    }

    fun setStore(store: app.pricewatch.shared.api.Store, mine: Boolean) {
        scope.launch {
            try {
                directory.setMine(store, mine)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                messages.say(e.friendly())
            }
        }
    }

    sealed interface FindState {
        data object Idle : FindState
        data object Busy : FindState
        data class Failed(val message: String) : FindState
        data class Done(val count: Int) : FindState
    }

    private val _find = MutableStateFlow<FindState>(FindState.Idle)
    val find: StateFlow<FindState> = _find.asStateFlow()

    /** Asks a retailer for the stores near a place; the ones found are offered on the stores page. */
    fun findStores(retailerId: String, inputs: Map<String, String>) {
        scope.launch {
            _find.value = FindState.Busy
            try {
                _find.value = FindState.Done(directory.find(retailerId, inputs))
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _find.value = FindState.Failed(e.friendly())
            }
        }
    }

    fun resetFind() { _find.value = FindState.Idle }

    fun setMember(retailerId: String, member: Boolean) {
        scope.launch {
            try {
                directory.setMember(retailerId, member)
                messages.say(if (member) "Member prices now count for ${directory.retailerName(retailerId)}" else "Member prices no longer count")
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                messages.say(e.friendly())
            }
        }
    }

    fun clearConnectError() { if (_connect.value is ConnectState.Failed) _connect.value = ConnectState.Idle }

    fun signOut(onDone: () -> Unit) {
        scope.launch {
            graph.signIn.disconnect()
            onDone()
        }
    }

    fun savePrefs(prefs: NotificationPrefs) {
        graph.sessions.savePrefs(prefs)
        platform.applyNotificationSchedule(prefs, graph.sessions.current != null)
    }

    fun checkForUpdate(announce: Boolean = false) {
        scope.launch {
            _checkingUpdate.value = true
            val found = graph.updates.check(platform.appVersionCode)
            _update.value = found
            _checkingUpdate.value = false
            if (announce) messages.say(if (found == null) "You have the latest version" else "Version ${found.release.versionName} is available")
        }
    }

    fun installUpdate() {
        val update = _update.value ?: return
        _install.value = InstallState.Downloading(null)
        platform.installUpdate(update) { state -> _install.value = state }
    }

    fun checkEventsNow() {
        scope.launch {
            val n = platform.checkForEventsNow()
            messages.say(when {
                n == null -> "Could not check just now"
                n == 0 -> "Nothing new"
                n == 1 -> "1 new alert"
                else -> "$n new alerts"
            })
        }
    }
}

/** What a scan is for: looking a product up, comparing prices, or giving a product its barcode. */
sealed interface ScanPurpose {
    /** Look the barcode up in the stores (the Lookup tab's camera). */
    data object Lookup : ScanPurpose
    data object Compare : ScanPurpose
    data class LinkBarcode(val productId: String, val productName: String) : ScanPurpose
}

/** Gives a product a barcode (or takes back one a person gave it), asking before merging two products. */
class BarcodeModel(private val graph: AppGraph, private val scope: CoroutineScope, private val monitors: MonitorModel, private val messages: Messages) {
    private val api get() = graph.api

    data class Conflict(val productId: String, val productName: String, val gtin: String, val existingName: String)

    private val _conflict = MutableStateFlow<Conflict?>(null)

    /** Set when the scanned barcode already belongs to another product: the person decides whether they are the same item. */
    val conflict: StateFlow<Conflict?> = _conflict.asStateFlow()

    /** Called after a barcode was linked or taken back, so screens showing older data can refresh. */
    var onChanged: () -> Unit = {}

    private val _linked = MutableStateFlow<Set<String>>(emptySet())

    /** Products that got a barcode in this session, so screens showing older data stop offering to link one. */
    val linked: StateFlow<Set<String>> = _linked.asStateFlow()

    fun link(productId: String, productName: String, gtin: String, merge: Boolean = false, onDone: () -> Unit = {}) {
        scope.launch {
            try {
                api.linkBarcode(productId, gtin, merge)
                _conflict.value = null
                _linked.update { it + productId }
                messages.say(if (merge) "Merged: $productName has that barcode" else "Barcode linked to $productName")
                monitors.refresh()
                onChanged()
                onDone()
            } catch (e: BarcodeBelongsToAnotherProduct) {
                _conflict.value = Conflict(productId, productName, gtin, e.existing.name)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                messages.say(e.friendly())
            }
        }
    }

    fun confirmMerge() {
        val c = _conflict.value ?: return
        link(c.productId, c.productName, c.gtin, merge = true)
    }

    fun dismissConflict() { _conflict.value = null }

    fun unlink(productId: String) {
        scope.launch {
            try {
                api.unlinkBarcode(productId)
                _linked.update { it - productId }
                messages.say("Barcode removed")
                monitors.refresh()
                onChanged()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                messages.say(e.friendly())
            }
        }
    }
}

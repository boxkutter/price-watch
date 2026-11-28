package app.pricewatch.ui

import app.pricewatch.shared.api.ApiException
import app.pricewatch.shared.api.Connector
import app.pricewatch.shared.api.MyStore
import app.pricewatch.shared.api.PriceWatchApi
import app.pricewatch.shared.api.Store
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update

/** The stores and retailers the server knows, and which of them are "mine". */
class Directory(private val api: PriceWatchApi) {
    private val _stores = MutableStateFlow<List<Store>>(emptyList())
    val stores: StateFlow<List<Store>> = _stores.asStateFlow()

    private val _mine = MutableStateFlow<List<MyStore>>(emptyList())
    val mine: StateFlow<List<MyStore>> = _mine.asStateFlow()

    private val _connectors = MutableStateFlow<Map<String, Connector>>(emptyMap())
    val connectors: StateFlow<Map<String, Connector>> = _connectors.asStateFlow()

    private val _members = MutableStateFlow<Set<String>>(emptySet())

    /** The chains the person is a member of (ICA, City Gross): membership is per chain, not per shop. */
    val members: StateFlow<Set<String>> = _members.asStateFlow()

    /** Called after membership changed, so prices that depend on it can be shown again. */
    var onMembershipChanged: () -> Unit = {}

    private val _found = MutableStateFlow<List<String>>(emptyList())

    /** Ids of the stores the last "find stores near me" turned up, to offer first. */
    val found: StateFlow<List<String>> = _found.asStateFlow()

    private val _loaded = MutableStateFlow(false)
    val loaded: StateFlow<Boolean> = _loaded.asStateFlow()

    suspend fun refresh() {
        _stores.value = api.stores()
        _mine.value = api.myStores()
        _members.value = api.memberships()
        _connectors.value = runCatching { api.connectors() }.getOrDefault(emptyList()).associateBy { it.id }
        _loaded.value = true
    }

    /** Asks a retailer for the stores near a place; they join [stores] and their ids are kept in [found]. */
    suspend fun find(retailerId: String, inputs: Map<String, String>): Int {
        val ids = api.findStores(retailerId, inputs)
        _stores.value = api.stores()
        _found.value = ids
        return ids.size
    }

    fun clearFound() { _found.value = emptyList() }

    /** Retailers that can look their stores up for a person. */
    fun findable(): List<Connector> = _connectors.value.values.filter { it.canFindStores }.sortedBy { it.name }

    /** Says the person is (or is not) a member of a chain; shown at once, undone if the server refuses. */
    suspend fun setMember(retailerId: String, member: Boolean) {
        val before = _members.value
        _members.value = if (member) before + retailerId else before - retailerId
        try {
            api.setMember(retailerId, member)
        } catch (e: ApiException) {
            _members.value = before
            throw e
        }
        onMembershipChanged()
    }

    fun clear() {
        _members.value = emptySet()
        _found.value = emptyList()
        _stores.value = emptyList()
        _mine.value = emptyList()
        _connectors.value = emptyMap()
        _loaded.value = false
    }

    fun retailerName(retailerId: String?): String = retailerId?.let { _connectors.value[it]?.name } ?: retailerId.orEmpty()

    fun logoUrl(retailerId: String?): String? = retailerId?.let { api.absolute(_connectors.value[it]?.logoUrl) }

    fun isMine(storeId: String) = _mine.value.any { it.storeId == storeId }

    /** Adds or removes a store from "my stores", showing the change at once and undoing it if the server refuses. */
    suspend fun setMine(store: Store, mine: Boolean) {
        val before = _mine.value
        _mine.update { list ->
            if (mine) (list + MyStore(store.id, store.name, store.retailerId)).distinctBy { it.storeId }.sortedBy { it.name }
            else list.filterNot { it.storeId == store.id }
        }
        try {
            if (mine) api.addMyStore(store.id) else api.removeMyStore(store.id)
        } catch (e: ApiException) {
            _mine.value = before
            throw e
        }
    }
}

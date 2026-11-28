package app.pricewatch.shared.session

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/** Where small values live between launches. Android encrypts them with the Keystore; iOS will use the Keychain. */
interface KeyValueStore {
    fun get(key: String): String?
    fun put(key: String, value: String?)
}

class MemoryKeyValueStore : KeyValueStore {
    private val values = mutableMapOf<String, String>()
    override fun get(key: String) = values[key]
    override fun put(key: String, value: String?) {
        if (value == null) values.remove(key) else values[key] = value
    }
}

/** The server and credentials one signed-in person uses. */
data class Session(
    val baseUrl: String,
    val userToken: String,
    val clientToken: String,
    val clientId: String,
    val email: String,
)

data class NotificationPrefs(
    val enabled: Boolean = true,
    val priceDrops: Boolean = true,
    val offers: Boolean = true,
    val stock: Boolean = true,
    val priceIncreases: Boolean = false,
    val intervalMinutes: Int = 60,
    /** Hold alerts back overnight: nothing is announced between [quietStart] and [quietEnd] (minutes after midnight). */
    val quietEnabled: Boolean = false,
    val quietStart: Int = 22 * 60,
    val quietEnd: Int = 7 * 60,
) {
    companion object {
        /** WorkManager cannot run more often than every 15 minutes, and a store is checked at most hourly. */
        val intervals = listOf(15, 30, 60, 120, 360)
    }
}

/** Reads and writes the session and the app's own preferences. */
class SessionRepository(private val store: KeyValueStore) {
    private val _session = MutableStateFlow(load())
    val session: StateFlow<Session?> = _session.asStateFlow()
    val current: Session? get() = _session.value

    private val _prefs = MutableStateFlow(loadPrefs())
    val prefs: StateFlow<NotificationPrefs> = _prefs.asStateFlow()

    private val _expired = MutableStateFlow(false)

    /** True after the server refused the token, until the person signs in again. */
    val expired: StateFlow<Boolean> = _expired.asStateFlow()

    private fun load(): Session? {
        val base = store.get(BASE_URL) ?: return null
        return Session(
            baseUrl = base,
            userToken = store.get(USER_TOKEN) ?: return null,
            clientToken = store.get(CLIENT_TOKEN) ?: return null,
            clientId = store.get(CLIENT_ID) ?: return null,
            email = store.get(EMAIL) ?: "",
        )
    }

    private fun loadPrefs(): NotificationPrefs {
        val d = NotificationPrefs()
        fun flag(key: String, default: Boolean) = store.get(key)?.let { it == "1" } ?: default
        return NotificationPrefs(
            enabled = flag(P_ENABLED, d.enabled),
            priceDrops = flag(P_DROPS, d.priceDrops),
            offers = flag(P_OFFERS, d.offers),
            stock = flag(P_STOCK, d.stock),
            priceIncreases = flag(P_INCREASES, d.priceIncreases),
            intervalMinutes = store.get(P_INTERVAL)?.toIntOrNull() ?: d.intervalMinutes,
            quietEnabled = flag(P_QUIET, d.quietEnabled),
            quietStart = store.get(P_QUIET_START)?.toIntOrNull() ?: d.quietStart,
            quietEnd = store.get(P_QUIET_END)?.toIntOrNull() ?: d.quietEnd,
        )
    }

    fun signIn(session: Session) {
        store.put(BASE_URL, session.baseUrl)
        store.put(USER_TOKEN, session.userToken)
        store.put(CLIENT_TOKEN, session.clientToken)
        store.put(CLIENT_ID, session.clientId)
        store.put(EMAIL, session.email)
        // A new account starts with a clean feed: the first poll only notes where it stands.
        store.put(CURSOR, null)
        store.put(BASELINED, null)
        store.put(EXPIRY_ANNOUNCED, null)
        _expired.value = false
        _session.value = session
    }

    fun signOut() {
        listOf(USER_TOKEN, CLIENT_TOKEN, CLIENT_ID, CURSOR, BASELINED).forEach { store.put(it, null) }
        _expired.value = false
        _session.value = null
    }

    /** The server said 401: the token is no good any more. Keep the server and email so signing in again is quick. */
    fun markExpired() {
        if (_session.value != null) _expired.value = true
    }

    fun savePrefs(prefs: NotificationPrefs) {
        fun flag(b: Boolean) = if (b) "1" else "0"
        store.put(P_ENABLED, flag(prefs.enabled))
        store.put(P_DROPS, flag(prefs.priceDrops))
        store.put(P_OFFERS, flag(prefs.offers))
        store.put(P_STOCK, flag(prefs.stock))
        store.put(P_INCREASES, flag(prefs.priceIncreases))
        store.put(P_INTERVAL, prefs.intervalMinutes.toString())
        store.put(P_QUIET, flag(prefs.quietEnabled))
        store.put(P_QUIET_START, prefs.quietStart.toString())
        store.put(P_QUIET_END, prefs.quietEnd.toString())
        _prefs.value = prefs
    }

    /** Last server address and email, offered again on the sign-in form. */
    var lastServer: String
        get() = store.get(BASE_URL) ?: ""
        set(value) = store.put(BASE_URL, value.ifBlank { null })
    var lastEmail: String
        get() = store.get(EMAIL) ?: ""
        set(value) = store.put(EMAIL, value.ifBlank { null })

    var eventCursor: String?
        get() = store.get(CURSOR)
        set(value) = store.put(CURSOR, value)

    /** Whether the feed's starting point has been noted (so old events never notify). */
    var baselined: Boolean
        get() = store.get(BASELINED) == "1"
        set(value) = store.put(BASELINED, if (value) "1" else null)

    /** Whether the person was told their sign-in expired (once, not on every background check). */
    var expiryAnnounced: Boolean
        get() = store.get(EXPIRY_ANNOUNCED) == "1"
        set(value) = store.put(EXPIRY_ANNOUNCED, if (value) "1" else null)

    /** The newest app version already announced with a notification, so each update is announced once. */
    var announcedUpdate: Int
        get() = store.get(ANNOUNCED_UPDATE)?.toIntOrNull() ?: 0
        set(value) = store.put(ANNOUNCED_UPDATE, value.toString())

    private companion object {
        const val EXPIRY_ANNOUNCED = "expiry_announced"
        const val ANNOUNCED_UPDATE = "announced_update"
        const val BASE_URL = "base_url"
        const val USER_TOKEN = "user_token"
        const val CLIENT_TOKEN = "client_token"
        const val CLIENT_ID = "client_id"
        const val EMAIL = "email"
        const val CURSOR = "event_cursor"
        const val BASELINED = "event_baselined"
        const val P_ENABLED = "notify_enabled"
        const val P_DROPS = "notify_drops"
        const val P_OFFERS = "notify_offers"
        const val P_STOCK = "notify_stock"
        const val P_INCREASES = "notify_increases"
        const val P_INTERVAL = "notify_interval"
        const val P_QUIET = "notify_quiet"
        const val P_QUIET_START = "notify_quiet_start"
        const val P_QUIET_END = "notify_quiet_end"
    }
}

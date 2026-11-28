package app.pricewatch.shared

import app.pricewatch.shared.api.PriceWatchApi
import app.pricewatch.shared.notify.EventNotifier
import app.pricewatch.shared.session.KeyValueStore
import app.pricewatch.shared.session.SessionRepository
import app.pricewatch.shared.session.SignIn
import app.pricewatch.shared.session.UpdateChecker
import io.ktor.client.engine.HttpClientEngine

/** The app's long-lived parts, built once per process by the platform shell (Application on Android). */
class AppGraph(keyValueStore: KeyValueStore, engine: HttpClientEngine? = null) {
    val sessions = SessionRepository(keyValueStore)
    val api = PriceWatchApi(engine, session = { sessions.current }, onUnauthorized = { sessions.markExpired() })
    val signIn = SignIn(api, sessions)
    val notifier = EventNotifier(api, sessions)
    val updates = UpdateChecker(api, sessions)
}

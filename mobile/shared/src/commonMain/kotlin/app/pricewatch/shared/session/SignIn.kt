package app.pricewatch.shared.session

import app.pricewatch.shared.api.ApiException
import app.pricewatch.shared.api.PriceWatchApi

/**
 * Connects the app to a person's own Price Watch server with nothing but their address and login.
 * Access is invitation-only and has two layers (docs/api.md, "Getting access"): the person signs in,
 * and the app gets its own credential. The server makes that credential in one call
 * (`POST /v1/me/app-credentials`), so no administrator has to prepare anything.
 */
class SignIn(private val api: PriceWatchApi, private val sessions: SessionRepository) {

    /** Signs in with email and password. Throws [ApiException] with words fit to show. */
    suspend fun connect(server: String, email: String, password: String, deviceName: String) {
        val baseUrl = address(server)
        val mail = email.trim()
        if (mail.isEmpty() || password.isEmpty()) throw ApiException(0, "invalid_request", "Enter your email and password.")
        val login = try {
            api.login(baseUrl, mail, password)
        } catch (e: ApiException) {
            if (e.unauthorized) throw ApiException(401, e.code, "Wrong email or password.")
            throw e
        }
        finish(baseUrl, mail, login.accessToken, deviceName)
    }

    /**
     * Creates the account from an invitation (the person chooses a password), then signs in. The
     * invitation knows the email, so the person is not asked for it.
     */
    suspend fun join(server: String, invitation: String, password: String, deviceName: String) {
        val baseUrl = address(server)
        if (invitation.isBlank()) throw ApiException(0, "invalid_request", "Enter the invitation code you were given.")
        if (password.length < MIN_PASSWORD) throw ApiException(0, "invalid_request", "Choose a password of at least $MIN_PASSWORD characters.")
        val email = try {
            api.acceptInvitation(baseUrl, invitation.trim(), password)
        } catch (e: ApiException) {
            if (e.notFound || e.status == 400 || e.status == 410) throw ApiException(e.status, e.code, "That invitation code is not valid, or it has expired or been used.")
            throw e
        }
        val login = api.login(baseUrl, email, password)
        finish(baseUrl, email, login.accessToken, deviceName)
    }

    private fun address(server: String) = normalizeServerAddress(server)
        ?: throw ApiException(0, "invalid_request", "Enter the server address, for example 192.168.1.20:8085.")

    private suspend fun finish(baseUrl: String, email: String, userToken: String, deviceName: String) {
        val credential = api.createAppCredential(baseUrl, userToken, "Price Watch $deviceName")
        // Signing in again (after a token expired) replaces the earlier sign-in instead of piling up.
        sessions.current?.takeIf { it.baseUrl == baseUrl && it.email == email }?.let {
            runCatching { api.revokeAppCredential(baseUrl, userToken, it.clientId) }
        }
        sessions.signIn(Session(baseUrl, userToken, credential.clientToken, credential.clientId, email))
    }

    suspend fun disconnect() {
        api.logout()
        sessions.signOut()
    }

    companion object {
        /** The service refuses shorter passwords. */
        const val MIN_PASSWORD = 16
    }
}

/**
 * "192.168.1.20:8085" becomes "http://192.168.1.20:8085": a server on the home network has no
 * certificate, so a bare address, `localhost` or a `.local` name is plain http; any other name is https.
 */
fun normalizeServerAddress(input: String): String? {
    val text = input.trim().trimEnd('/')
    if (text.isEmpty() || text.any { it.isWhitespace() }) return null
    if (text.startsWith("http://") || text.startsWith("https://")) return text
    val host = text.substringBefore('/').substringBefore(':')
    val local = host == "localhost" || host.endsWith(".local") || host.endsWith(".lan") ||
        host.matches(Regex("""\d{1,3}(\.\d{1,3}){3}""")) || !host.contains('.')
    return (if (local) "http://" else "https://") + text
}

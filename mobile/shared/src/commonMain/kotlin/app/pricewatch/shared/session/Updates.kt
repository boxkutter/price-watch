package app.pricewatch.shared.session

import app.pricewatch.shared.api.ApiException
import app.pricewatch.shared.api.PriceWatchApi
import app.pricewatch.shared.api.ReleaseInfo

/** A newer app than the one installed, and where to get it. */
data class AppUpdate(val release: ReleaseInfo, val downloadUrl: String)

/** Asks the person's own server whether a newer app is published at /download. */
class UpdateChecker(private val api: PriceWatchApi, private val sessions: SessionRepository) {

    /** The update on offer, or null when the app is current, nothing is published or the server is out of reach. */
    suspend fun check(installedVersionCode: Int): AppUpdate? {
        val base = sessions.current?.baseUrl ?: sessions.lastServer.takeIf { it.isNotBlank() } ?: return null
        val release = try {
            api.latestRelease(base)
        } catch (e: ApiException) {
            return null
        } ?: return null
        if (release.versionCode <= installedVersionCode) return null
        return AppUpdate(release, absoluteUrl(base, release.url))
    }

    private fun absoluteUrl(base: String, url: String) =
        if (url.startsWith("http")) url else base.trimEnd('/') + url
}

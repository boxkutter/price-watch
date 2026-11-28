package app.pricewatch.shared

import app.pricewatch.shared.api.ApiException
import app.pricewatch.shared.api.BarcodeBelongsToAnotherProduct
import app.pricewatch.shared.api.SearchItem
import app.pricewatch.shared.format.asBarcode
import app.pricewatch.shared.format.formatMoney
import app.pricewatch.shared.format.formatUnitPrice
import app.pricewatch.shared.format.priceChange
import app.pricewatch.shared.format.relativeTime
import app.pricewatch.shared.lookup.groupResults
import app.pricewatch.shared.notify.NoticeKind
import app.pricewatch.shared.notify.QuietHours
import app.pricewatch.shared.session.NotificationPrefs
import kotlinx.datetime.TimeZone
import app.pricewatch.shared.session.normalizeServerAddress
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.test.runTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue
import kotlin.time.Instant

class FormatTest {
    @Test fun moneyUsesKronorWithACommaAndNeverInventsZero() {
        assertEquals("19,95 kr", formatMoney("19.95"))
        assertEquals("20,00 kr", formatMoney("20"))
        assertEquals("–", formatMoney(null))
        assertEquals("12,50 EUR", formatMoney("12.5", "EUR"))
        assertEquals("13,30 kr/kg", formatUnitPrice("13.30", "kg"))
        assertNull(formatUnitPrice(null, "kg"))
    }

    @Test fun priceChangeIsSignedAndUnknownWhenEitherSideIsUnknown() {
        assertEquals(-2.0, priceChange("17.95", "19.95")!!, 0.001)
        assertEquals(0.0, priceChange("19.95", "19.95"))
        assertNull(priceChange("19.95", null))
    }

    @Test fun barcodesAreDigitsOfTheRightLength() {
        assertEquals("7310130321085", asBarcode("7310130321085"))
        assertEquals("73101303", asBarcode("73101303"))
        assertNull(asBarcode("havregryn"))
        assertNull(asBarcode("12345"))
        assertNull(asBarcode("7310 1303 21085"))
    }

    @Test fun relativeTimeReadsNaturally() {
        val now = Instant.parse("2026-11-28T12:00:00Z")
        assertEquals("just now", relativeTime("2026-11-28T11:59:50Z", now))
        assertEquals("5 minutes ago", relativeTime("2026-11-28T11:55:00Z", now))
        assertEquals("2 hours ago", relativeTime("2026-11-28T10:00:00Z", now))
        assertEquals("yesterday", relativeTime("2026-11-27T10:00:00Z", now))
        assertEquals("", relativeTime(null, now))
    }

    @Test fun relativeTimeReadsTheServersTimestamps() {
        // the service writes Python isoformat(): microseconds and a +00:00 offset
        val now = Instant.parse("2026-11-28T12:00:00Z")
        assertEquals("1 minute ago", relativeTime("2026-11-28T11:58:50.492035+00:00", now))
        assertEquals("3 hours ago", relativeTime("2026-11-28T08:59:00.000001+00:00", now))
    }

    @Test fun serverAddressesGetTheRightScheme() {
        assertEquals("http://192.168.1.20:8085", normalizeServerAddress("192.168.1.20:8085"))
        assertEquals("http://unraid:8085", normalizeServerAddress(" unraid:8085/ "))
        assertEquals("https://prices.example.org", normalizeServerAddress("prices.example.org"))
        assertEquals("http://localhost:8085", normalizeServerAddress("localhost:8085"))
        assertEquals("https://x.org", normalizeServerAddress("https://x.org/"))
        assertNull(normalizeServerAddress("  "))
        assertNull(normalizeServerAddress("not an address"))
    }
}

private fun item(store: String, listing: String, price: String?, key: String? = null, name: String = "Havregryn") =
    SearchItem(productId = "p-$listing", listingId = listing, storeId = store, name = name,
        currentPrice = price, likeKey = key, brand = "AXA", size = "1.5 kg")

class GroupingTest {
    @Test fun sameItemInSeveralStoresIsOneCardWithTheCheapestFirst() {
        val groups = groupResults(listOf(
            item("a", "l1", "21.72", key = "barcode:1"),
            item("b", "l2", "19.95", key = "barcode:1"),
            item("c", "l3", null, key = "barcode:1"),
            item("a", "l4", "9.00", key = "barcode:2", name = "Gryn"),
        ))
        assertEquals(2, groups.size)
        val first = groups[0]
        assertEquals(listOf("l2", "l1", "l3"), first.offers.map { it.listingId })
        assertEquals("b", first.cheapest.storeId)
        assertEquals(3, first.storeCount)
        assertEquals(1.77, first.spread!!, 0.001)
        assertEquals("AXA · 1.5 kg", first.subtitle)
    }

    @Test fun anItemWithoutALikeKeyStandsAlone() {
        val groups = groupResults(listOf(item("a", "l1", "5"), item("b", "l2", "6")))
        assertEquals(2, groups.size)
        assertNull(groups[0].spread)
    }
}

class NotifierTest {
    private fun event(id: String, type: String, payload: String, monitor: String = "m1") =
        """{"id":"$id","type":"$type","monitor_id":"$monitor","payload":$payload,"created_at":"2026-11-28T10:00:00Z"}"""

    private val monitors = """{"items":[{"id":"m1","name":"Havregryn 1,5KG AXA","store_name":"City Gross Höör",
        "latest":{"current_price":"17.95","currency":"SEK"}}]}"""

    @Test fun theFirstLookOnlyNotesWhereTheFeedStandsThenNewEventsNotify() = runTest {
        val server = FakeServer()
        server.on("GET /v1/events", """{"items":[${event("e1", "price.decreased", """{"old":"20.00","new":"19.95"}""")}],"next_cursor":null}""")
        server.on("GET /v1/monitors", monitors)
        val graph = graphWith(server)

        assertTrue(graph.notifier.poll().isEmpty(), "old events must not notify after signing in")
        assertEquals("e1", graph.sessions.eventCursor)

        server.on("GET /v1/events", """{"items":[${event("e2", "price.decreased", """{"old":"19.95","new":"17.95"}""")}],"next_cursor":null}""")
        val notices = graph.notifier.poll()
        assertEquals(1, notices.size)
        assertEquals("Havregryn 1,5KG AXA", notices[0].title)
        assertEquals("Now 17,95 kr at City Gross Höör, was 19,95 kr", notices[0].text)
        assertEquals("e2", graph.sessions.eventCursor)
        // the second request asked only for what came after the first event
        assertTrue(server.requests.filter { it.url.encodedPath == "/v1/events" }.last().url.parameters["after"] == "e1")
    }

    @Test fun twoChecksAtOnceNeverAnnounceOldEventsAfterSigningIn() = runTest {
        // the background worker and the app's own first look can run together right after sign-in
        val server = FakeServer()
        val feed = listOf("e1")
        server.onRequest("GET /v1/events") { request ->
            val after = request.url.parameters["after"]
            val rest = if (after == null) feed else feed.drop(feed.indexOf(after) + 1)
            """{"items":[${rest.joinToString(",") { event(it, "price.decreased", """{"old":"20.00","new":"19.95"}""") }}],"next_cursor":null}"""
        }
        server.on("GET /v1/monitors", monitors)
        val graph = graphWith(server)

        val both = listOf(async { graph.notifier.poll() }, async { graph.notifier.poll() }).awaitAll()

        assertTrue(both.all { it.isEmpty() }, "events from before signing in must stay quiet: $both")
        assertEquals("e1", graph.sessions.eventCursor)
    }

    @Test fun reachingTheTargetIsOneNoticeAndPreferencesAreHonoured() = runTest {
        val server = FakeServer()
        server.on("GET /v1/events", """{"items":[],"next_cursor":null}""")
        server.on("GET /v1/monitors", monitors)
        val graph = graphWith(server)
        graph.notifier.poll() // baseline

        server.on("GET /v1/events", """{"items":[
            ${event("e1", "price.decreased", """{"old":"19.95","new":"17.95"}""")},
            ${event("e2", "price.threshold_reached", """{"price":"17.95","threshold":"18.00"}""")},
            ${event("e3", "price.increased", """{"old":"17.95","new":"18.50"}""")},
            ${event("e4", "offer.detected", """{"terms":"2 för 35 kr","eligibility":"public"}""")},
            ${event("e5", "something.new", "{}")}],"next_cursor":null}""")
        val notices = graph.notifier.poll()
        // the drop is folded into the target; the increase is off by default; the unknown type is ignored
        assertEquals(listOf(NoticeKind.TargetReached, NoticeKind.Offer), notices.map { it.kind })
        assertEquals("Reached your target: 17,95 kr at City Gross Höör (target 18,00 kr)", notices[0].text)
        assertEquals("e5", graph.sessions.eventCursor)
    }

    @Test fun aGoneCursorStartsAgainQuietly() = runTest {
        val server = FakeServer()
        server.on("GET /v1/events", """{"code":"not_found","message":"Cursor not found"}""", 404)
        val graph = graphWith(server)
        graph.sessions.baselined = true
        graph.sessions.eventCursor = "deleted"
        // the saved place is dropped and the feed is read again from the start; here that fails too, so it surfaces
        assertFailsWith<ApiException> { graph.notifier.poll() }
        assertNull(graph.sessions.eventCursor)
        assertFalse(graph.sessions.baselined)

        server.on("GET /v1/events", """{"items":[${event("e1", "product.restocked", "{}")}],"next_cursor":null}""")
        server.on("GET /v1/monitors", monitors)
        assertTrue(graph.notifier.poll().isEmpty(), "starting again is quiet: old events are not announced")
        assertEquals("e1", graph.sessions.eventCursor)
    }
}

class MultiBuyTest {
    private fun event(id: String, type: String, payload: String) =
        """{"id":"$id","type":"$type","monitor_id":"m1","payload":$payload,"created_at":"2026-11-28T10:00:00Z"}"""

    private val monitors = """{"items":[{"id":"m1","name":"Oats","store_name":"City Gross Höör","latest":{"current_price":"20.00","currency":"SEK"}}]}"""

    @Test fun aMultiBuyThatReachesTheTargetIsAnnouncedPerItemAndTheOfferNoticeIsFolded() = runTest {
        val server = FakeServer()
        server.on("GET /v1/events", """{"items":[],"next_cursor":null}""")
        server.on("GET /v1/monitors", monitors)
        val graph = graphWith(server)
        graph.notifier.poll() // baseline

        server.on("GET /v1/events", """{"items":[
            ${event("e1", "offer.detected", """{"terms":"2 för 35 kr","eligibility":"public"}""")},
            ${event("e2", "price.threshold_reached", """{"price":"17.50","threshold":"19","multibuy":true,"terms":"2 för 35 kr","quantity":2}""")}],"next_cursor":null}""")
        val notices = graph.notifier.poll()

        assertEquals(1, notices.size, "the plain offer notice adds nothing once the target was reached by it")
        assertEquals(NoticeKind.TargetReached, notices[0].kind)
        assertTrue(notices[0].viaOffer)
        assertEquals("Reached your target: 17,50 kr each with 2 för 35 kr at City Gross Höör (target 19,00 kr)", notices[0].text)
    }

    @Test fun anOfferThatDoesNotReachTheTargetStillAnnouncesTheOffer() = runTest {
        val server = FakeServer()
        server.on("GET /v1/events", """{"items":[],"next_cursor":null}""")
        server.on("GET /v1/monitors", monitors)
        val graph = graphWith(server)
        graph.notifier.poll()
        server.on("GET /v1/events", """{"items":[${event("e1", "offer.detected", """{"terms":"2 för 39 kr","eligibility":"public"}""")}],"next_cursor":null}""")
        assertEquals(listOf(NoticeKind.Offer), graph.notifier.poll().map { it.kind })
    }

    @Test fun aWatchedProductCarriesItsBestMultiBuyAndSearchOffersTheirPricePerItem() = runTest {
        val server = FakeServer()
        server.on("GET /v1/monitors", """{"items":[{"id":"m1","name":"Oats","offer":{"terms":"2 för 35 kr","quantity":2,"unit_price":"17.50","valid_to":null}},{"id":"m2","name":"Rice","offer":null}]}""")
        val items = graphWith(server).api.monitors()
        assertEquals("17.50", items[0].offer?.unitPrice)
        assertEquals(2, items[0].offer?.quantity)
        assertNull(items[1].offer)
        val offer = app.pricewatch.shared.api.ApiJson.decodeFromString<app.pricewatch.shared.api.Offer>(
            """{"terms":"2 för 35 kr","quantity":2,"unit_price":"17.50","eligibility":"public"}""")
        assertEquals("17.50", offer.unitPrice)
    }
}

class MemberDealsTest {
    private fun item(store: String, listing: String, shelf: String, best: String? = null, member: Boolean = false, key: String = "barcode:1") =
        SearchItem(productId = "p", listingId = listing, storeId = store, name = "Havregryn", currentPrice = shelf, likeKey = key,
            best = best?.let { app.pricewatch.shared.api.BestOffer(terms = "Medlemspris: $it kr", quantity = 1, unitPrice = it, member = member) })

    @Test fun storesAreRankedByWhatOneCostsThePersonNotByTheShelfPrice() {
        val groups = groupResults(listOf(
            item("coop", "l1", "17.50"),
            item("citygross", "l2", "19.95", best = "15.00", member = true),   // dearer on the shelf, cheapest for a member
        ))
        val group = groups.single()
        assertEquals(listOf("citygross", "coop"), group.offers.map { it.storeId })
        assertEquals("15.00", group.cheapest.effectivePrice)
        assertEquals(2.5, group.spread!!, 0.001, "the spread is measured on what is paid")
    }

    @Test fun anItemWithNoDealCostsItsShelfPrice() {
        assertEquals("17.50", item("coop", "l1", "17.50").effectivePrice)
    }

    @Test fun aMemberPriceThatReachedTheTargetIsWordedAsSuch() = runTest {
        val server = FakeServer()
        server.on("GET /v1/events", """{"items":[],"next_cursor":null}""")
        server.on("GET /v1/monitors", """{"items":[{"id":"m1","name":"Oats","store_name":"City Gross Höör","latest":{"current_price":"20.00","currency":"SEK"}}]}""")
        val graph = graphWith(server)
        graph.notifier.poll()
        server.on("GET /v1/events", """{"items":[{"id":"e1","type":"price.threshold_reached","monitor_id":"m1","payload":{"price":"15.00","threshold":"18","member":true,"multibuy":false,"terms":"Medlemspris: 15.00 kr","quantity":1},"created_at":"2026-11-28T10:00:00Z"}],"next_cursor":null}""")
        val notice = graph.notifier.poll().single()
        assertEquals("Reached your target with your member price: 15,00 kr with Medlemspris: 15.00 kr at City Gross Höör (target 18,00 kr)", notice.text)
    }

    @Test fun membershipIsPerChainAndShowsOnTheStoresTheServerReports() = runTest {
        val server = FakeServer()
        server.on("PUT /v1/me/memberships/se.citygross", "", 204)
        server.on("DELETE /v1/me/memberships/se.citygross", "", 204)
        server.on("GET /v1/me/stores", """{"items":[
            {"store_id":"a","name":"City Gross Höör","retailer_id":"se.citygross","member":true},
            {"store_id":"b","name":"City Gross Staffanstorp","retailer_id":"se.citygross","member":true},
            {"store_id":"c","name":"Coop Online Syd","retailer_id":"se.coop"}]}""")
        val graph = graphWith(server)
        graph.api.setMember("se.citygross", true)
        graph.api.setMember("se.citygross", false)
        assertEquals(listOf("PUT", "DELETE"), server.requests.filter { it.url.encodedPath.contains("memberships") }.map { it.method.value })
        val mine = graph.api.myStores()
        assertEquals(listOf(true, true, false), mine.map { it.member })
    }
}

class SignInTest {
    private val login = """{"access_token":"tok","expires_in":100,"user_id":"u"}"""
    private val credential = """{"client_id":"cid","client_token":"ctok","scopes":["catalogue:read"]}"""

    @Test fun anEmailAndPasswordIsAllThatIsNeeded() = runTest {
        val server = FakeServer()
        server.on("POST /v1/auth/login", login)
        server.on("POST /v1/me/app-credentials", credential, 201)
        val graph = graphWith(server, signedInSession = false)

        graph.signIn.connect("192.168.1.20:8085", " me@example.org ", "pw", "Pixel 9a")

        val s = assertNotNull(graph.sessions.current)
        assertEquals("http://192.168.1.20:8085", s.baseUrl)
        assertEquals("tok", s.userToken)
        assertEquals("ctok", s.clientToken)
        assertEquals("cid", s.clientId)
        val made = server.requests.first { it.url.encodedPath == "/v1/me/app-credentials" }
        assertNull(made.headers["X-Client-Token"], "the app has no client token yet: the person's token is enough")
        assertEquals("Bearer tok", made.headers["Authorization"])
        assertTrue(made.body.toString().isNotEmpty())
    }

    @Test fun anInvitationCreatesTheAccountAndSignsInWithoutAskingForTheEmail() = runTest {
        val server = FakeServer()
        server.on("POST /v1/auth/accept-invitation", """{"user_id":"u","email":"new@example.org"}""", 201)
        server.on("POST /v1/auth/login", login)
        server.on("POST /v1/me/app-credentials", credential, 201)
        val graph = graphWith(server, signedInSession = false)

        graph.signIn.join("unraid:8085", " invite-code ", "a-password-of-16-chars", "Pixel 9a")

        assertEquals("new@example.org", graph.sessions.current?.email)
        // a short password never reaches the server
        val e = assertFailsWith<ApiException> { graph.signIn.join("unraid:8085", "code", "short", "Pixel 9a") }
        assertTrue(e.message.contains("16"))
    }

    @Test fun signingInAgainReplacesTheEarlierAppSignIn() = runTest {
        val server = FakeServer()
        server.on("POST /v1/auth/login", login)
        server.on("POST /v1/me/app-credentials", """{"client_id":"new-id","client_token":"new-tok"}""", 201)
        server.on("DELETE /v1/me/app-credentials/client-id", "", 204)
        val graph = graphWith(server) // already signed in as me@example.org on http://server:8085 with client-id
        graph.sessions.markExpired()

        graph.signIn.connect("server:8085", "me@example.org", "pw", "Pixel 9a")

        assertEquals("new-id", graph.sessions.current?.clientId)
        assertTrue(server.requests.any { it.method.value == "DELETE" && it.url.encodedPath == "/v1/me/app-credentials/client-id" })
        assertFalse(graph.sessions.expired.value)
    }

    @Test fun signingOutRevokesTheAppAndForgetsTheSession() = runTest {
        val server = FakeServer()
        server.on("DELETE /v1/me/app-credentials/client-id", "", 204)
        server.on("POST /v1/auth/logout", "", 204)
        val graph = graphWith(server)
        graph.signIn.disconnect()
        assertNull(graph.sessions.current)
        assertTrue(server.requests.any { it.method.value == "DELETE" && it.url.encodedPath.endsWith("/client-id") })
    }

    @Test fun mistakesComeBackInWordsAPersonCanActOn() = runTest {
        val server = FakeServer()
        server.on("POST /v1/auth/login", """{"code":"unauthorized","message":"Invalid credentials"}""", 401)
        val graph = graphWith(server, signedInSession = false)
        var e = assertFailsWith<ApiException> { graph.signIn.connect("s:1", "a@b.c", "bad", "Pixel") }
        assertEquals("Wrong email or password.", e.message)

        e = assertFailsWith<ApiException> { graph.signIn.connect("s:1", "", "", "Pixel") }
        assertTrue(e.message.contains("email"))
        e = assertFailsWith<ApiException> { graph.signIn.connect("not an address", "a@b.c", "pw", "Pixel") }
        assertTrue(e.message.contains("server address"))

        server.on("POST /v1/auth/accept-invitation", """{"code":"not_found","message":"Invitation not found"}""", 404)
        e = assertFailsWith<ApiException> { graph.signIn.join("s:1", "old-code", "a-password-of-16-chars", "Pixel") }
        assertTrue(e.message.contains("invitation"))
        assertNull(graph.sessions.current)
    }

    @Test fun anUnreachableServerSaysSo() = runTest {
        val graph = AppGraph(app.pricewatch.shared.session.MemoryKeyValueStore(),
            io.ktor.client.engine.mock.MockEngine { throw RuntimeException("no route to host") })
        val e = assertFailsWith<ApiException> { graph.signIn.connect("s:1", "a@b.c", "pw", "Pixel") }
        assertTrue(e.unreachable)
    }

    @Test fun anExpiredTokenIsRemembered() = runTest {
        val server = FakeServer()
        server.on("GET /v1/monitors", """{"code":"unauthorized","message":"Invalid token"}""", 401)
        val graph = graphWith(server)
        assertFailsWith<ApiException> { graph.api.monitors() }
        assertTrue(graph.sessions.expired.value)
    }
}

class ApiCallsTest {
    private val monitor = """{"id":"m1","name":"Oats","conditions":{"listing_id":"l1","maximum_price":"17.50"}}"""

    @Test fun anAlertPriceIsSetAndRemovedWithPatch() = runTest {
        val server = FakeServer()
        server.on("PATCH /v1/monitors/m1", monitor)
        val graph = graphWith(server)

        assertEquals("17.50", graph.api.setAlertPrice("m1", "17.50").conditions.maximumPrice)
        graph.api.setAlertPrice("m1", null)

        val bodies = server.requests.filter { it.method.value == "PATCH" }.map { it.body.toString() }
        assertEquals(2, bodies.size)
        val sent = server.sentBodies()
        assertEquals("""{"maximum_price":"17.50"}""", sent[0])
        assertEquals("""{"maximum_price":null}""", sent[1], "null removes the alert price; leaving the field out would keep it")
    }

    @Test fun aBarcodeThatBelongsToAnotherProductIsAQuestionNotAnError() = runTest {
        val server = FakeServer()
        server.on("POST /v1/products/p1/barcode",
            """{"code":"conflict","message":"x","details":[{"existing_product":{"id":"p2","name":"Havregryn 1,5KG AXA","retailers":["se.coop"]}}]}""", 409)
        val graph = graphWith(server)

        val e = assertFailsWith<BarcodeBelongsToAnotherProduct> { graph.api.linkBarcode("p1", "7310130321085") }
        assertEquals("p2", e.existing.id)
        assertEquals("Havregryn 1,5KG AXA", e.existing.name)

        server.on("POST /v1/products/p1/barcode", """{"outcome":"merged"}""")
        graph.api.linkBarcode("p1", "7310130321085", merge = true)
        assertTrue(server.sentBodies().last().contains(""""merge":true"""))
    }

    @Test fun aBarcodeTheStorePublishesCannotBeChangedAndSaysWhy() = runTest {
        val server = FakeServer()
        server.on("POST /v1/products/p1/barcode", """{"code":"conflict","message":"The store publishes this barcode"}""", 409)
        val e = assertFailsWith<ApiException> { graphWith(server).api.linkBarcode("p1", "7310130321085") }
        assertEquals("The store publishes this barcode", e.message)
    }

    @Test fun findingStoresGivesBackTheirIds() = runTest {
        val server = FakeServer()
        server.on("POST /v1/retailers/se.coop/stores/discover", """{"job_id":"j1","status":"queued"}""", 202)
        server.on("GET /v1/jobs/j1", """{"id":"j1","state":"succeeded","result":{"status":"success","store_ids":["s1","s2"]}}""")
        val ids = graphWith(server).api.findStores("se.coop", mapOf("postcode" to "264 31"))
        assertEquals(listOf("s1", "s2"), ids)
        assertEquals("""{"postcode":"264 31"}""", server.sentBodies().first())
    }

    @Test fun aMistypedPostcodeComesBackInTheRetailersWords() = runTest {
        val server = FakeServer()
        server.on("POST /v1/retailers/se.coop/stores/discover", """{"job_id":"j1","status":"queued"}""", 202)
        server.on("GET /v1/jobs/j1", """{"id":"j1","state":"extraction_failed","result":{"status":"extraction_failed","detail":"No Coop store found for postcode 00000"}}""")
        val e = assertFailsWith<ApiException> { graphWith(server).api.findStores("se.coop", mapOf("postcode" to "00000")) }
        assertEquals("No Coop store found for postcode 00000", e.message)
    }
}

class QuietHoursTest {
    @Test fun aNightRunsPastMidnight() {
        val night = { m: Int -> QuietHours.contains(22 * 60, 7 * 60, m) }
        assertTrue(night(23 * 60)); assertTrue(night(0)); assertTrue(night(6 * 60 + 59))
        assertFalse(night(7 * 60)); assertFalse(night(12 * 60)); assertFalse(night(21 * 60 + 59))
        assertTrue(night(22 * 60))
    }

    @Test fun aDaytimePeriodAndAnEmptyOne() {
        assertTrue(QuietHours.contains(13 * 60, 15 * 60, 14 * 60))
        assertFalse(QuietHours.contains(13 * 60, 15 * 60, 15 * 60))
        assertFalse(QuietHours.contains(8 * 60, 8 * 60, 8 * 60), "the same start and end means no quiet hours")
    }

    @Test fun onlyWhenSwitchedOnAndInTheTimeZoneOfThePhone() {
        val prefs = NotificationPrefs(quietEnabled = true)
        val stockholm = TimeZone.of("Europe/Stockholm") // UTC+1 in winter
        assertTrue(QuietHours.isQuiet(prefs, Instant.parse("2026-11-28T22:30:00Z"), stockholm), "23:30 there")
        assertFalse(QuietHours.isQuiet(prefs, Instant.parse("2026-11-28T08:00:00Z"), stockholm), "09:00 there")
        assertFalse(QuietHours.isQuiet(prefs.copy(quietEnabled = false), Instant.parse("2026-11-28T22:30:00Z"), stockholm))
        assertEquals("07:05", QuietHours.label(7 * 60 + 5))
    }

    @Test fun thePreferencesSurviveARestart() {
        val store = app.pricewatch.shared.session.MemoryKeyValueStore()
        val repo = app.pricewatch.shared.session.SessionRepository(store)
        repo.savePrefs(NotificationPrefs(quietEnabled = true, quietStart = 23 * 60 + 30, quietEnd = 6 * 60))
        val again = app.pricewatch.shared.session.SessionRepository(store).prefs.value
        assertTrue(again.quietEnabled); assertEquals(23 * 60 + 30, again.quietStart); assertEquals(6 * 60, again.quietEnd)
    }
}

class UpdateTest {
    private val release = """{"version_code":5,"version_name":"0.5.0","notes":"Better charts","size_bytes":1000,"sha256":"ab","url":"/download/pricewatch.apk"}"""

    @Test fun aNewerAppIsOfferedAndTheSameOrOlderIsNot() = runTest {
        val server = FakeServer()
        server.on("GET /download/latest.json", release)
        val graph = graphWith(server)
        val update = assertNotNull(graph.updates.check(installedVersionCode = 4))
        assertEquals("http://server:8085/download/pricewatch.apk", update.downloadUrl)
        assertEquals("0.5.0", update.release.versionName)
        assertNull(graph.updates.check(installedVersionCode = 5))
        assertNull(graph.updates.check(installedVersionCode = 9))
        assertNull(server.requests.first().headers["Authorization"], "the update check needs no sign-in")
    }

    @Test fun nothingPublishedOrAnUnreachableServerMeansNoUpdate() = runTest {
        assertNull(graphWith(FakeServer()).updates.check(1))
        val down = AppGraph(app.pricewatch.shared.session.MemoryKeyValueStore(),
            io.ktor.client.engine.mock.MockEngine { throw RuntimeException("down") })
        down.sessions.signIn(signedIn)
        assertNull(down.updates.check(1))
    }
}

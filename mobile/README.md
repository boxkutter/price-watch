# Price Watch for Android (and, later, iPhone and iPad)

A phone app for the Price Watch service. Four tabs along the bottom:

| Tab | What it does |
|---|---|
| **Lookup** | Search every store you chose by product name or barcode, or paste a product page link. Results of the same item are lined up by store, cheapest first, with the price per item of any multi-buy. Tap **Watch**, with an optional alert price. An item with no barcode can be given one (scan or type it). |
| **Monitor** | Your watchlist with live prices, change badges and "target reached"; per product a price chart, the alert price (change or remove it), the barcode and any multi-buy; an **Activity** feed of price drops, offers and restocks. |
| **Compare** | In a shop: scan a barcode (or type it) and see that item's price in each of your stores, cheapest first, and what you would save. |
| **Settings** | Sign in, choose your stores (or find the ones near you by postcode or town), choose which alerts you want, quiet hours and how often the phone looks, update the app. |

The phone **notifies** you when a watched product drops in price, reaches your target, goes on offer
or is back in stock.

## How it is built

Kotlin Multiplatform, so the iPhone and iPad app is mostly adding iOS targets, not a rewrite. The layout
follows the other mobile apps on this machine (`mobileauditor`, `mysimple-template`).

```
mobile/
  shared/       Kotlin (commonMain): the API client, models, sign-in, event and notification logic,
                price formatting, update check. No UI and nothing Android-specific. Tested on the JVM.
  composeApp/   Compose Multiplatform UI (commonMain): theme, the four tabs, state holders.
                `PlatformServices` is the one interface for what only a phone can do.
  androidApp/   The Android shell: encrypted storage, WorkManager, notifications, camera scanner,
                update installer, manifest. This is the only module that knows it is Android.
  scripts/publish.sh   Build a signed release and publish it to the server (see "Updates").
```

To add iOS: add the iOS targets to `shared` and `composeApp` (Ktor's Darwin engine in `iosMain`), write
an iOS `PlatformServices` (Keychain storage, APNs/background refresh, AVFoundation scanner) and a thin
SwiftUI host. Nothing in `commonMain` needs to change.

## Run it

Needs JDK 21 and the Android SDK (`local.properties` has `sdk.dir`).

```bash
./gradlew :androidApp:assembleDebug          # build
./gradlew :shared:testAndroidHostTest :androidApp:testDebugUnitTest   # tests
adb install -r androidApp/build/outputs/apk/debug/androidApp-debug.apk
```

On an emulator, `adb reverse tcp:8085 tcp:8085` lets the app reach the development server as
`localhost:8085`.

## Signing in

Enter the **server address** (`192.168.1.20:8085`; a bare address, `localhost` or a `.local` name is plain
http, anything else https), your **email** and your **password**. That is all: after the login the app asks
the server for its own credential (`POST /v1/me/app-credentials`), named for the phone, so no administrator
has to create anything and there is no App ID or token to type. The server and email are remembered, so
signing in again needs only the password.

A new person taps **I have an invitation code** and enters the server address, the code the administrator
gave them and the password they choose (16 characters or more); the invitation knows the email.

The user token (30 days) and the app's credential are stored encrypted with a key in the Android Keystore.
Signing out revokes the app's credential on the server (it can also be revoked there, and up to 10 phones
are kept per person). When the server refuses the token the app says so and asks you to sign in again,
which replaces the earlier sign-in.

## Notifications

The service has no push channel for phones yet (it has signed webhooks and the event feed). The app uses
the event feed: a background job (WorkManager) asks `GET /v1/events?after=<last seen>` as often as you
chose (15 minutes to 6 hours; the server checks each product about hourly) and turns new events into
notifications, one per event plus a grouped summary. It keeps its place in the feed, so nothing is
announced twice, and the first look after signing in only notes where the feed stands.

**Quiet hours** (Settings, for example 22:00 to 07:00) hold alerts back: the phone does not even ask the
server during them, so nothing is lost, and the first look afterwards announces what happened overnight.

**Multi-buys count towards the alert price.** "2 för 35 kr" is 17,50 kr each, so it reaches an alert price of
19 kr even when one item costs 20 kr; the server reads the wording ("3 för 2", "Köp 2 betala för 1" and
"19 kr/st vid köp av 2" too) and the app shows the price per item beside the offer, marks "Target reached" and
words the notification as "17,50 kr each with 2 för 35 kr". Member offers are never counted.

This needs no Google account or Firebase, which suits a self-hosted server and a phone without Google
services. The cost is that alerts arrive within the chosen interval, not instantly. **On iPhone this is
the weak spot**: iOS runs background refresh only when it decides to. Reliable alerts there need a push
from the server (APNs), which means adding a device-registration endpoint and a push sender to the
service; the app side is then a small change.

## Updates (`/download`)

The service serves the app at **`/download`** (a page with a Download button), `/download/pricewatch.apk`
and `/download/latest.json` (see `app/downloads.py`). Installed apps check `latest.json` when opened and
about hourly, and the background check announces a new version once. **Settings → Install update**
downloads the file, checks its SHA-256 against `latest.json`, and hands it to Android's installer. The
first time, Android asks to allow installs from Price Watch.

To release a new version:

```bash
mobile/scripts/publish.sh --bump --notes "What changed"            # to ../data/downloads (development)
mobile/scripts/publish.sh --bump --to root@tower:/mnt/user/appdata/retail-monitor/downloads   # Unraid
```

It raises the version, builds and signs the release, verifies the signature and writes
`pricewatch.apk` and `latest.json`. Every release needs a higher `versionCode` than the last
(`version.properties`), or phones will not treat it as an update.

**The signing key.** The first run creates `mobile/pricewatch-release.jks` and `keystore.properties`
(both git-ignored). Android installs an update only over an app signed with the same key, so **back these
two files up**: if they are lost, every installed app has to be uninstalled before a new build can be
installed. Google Play Protect may show "App scan recommended" for a sideloaded app on phones that have it;
"Install without scanning" is fine for your own build.

## Design

Clean and quiet: a soft green-grey background, white rounded cards, one green accent, green for cheaper
and orange for dearer, tabular digits for prices, light and dark themes that follow the phone. Prices show
as `19,95 kr`. Unknown is shown as a dash, never as zero.

## Not done yet

Percentage discounts ("20 % rabatt") are plain offers and are not compared with the alert price, nor are
member offers; Compare ignores multi-buys; release builds are not shrunk (R8) yet; iPhone push
(see Notifications).

#!/usr/bin/env bash
# Build a signed release of the Android app and publish it, so the server offers it at /download
# and installed apps find the update (Settings shows "Update available").
#
#   scripts/publish.sh [--bump | --version 1.2.0] [--notes "What changed"] [--to DEST] [--no-build]
#
#   --bump           raise versionCode by one (and the patch number of versionName)
#   --version X.Y.Z  set versionName (versionCode is raised by one as well)
#   --notes TEXT     shown on the download page and in the app's update card
#   --to DEST        where to publish:
#                      https://server   upload to the running service (needs APP_UPLOAD_TOKEN, below)
#                      a folder         ../data/downloads is the development stack's
#                      user@host:/path  copy over ssh
#                    Default: PW_SERVER from mobile/.publish.env when it is set, else ../data/downloads.
#   --no-build       publish the release APK that was built last
#
# Publishing over HTTPS (the usual way): put the server's address and its upload token in the
# git-ignored mobile/.publish.env (environment variables of the same names win):
#     PW_SERVER=https://price-watch.h03.eu
#     APP_UPLOAD_TOKEN=<the value of APP_UPLOAD_TOKEN in the server's .env>
# The server refuses a wrong token, a file that is not an APK, and a build older than the one on offer.
# A reverse proxy in front of it must allow uploads of about 50 MB (Nginx Proxy Manager:
# `client_max_body_size 100m;` under the host's Advanced tab).
#
# Every release must have a higher versionCode than the one before, or installed apps will not see it
# as an update. The first run creates the signing key (keystore.properties and pricewatch-release.jks,
# both git-ignored). Updates install only over an app signed with the same key: back that key up.
set -euo pipefail
cd "$(dirname "$0")/.."

# Optional local settings (git-ignored). Values already in the environment win.
if [[ -f .publish.env ]]; then
    while IFS='=' read -r key value; do
        case "$key" in PW_SERVER|APP_UPLOAD_TOKEN) [[ -n ${!key:-} ]] || export "$key=$value" ;; esac
    done < .publish.env
fi

dest=""; notes=""; bump=""; version=""; build=1
while [[ $# -gt 0 ]]; do
    case $1 in
        --bump) bump=1 ;;
        --version) version=${2:?--version needs a value}; shift ;;
        --notes) notes=${2:?--notes needs text}; shift ;;
        --to) dest=${2:?--to needs a destination}; shift ;;
        --no-build) build="" ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done

dest=${dest:-${PW_SERVER:-../data/downloads}}

prop() { sed -n "s/^$1=//p" version.properties; }
code=$(prop versionCode); name=$(prop versionName)
if [[ -n $version || -n $bump ]]; then
    code=$((code + 1))
    if [[ -n $version ]]; then name=$version
    else name="${name%.*}.$(( ${name##*.} + 1 ))"; fi
    printf 'versionCode=%s\nversionName=%s\n' "$code" "$name" > version.properties
    echo "Version is now $name ($code)"
fi

# The signing key, created once.
if [[ ! -f keystore.properties ]]; then
    command -v keytool >/dev/null || { echo "keytool not found: install a JDK" >&2; exit 1; }
    pass=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)
    keytool -genkeypair -keystore pricewatch-release.jks -alias pricewatch -keyalg RSA -keysize 4096 \
        -validity 36500 -storepass "$pass" -keypass "$pass" -dname "CN=Price Watch" >/dev/null 2>&1
    printf 'storeFile=pricewatch-release.jks\nstorePassword=%s\nkeyAlias=pricewatch\nkeyPassword=%s\n' "$pass" "$pass" > keystore.properties
    chmod 600 keystore.properties pricewatch-release.jks
    echo "Created the signing key: mobile/pricewatch-release.jks (+ keystore.properties)."
    echo "BACK THESE TWO FILES UP. Without them, installed apps can never be updated."
fi

apk=androidApp/build/outputs/apk/release/androidApp-release.apk
if [[ -n $build ]]; then ./gradlew :androidApp:assembleRelease --console=plain -q; fi
[[ -f $apk ]] || { echo "No release APK at $apk" >&2; exit 1; }

# It must be signed, or phones refuse it.
sdk=$(sed -n 's/^sdk\.dir=//p' local.properties 2>/dev/null || echo "${ANDROID_HOME:-$HOME/Android/Sdk}")
apksigner=$(ls -d "$sdk"/build-tools/*/apksigner 2>/dev/null | sort -V | tail -1 || true)
if [[ -n $apksigner ]]; then "$apksigner" verify "$apk" || { echo "The APK is not properly signed" >&2; exit 1; }; fi

json=$(python3 - "$code" "$name" "$notes" <<'PY'
import json, sys, datetime
print(json.dumps({"version_code": int(sys.argv[1]), "version_name": sys.argv[2], "notes": sys.argv[3],
                  "published_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, indent=2))
PY
)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
cp "$apk" "$tmp/pricewatch.apk"; printf '%s\n' "$json" > "$tmp/latest.json"

if [[ $dest == http://* || $dest == https://* ]]; then
    [[ -n ${APP_UPLOAD_TOKEN:-} ]] || { echo "Set APP_UPLOAD_TOKEN in mobile/.publish.env to the token in the server .env" >&2; exit 2; }
    server=${dest%/}
    echo "Uploading $(du -h "$apk" | cut -f1) to $server as $name ($code)"
    # the notes travel percent-encoded, so any text (and non-ASCII) survives a header
    encoded=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$notes")
    curl --fail-with-body --silent --show-error --max-time 900 -X PUT "$server/download/pricewatch.apk" \
        -H "Authorization: Bearer $APP_UPLOAD_TOKEN" -H "X-Version-Name: $name" -H "X-Version-Code: $code" \
        -H "X-Notes: $encoded" -H "Content-Type: application/vnd.android.package-archive" --data-binary @"$apk"
    echo
    live=$(curl --silent --show-error --max-time 30 "$server/download/latest.json" | python3 -c 'import sys, json; print(json.load(sys.stdin)["version_name"])')
    [[ $live == "$name" ]] || { echo "The server offers $live, not $name" >&2; exit 1; }
    echo "Published Price Watch $name ($code). Phones get it at $server/download; installed apps offer it in Settings."
    exit 0
fi

if [[ $dest == *:* ]]; then
    host=${dest%%:*}; path=${dest#*:}
    scp -q "$tmp/pricewatch.apk" "$host:$path/pricewatch.apk.new"
    scp -q "$tmp/latest.json" "$host:$path/latest.json.new"
    ssh "$host" "cd '$path' && mv pricewatch.apk.new pricewatch.apk && mv latest.json.new latest.json"
else
    mkdir -p "$dest"
    cp "$tmp/pricewatch.apk" "$dest/pricewatch.apk.new"; cp "$tmp/latest.json" "$dest/latest.json.new"
    # the APK first, then the manifest: the server never announces a file that is not there yet
    mv "$dest/pricewatch.apk.new" "$dest/pricewatch.apk"; mv "$dest/latest.json.new" "$dest/latest.json"
fi
echo "Published Price Watch $name ($code), $(du -h "$apk" | cut -f1): $dest"
echo "People get it at <server>/download; installed apps offer it in Settings."

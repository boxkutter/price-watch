"""The Android app, offered by the service itself at /download so people can install it and update it.

A release is two files in one folder (APP_DOWNLOADS_DIR): `pricewatch.apk` and `latest.json`,
`{"version_code": 3, "version_name": "0.3.0", "notes": "...", "published_at": "..."}`. The service
never builds or signs anything: it only serves what was published, and works out the size and
checksum itself so they cannot be wrong. The pages are public: installing has to work before anyone
has signed in, and the app holds no secrets (the same is true of /demo).

Publishing is not public. With APP_UPLOAD_TOKEN set (24+ characters), the developer's machine
uploads a release with `PUT /download/pricewatch.apk` (mobile/scripts/publish.sh does it); the file
must be an APK, not over 150 MB, and not an older build than the one on offer.
"""
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import zipfile
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote

import anyio

APK_NAME = "pricewatch.apk"
MANIFEST_NAME = "latest.json"
APK_URL = "/download/" + APK_NAME


@lru_cache(maxsize=4)
def _sha256(path: str, mtime_ns: int, size: int) -> str:
    """Hash of the file; remembered per (path, modified time, size) so a request does not re-read 20 MB."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def latest_release(directory: str | Path) -> dict | None:
    """The published release, or None when nothing (valid) has been published."""
    folder = Path(directory)
    apk, manifest = folder / APK_NAME, folder / MANIFEST_NAME
    if not (apk.is_file() and manifest.is_file()):
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    code, name = data.get("version_code") if isinstance(data, dict) else None, data.get("version_name") if isinstance(data, dict) else None
    if not isinstance(code, int) or isinstance(code, bool) or not isinstance(name, str) or not name:
        return None
    stat = apk.stat()
    return {"version_code": code, "version_name": name, "notes": str(data.get("notes") or ""),
            "published_at": data.get("published_at"), "size_bytes": stat.st_size,
            "sha256": _sha256(str(apk), stat.st_mtime_ns, stat.st_size), "url": APK_URL}


def apk_filename(release: dict) -> str:
    safe = "".join(c for c in release["version_name"] if c.isalnum() or c in ".-_") or str(release["version_code"])
    return f"price-watch-{safe}.apk"


def download_page(release: dict | None) -> str:
    """A small page that works on a phone: what the latest version is and a button to get it."""
    if release is None:
        body = ("<h1>Price Watch for Android</h1>"
                "<p class=muted>No app has been published on this server yet.</p>")
    else:
        megabytes = release["size_bytes"] / 1_000_000
        notes = (f"<h2>What is new</h2><p class=notes>{html.escape(release['notes'])}</p>"
                 if release["notes"] else "")
        body = f"""<h1>Price Watch for Android</h1>
<p class=muted>Version {html.escape(release['version_name'])} · {megabytes:.1f} MB</p>
<p><a class=button href="{APK_URL}">Download the app</a></p>
{notes}
<h2>Installing</h2>
<ol>
<li>Open the downloaded file. Android may ask to allow installs from this browser: allow it for this one install.</li>
<li>The first time, open <b>Settings</b> in the app and connect to this server.</li>
<li>Later updates: the app tells you when a new version is here, and installs it from <b>Settings</b>.</li>
</ol>
<p class=muted>SHA-256 <code>{release['sha256']}</code></p>"""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Price Watch for Android</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;max-width:34rem;margin:0 auto;padding:2rem 1.25rem;color:#16211d;background:#f6f8f7}}
h1{{font-size:1.6rem;margin:0 0 .25rem}} h2{{font-size:1.05rem;margin:1.8rem 0 .4rem}}
.muted{{color:#5d6b66}} .notes{{white-space:pre-wrap}} code{{font-size:.72rem;word-break:break-all}}
.button{{display:inline-block;background:#0b6e4f;color:#fff;text-decoration:none;padding:.85rem 1.6rem;border-radius:999px;font-weight:600}}
@media (prefers-color-scheme:dark){{body{{background:#0e1412;color:#e6eeea}}.muted{{color:#98a8a1}}}}
</style></head><body>{body}</body></html>"""


# ---- publishing -----------------------------------------------------------------------------------------

MAX_APK_BYTES = 150_000_000
MIN_TOKEN_LENGTH = 24
_VERSION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,39}")


class UploadRefused(Exception):
    """An upload that must not be accepted, with the status to answer and words fit to show."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def uploads_enabled(token: str) -> bool:
    return len(token) >= MIN_TOKEN_LENGTH


def check_token(token: str, authorization: str) -> None:
    """Refuses unless the caller sent the configured token (compared in constant time)."""
    if not uploads_enabled(token):
        raise UploadRefused(404, "Uploading is not switched on for this server")
    if not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        raise UploadRefused(401, "Wrong or missing upload token")


def check_version(directory: str | Path, version_code: str, version_name: str) -> tuple[int, str]:
    """The version being uploaded, valid and not older than what is on offer (phones only update to a higher build)."""
    if not version_code.isdigit() or not 0 < int(version_code) < 10**9:
        raise UploadRefused(422, "X-Version-Code must be a whole number above zero")
    if not _VERSION_NAME.fullmatch(version_name):
        raise UploadRefused(422, "X-Version-Name must be a short version such as 0.3.0")
    current = latest_release(directory)
    if current and int(version_code) < current["version_code"]:
        raise UploadRefused(409, f"Build {version_code} is older than the build on offer ({current['version_code']}); "
                                 "raise the version code")
    return int(version_code), version_name


def check_is_apk(path: Path) -> None:
    if not zipfile.is_zipfile(path):
        raise UploadRefused(422, "That is not an APK")
    with zipfile.ZipFile(path) as archive:
        if "AndroidManifest.xml" not in archive.namelist():
            raise UploadRefused(422, "That is not an APK (no AndroidManifest.xml)")


async def receive_apk(directory: str | Path, stream, declared_length: int | None) -> Path:
    """Writes the request body to a temporary file beside the release, refusing anything too big or not an APK."""
    folder = Path(directory)
    if declared_length is not None and declared_length > MAX_APK_BYTES:
        raise UploadRefused(413, f"The file is over {MAX_APK_BYTES // 1_000_000} MB")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        temp = folder / f".upload-{secrets.token_hex(6)}.apk"
        size = 0
        async with await anyio.open_file(temp, "wb") as out:
            async for chunk in stream:
                size += len(chunk)
                if size > MAX_APK_BYTES:
                    raise UploadRefused(413, f"The file is over {MAX_APK_BYTES // 1_000_000} MB")
                await out.write(chunk)
    except UploadRefused:
        temp.unlink(missing_ok=True)
        raise
    except OSError as exc:
        raise UploadRefused(503, "The downloads folder cannot be written to") from exc
    try:
        check_is_apk(temp)
    except UploadRefused:
        temp.unlink(missing_ok=True)
        raise
    return temp


def publish(directory: str | Path, apk: Path, version_code: int, version_name: str, notes: str) -> dict:
    """Puts a received APK on offer: the file first, then the manifest, each replaced in one step so the
    page never announces a file that is not there (or a half-written one)."""
    folder = Path(directory)
    manifest = {"version_code": version_code, "version_name": version_name, "notes": unquote(notes)[:2000],
                "published_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        os.replace(apk, folder / APK_NAME)
        pending = folder / f".latest-{secrets.token_hex(6)}.json"
        pending.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.replace(pending, folder / MANIFEST_NAME)
    except OSError as exc:
        raise UploadRefused(503, "The downloads folder cannot be written to") from exc
    return latest_release(folder) or manifest

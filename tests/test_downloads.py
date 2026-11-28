"""The Android app is served at /download so people can install it and the app can find updates."""
import hashlib
import io
import json
import os
import zipfile
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app import downloads
from app.config import settings
from app.downloads import latest_release
from app.main import app

APK = b"PK\x03\x04 pretend this is an apk"


@pytest.fixture
def published(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_downloads_dir", str(tmp_path))
    (tmp_path / "pricewatch.apk").write_bytes(APK)
    (tmp_path / "latest.json").write_text(json.dumps(
        {"version_code": 7, "version_name": "0.7.0", "notes": "Faster <lists>", "published_at": "2026-11-28T10:00:00Z"}))
    return tmp_path


def test_nothing_published_says_so_and_offers_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_downloads_dir", str(tmp_path))
    client = TestClient(app)
    page = client.get("/download")
    assert page.status_code == 200 and "No app has been published" in page.text
    assert client.get("/download/latest.json").status_code == 404
    assert client.get("/download/pricewatch.apk").status_code == 404


def test_the_latest_release_is_described_for_the_app_and_needs_no_login(published):
    client = TestClient(app)
    body = client.get("/download/latest.json").json()
    assert body == {"version_code": 7, "version_name": "0.7.0", "notes": "Faster <lists>",
                    "published_at": "2026-11-28T10:00:00Z", "size_bytes": len(APK),
                    "sha256": hashlib.sha256(APK).hexdigest(), "url": "/download/pricewatch.apk"}
    assert client.get("/download/latest.json").headers["cache-control"] == "no-store"


def test_the_apk_is_served_as_an_android_package_with_a_helpful_name(published):
    response = TestClient(app).get("/download/pricewatch.apk")
    assert response.status_code == 200 and response.content == APK
    assert response.headers["content-type"] == "application/vnd.android.package-archive"
    assert 'filename="price-watch-0.7.0.apk"' in response.headers["content-disposition"]


def test_download_managers_can_ask_with_head_first(published):
    client = TestClient(app)
    for path in ("/download", "/download/latest.json", "/download/pricewatch.apk"):
        assert client.head(path).status_code == 200, path


def test_the_page_shows_the_version_and_escapes_the_notes(published):
    page = TestClient(app).get("/download").text
    assert "Version 0.7.0" in page and 'href="/download/pricewatch.apk"' in page
    assert "Faster &lt;lists&gt;" in page and "<lists>" not in page
    assert hashlib.sha256(APK).hexdigest() in page


def test_a_new_publish_replaces_the_old_one_even_for_the_same_size(published):
    client = TestClient(app)
    first = client.get("/download/latest.json").json()["sha256"]
    (published / "pricewatch.apk").write_bytes(APK[::-1])
    assert client.get("/download/latest.json").json()["sha256"] != first


@pytest.mark.parametrize("manifest", ["not json", "[]", '{"version_name": "1"}', '{"version_code": "3", "version_name": "x"}',
                                      '{"version_code": true, "version_name": "x"}', '{"version_code": 1, "version_name": ""}'])
def test_a_damaged_manifest_means_nothing_is_published(tmp_path, manifest):
    (tmp_path / "pricewatch.apk").write_bytes(APK)
    (tmp_path / "latest.json").write_text(manifest)
    assert latest_release(tmp_path) is None


# ---- publishing from the developer's machine ----------------------------------------------------------------



TOKEN = "t" * 32


def make_apk(extra: bytes = b"") -> bytes:
    """A minimal APK: a zip with an AndroidManifest.xml."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"manifest" + extra)
        archive.writestr("classes.dex", b"dex")
    return out.getvalue()


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_downloads_dir", str(tmp_path))
    monkeypatch.setattr(settings, "app_upload_token", TOKEN)
    return tmp_path


def put(body: bytes | None = None, token: str = TOKEN, code: str = "5", name: str = "0.3.0", notes: str = "", **extra):
    headers = {"Authorization": f"Bearer {token}", "X-Version-Code": code, "X-Version-Name": name, "X-Notes": notes, **extra}
    return TestClient(app).put("/download/pricewatch.apk", content=make_apk() if body is None else body, headers=headers)


def test_uploading_is_off_until_a_token_is_set(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_downloads_dir", str(tmp_path))
    for token in ("", "too-short"):
        monkeypatch.setattr(settings, "app_upload_token", token)
        assert put(token=token or "x" * 32).status_code == 404
    assert not (tmp_path / "pricewatch.apk").exists()


def test_a_release_uploaded_with_the_token_is_put_on_offer(uploads):
    notes = "Member prices & multi-buys: the cheapest deal for you"
    response = put(notes=quote(notes), name="0.3.0", code="5")
    assert response.status_code == 201
    body = response.json()
    assert (body["version_name"], body["version_code"], body["notes"]) == ("0.3.0", 5, notes)
    assert body["size_bytes"] == len(make_apk()) and len(body["sha256"]) == 64
    client = TestClient(app)
    assert client.get("/download/latest.json").json()["version_code"] == 5
    assert client.get("/download/pricewatch.apk").content == make_apk()
    assert "Version 0.3.0" in client.get("/download").text
    assert [p.name for p in uploads.iterdir()] == ["latest.json", "pricewatch.apk"]      # nothing left behind


@pytest.mark.parametrize("token", ["", "wrong" * 8, TOKEN + "x", TOKEN[:-1]])
def test_a_wrong_or_missing_token_is_refused_and_nothing_changes(uploads, token):
    assert put(token=token).status_code == 401
    assert list(uploads.iterdir()) == []


def test_only_an_apk_is_accepted(uploads):
    assert put(b"this is not a zip").status_code == 422
    not_an_apk = io.BytesIO()
    with zipfile.ZipFile(not_an_apk, "w") as archive:
        archive.writestr("readme.txt", b"hello")
    assert put(not_an_apk.getvalue()).status_code == 422
    assert list(uploads.iterdir()) == []                                                 # the temporary file is removed


def test_a_file_over_the_limit_is_refused(uploads, monkeypatch):
    monkeypatch.setattr(downloads, "MAX_APK_BYTES", 100)
    assert put(make_apk(b"x" * 500)).status_code == 413
    assert list(uploads.iterdir()) == []


@pytest.mark.parametrize("code,name", [("", "0.3.0"), ("abc", "0.3.0"), ("0", "0.3.0"), ("-1", "0.3.0"),
                                       ("5", ""), ("5", "../evil"), ("5", "a" * 41)])
def test_the_version_must_be_stated_plainly(uploads, code, name):
    assert put(code=code, name=name).status_code == 422


def test_an_older_build_never_replaces_a_newer_one_but_the_same_build_may_be_sent_again(uploads):
    assert put(code="5").status_code == 201
    older = put(make_apk(b"older"), code="4", name="0.2.9")
    assert older.status_code == 409 and "older" in older.json()["message"]
    assert TestClient(app).get("/download/latest.json").json()["version_code"] == 5
    assert TestClient(app).get("/download/pricewatch.apk").content == make_apk()        # the file is unchanged
    assert put(make_apk(b"again"), code="5").status_code == 201                          # idempotent re-publish
    assert put(make_apk(b"newer"), code="6", name="0.3.1").status_code == 201


def test_a_downloads_folder_that_cannot_be_written_is_reported_not_a_crash(uploads, monkeypatch):
    uploads.chmod(0o500)
    try:
        if (uploads / "probe").parent.stat().st_mode & 0o200 == 0 and os.access(uploads, os.W_OK):
            pytest.skip("running as a user that ignores permissions")
        assert put().status_code == 503
    finally:
        uploads.chmod(0o700)


def test_the_token_is_not_the_only_thing_in_the_way_of_reading(uploads):
    # downloading stays public and works without any token
    put()
    assert TestClient(app).get("/download/latest.json").status_code == 200

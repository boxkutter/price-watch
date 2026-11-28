"""The Android app is served at /download so people can install it and the app can find updates."""
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

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

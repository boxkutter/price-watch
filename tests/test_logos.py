"""Retailer logos: an administrator uploads one; everyone sees it; without one a badge is drawn."""
import io

from fastapi.testclient import TestClient
from PIL import Image

from app.main import app
from tests.test_regressions import admin_client


def logo(width=400, height=200, colour=(220, 0, 20, 128)) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (width, height), colour).save(out, "PNG")
    return out.getvalue()


def listing(client):
    return {c["id"]: c for c in client.get("/v1/connectors").json()["items"]}


def test_an_admin_uploads_a_logo_that_everyone_can_see(fresh_db):
    admin = admin_client()
    assert listing(admin)["se.citygross"]["logo_url"] is None
    up = admin.put("/v1/admin/connectors/se.citygross/logo", content=logo())
    assert up.status_code == 200
    url = up.json()["logo_url"]
    assert url.startswith("/logos/se.citygross?v=") and listing(admin)["se.citygross"]["logo_url"] == url
    assert listing(admin)["se.coop"]["logo_url"] is None

    served = TestClient(app).get(url)                       # no login: an <img> tag sends none
    assert served.status_code == 200 and served.headers["content-type"] == "image/webp"
    picture = Image.open(io.BytesIO(served.content))
    assert picture.size == (160, 80) and picture.mode == "RGBA"
    assert picture.getpixel((5, 5))[3] < 255                # the transparency was kept

    assert admin.delete("/v1/admin/connectors/se.citygross/logo").status_code == 204
    assert TestClient(app).get("/logos/se.citygross").status_code == 404
    assert listing(admin)["se.citygross"]["logo_url"] is None


def test_only_an_admin_can_set_a_logo_and_it_must_be_a_picture_for_a_known_retailer(fresh_db):
    admin = admin_client()
    assert TestClient(app).put("/v1/admin/connectors/se.coop/logo", content=logo()).status_code == 401
    assert admin.put("/v1/admin/connectors/se.coop/logo", content=b"not a picture").status_code == 422
    assert admin.put("/v1/admin/connectors/ex.nothing/logo", content=logo()).status_code == 404


# ---- fetching a logo from the retailer's website -----------------------------------------------

import httpx
import pytest

from app import logos
from tests.test_politeness import job_of

HOME = """<html><head>
<link rel="icon" href="/favicon-32.png" sizes="32x32">
<link rel="icon" type="image/svg+xml" href="/icon.svg">
<link rel="apple-touch-icon" href="https://static.citygross.se/touch-180.png" sizes="180x180">
<link rel="icon" href="https://evil.example/logo.png" sizes="512x512">
</head><body>hi</body></html>"""


def png(side) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (side, side), (200, 20, 20)).save(out, "PNG")
    return out.getvalue()


class FakeWebsite:
    def __init__(self, pages=None):
        self.requests: list[str] = []
        self.pages = pages if pages is not None else {
            "https://www.citygross.se/": HOME.encode(),
            "https://www.citygross.se/favicon-32.png": png(32),
            "https://static.citygross.se/touch-180.png": png(180)}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        body = self.pages.get(str(request.url))
        return httpx.Response(200 if body is not None else 404, content=body or b"")


@pytest.fixture
def website(monkeypatch, cg):
    site = FakeWebsite()
    monkeypatch.setattr(logos, "_transport", httpx.MockTransport(site))
    monkeypatch.setattr(logos, "MIN_INTERVAL", 0.0)
    from types import SimpleNamespace
    site.cg = SimpleNamespace(client=cg.client)
    site.admin = cg.client
    return site


def fetch(site, retailer="se.citygross"):
    return job_of(site.cg, site.admin.post(f"/v1/admin/connectors/{retailer}/logo/fetch"))


def test_the_logo_is_fetched_from_the_retailers_own_website_and_the_best_icon_is_kept(website):
    done = fetch(website)
    assert done["state"] == "succeeded"
    assert done["result"]["source_url"] == "https://static.citygross.se/touch-180.png"
    assert (done["result"]["width"], done["result"]["height"]) == (180, 180)
    assert not done["result"]["small"]
    assert website.requests == ["https://www.citygross.se/", "https://static.citygross.se/touch-180.png"]
    assert "evil.example" not in " ".join(website.requests)        # never anywhere but its own site
    url = next(c["logo_url"] for c in website.admin.get("/v1/connectors").json()["items"]
               if c["id"] == "se.citygross")
    shown = TestClient(app).get(url)
    assert shown.status_code == 200 and Image.open(io.BytesIO(shown.content)).size == (160, 160)


def test_a_small_favicon_is_used_when_nothing_bigger_exists_and_flagged(website):
    website.pages["https://www.citygross.se/"] = b'<link rel="icon" href="/favicon-32.png" sizes="32x32">'
    done = fetch(website)
    assert done["state"] == "succeeded" and done["result"]["small"] is True
    assert done["result"]["source_url"] == "https://www.citygross.se/favicon-32.png"


def test_no_usable_icon_says_so_and_changes_nothing(website):
    website.pages = {"https://www.citygross.se/": b'<link rel="icon" type="image/svg+xml" href="/i.svg">'}
    done = fetch(website)
    assert done["state"] == "not_found" and "Upload a logo" in done["result"]["detail"]
    assert next(c["logo_url"] for c in website.admin.get("/v1/connectors").json()["items"]
                if c["id"] == "se.citygross") is None


def test_the_fetch_is_a_gated_worker_job(website, monkeypatch):
    assert website.admin.post("/v1/admin/connectors/ex.nothing/logo/fetch").status_code == 404
    first = website.admin.post("/v1/admin/connectors/se.citygross/logo/fetch")
    again = website.admin.post("/v1/admin/connectors/se.citygross/logo/fetch")
    assert first.status_code == again.status_code == 202 and first.json()["job_id"] == again.json()["job_id"]
    assert website.requests == []                                     # nothing happens until the worker runs
    monkeypatch.setattr(logos, "site_origin", lambda info: None)
    assert website.admin.post("/v1/admin/connectors/se.coop/logo/fetch").status_code == 422
    website.admin.post("/v1/admin/connectors/se.citygross/disable")
    assert website.admin.post("/v1/admin/connectors/se.citygross/logo/fetch").status_code == 404


def test_the_icons_the_page_names_are_ranked_touch_icons_first_and_svgs_skipped():
    ranked = logos.icon_candidates(HOME, "https://www.citygross.se/")
    urls = [u for _s, u in ranked]
    assert urls[0] == "https://evil.example/logo.png" or urls[0] == "https://static.citygross.se/touch-180.png"
    assert "https://www.citygross.se/icon.svg" not in urls
    assert urls[-1] == "https://www.citygross.se/favicon.ico"
    assert logos.site_origin({"product_urls": [{"example": "https://www.coop.se/handla/x/"}],
                              "allowed_hosts": ["external.api.coop.se"]}) == "https://www.coop.se/"
    assert logos.site_origin({"product_urls": [], "allowed_hosts": ["external.api.coop.se"]}) is None

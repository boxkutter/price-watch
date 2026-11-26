"""The reference database (Open Food Facts): lookups, our own pictures, mismatches, user photos."""
import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app import reference
from app.db import SessionLocal
from app.main import app
from app.models import Job, Product, ProductImage, ReferenceProduct
from tests.test_politeness import AXA, search


def picture(width=1600, height=900, colour=(180, 40, 40), fmt="JPEG") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(out, fmt)
    return out.getvalue()


class FakeOff:
    """Stands in for Open Food Facts: product records by code, and a picture."""

    def __init__(self, products: dict | None = None):
        self.products = products or {}
        self.calls: list[httpx.Request] = []
        self.status_override: int | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.status_override:
            return httpx.Response(self.status_override, headers={"Retry-After": "120"})
        if request.url.host == "images.openfoodfacts.org":
            return httpx.Response(200, content=picture(), headers={"content-type": "image/jpeg"})
        if request.url.host == "world.openfoodfacts.org":
            code = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
            if code in self.products:
                return httpx.Response(200, content=json.dumps(
                    {"code": code, "status": 1, "product": self.products[code]}).encode())
        return httpx.Response(404, content=b'{"status":0}')


@pytest.fixture
def off(monkeypatch):
    site = FakeOff({AXA: {"code": AXA, "product_name": "Havregryn", "product_name_sv": "Havregryn Fin",
                          "brands": "AXA", "quantity": "1,5 kg",
                          "image_front_url": "https://images.openfoodfacts.org/images/p/front.jpg",
                          "categories": "Cereals", "countries_tags": ["en:sweden"]}})
    monkeypatch.setattr(reference, "_transport", httpx.MockTransport(site))
    monkeypatch.setattr(reference, "_client", None)       # built afresh, on the fake network
    monkeypatch.setattr(reference, "_cooldown_until", 0.0)
    return site


def run_lookups() -> int:
    from app.worker import run_job
    with SessionLocal.begin() as db:
        queued = reference.schedule(db, limit=5)
    while run_job():
        pass
    return queued


def known_product(cg):
    """A product with a barcode, found through the fake City Gross."""
    item = search(cg, gtin=AXA)["result"]["items"][0]
    return item["product_id"]


def test_a_barcode_is_looked_up_once_and_the_picture_is_kept_as_our_own_reduced_copy(cg, off):
    pid = known_product(cg)
    assert run_lookups() == 1
    with SessionLocal() as db:
        ref = db.get(ReferenceProduct, "0" + AXA)
        assert ref.status == "found" and ref.name == "Havregryn Fin" and ref.brand == "AXA"
        assert ref.page_url.endswith(f"/product/{AXA}") and ref.details["site"] == "Open Food Facts"
        (image,) = db.scalars(select(ProductImage).where(ProductImage.product_id == pid)).all()
        assert image.source == "reference" and image.owner_id is None
        assert image.content_type == "image/webp" and max(image.width, image.height) == 512
        assert "Open Food Facts" in image.credit and len(image.data) < 5_000
    assert [r.url.host for r in off.calls] == ["world.openfoodfacts.org", "images.openfoodfacts.org"]
    assert off.calls[0].headers["user-agent"] == "retail-monitor-tests"
    assert run_lookups() == 0                      # known now: not asked again until it is due


def test_an_unknown_barcode_is_remembered_and_not_asked_again(cg, off):
    off.products.clear()
    known_product(cg)
    assert run_lookups() == 1
    with SessionLocal() as db:
        ref = db.get(ReferenceProduct, "0" + AXA)
        assert ref.status == "not_found" and db.scalars(select(ProductImage)).all() == []
    asked = len(off.calls)
    assert asked == 3 and run_lookups() == 0       # all three reference sites said no, once


def test_search_results_carry_the_picture_and_say_where_the_reference_disagrees(cg, off):
    off.products[AXA]["quantity"] = "500 g"
    off.products[AXA]["brands"] = "Garant"
    known_product(cg)
    run_lookups()
    item = search(cg, gtin=AXA)["result"]["items"][0]
    assert item["image"]["source"] == "reference" and item["image"]["url"].startswith("/images/")
    assert item["image"]["credit"] == "Open Food Facts contributors, CC BY-SA"
    ref = item["reference"]
    assert ref["name"] == "Havregryn Fin" and ref["source"] == "Open Food Facts"
    assert {f["field"] for f in ref["flags"]} == {"size", "brand"}
    assert ref["flags"][0] == {"field": "size", "ours": "1.5 kg", "reference": "500 g"}


def test_matching_details_raise_no_flag(cg, off):
    known_product(cg)
    run_lookups()
    assert search(cg, gtin=AXA)["result"]["items"][0]["reference"]["flags"] == []


def test_the_picture_is_served_without_login_and_a_users_own_picture_replaces_it(cg, off):
    pid = known_product(cg)
    run_lookups()
    first = search(cg, gtin=AXA)["result"]["items"][0]["image"]
    anonymous = TestClient(app)                    # an <img> tag sends no token
    served = anonymous.get(first["url"])
    assert served.status_code == 200 and served.headers["content-type"] == "image/webp"
    assert "immutable" in served.headers["cache-control"]
    assert Image.open(io.BytesIO(served.content)).format == "WEBP"
    assert anonymous.get("/images/not-a-picture").status_code == 404

    mine = cg.client.put(f"/v1/products/{pid}/image", content=picture(300, 800, (10, 90, 200), "PNG"))
    assert mine.status_code == 200 and mine.json()["image"]["source"] == "user"
    assert mine.json()["image"]["url"] != first["url"]
    shown = search(cg, gtin=AXA)["result"]["items"][0]["image"]
    assert shown["source"] == "user" and shown["credit"] is None
    got = Image.open(io.BytesIO(anonymous.get(shown["url"]).content))
    assert got.size == (192, 512)                  # reduced, shape kept

    again = cg.client.put(f"/v1/products/{pid}/image", content=picture())   # replaces, not adds
    with SessionLocal() as db:
        assert len(db.scalars(select(ProductImage).where(ProductImage.source == "user")).all()) == 1
    assert again.status_code == 200
    assert cg.client.get(f"/v1/products/{pid}").json()["image"]["source"] == "user"
    assert cg.client.delete(f"/v1/products/{pid}/image").status_code == 204
    assert search(cg, gtin=AXA)["result"]["items"][0]["image"]["source"] == "reference"


def test_another_persons_picture_is_not_shown_to_you(cg, off):
    pid = known_product(cg)
    cg.client.put(f"/v1/products/{pid}/image", content=picture())
    with SessionLocal() as db:
        assert reference.best_image(db, pid, "someone-else") is None
        assert reference.best_image(db, pid, None) is None


def test_bad_or_oversized_uploads_are_refused(cg, off, monkeypatch):
    pid = known_product(cg)
    assert cg.client.put(f"/v1/products/{pid}/image", content=b"not a picture").status_code == 422
    assert cg.client.put("/v1/products/missing/image", content=picture()).status_code == 404
    monkeypatch.setattr("app.main.settings.max_image_upload_bytes", 1000)
    assert cg.client.put(f"/v1/products/{pid}/image", content=picture(2000, 2000)).status_code == 413


def test_a_rate_limit_pauses_lookups_and_nothing_is_recorded(cg, off):
    known_product(cg)
    off.status_override = 429
    assert run_lookups() == 1
    with SessionLocal() as db:
        assert db.scalars(select(ReferenceProduct)).all() == []
        assert db.scalars(select(Job).where(Job.kind == "reference_lookup")).one().state == "rate_limited"
        assert reference.schedule(db) == 0         # cooling down: ask for nothing


def test_lookups_are_capped_per_day_and_can_be_switched_off(cg, off, monkeypatch):
    known_product(cg)
    monkeypatch.setattr(reference.settings, "reference_enabled", False)
    with SessionLocal() as db:
        assert reference.schedule(db) == 0
    monkeypatch.setattr(reference.settings, "reference_enabled", True)
    monkeypatch.setattr(reference.settings, "reference_max_lookups_per_day", 0)
    with SessionLocal() as db:
        assert reference.schedule(db) == 0


def test_the_reference_never_changes_the_stores_barcode(cg, off):
    pid = known_product(cg)
    off.products[AXA]["code"] = "7310130000000"
    run_lookups()
    with SessionLocal() as db:
        product = db.get(Product, pid)
        assert product.gtin14 == "0" + AXA and product.gtin_source == "retailer"


def test_a_search_is_served_before_background_lookups_and_lookups_never_pile_up(cg, off):
    from app.worker import claim_job
    known_product(cg)
    with SessionLocal.begin() as db:
        assert reference.schedule(db, limit=2) == 1       # one barcode, one lookup queued
        assert reference.schedule(db, limit=2) == 0       # already waiting: nothing more is added
    queued = cg.client.post("/v1/search/jobs", json={"gtin": AXA, "store_ids": [cg.store_id]})
    assert queued.status_code == 202                      # created after the lookup, so it is behind it
    with SessionLocal.begin() as db:
        assert claim_job(db).kind == "search"             # ...but a person's search goes first

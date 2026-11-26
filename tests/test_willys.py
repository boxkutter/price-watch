"""Willys connector: parsing trimmed real responses, member offers, and how it fits the protocol."""
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app import retrieval
from app.connectors import registry
from app.connectors.base import ScanRequest
from app.connectors.contract import check_connector, client_for
from app.connectors.http import PoliteClient
from app.connectors.manifest import load_manifest
from app.connectors.registry import BUILTIN_DIR, CONNECTORS
from app.connectors.urls import match_product_url
from app.db import SessionLocal
from app.models import Monitor, Offer, Product, Store
from app.worker import run_job, schedule_due
from tests.connector_modules import module_of
from tests.test_regressions import admin_client

_module = module_of("se.willys")
WillysConnector, parse_product = _module.WillysConnector, _module.parse_product
FIXTURES = Path(__file__).parent / "fixtures"
MILK, COFFEE, OATS, APPLE = "101233933_ST", "101261204_ST", "101833307_ST", "100144504_KG"
MILK_EAN = "7340083443893"
LINK = "https://www.willys.se/produkt/Mellanmjolk-Langre-Hallbarhet-1-5procent-101233933_ST"


def load(name):
    return json.loads((FIXTURES / name).read_text(), parse_float=Decimal)


def search_items():
    return {i["code"]: i for i in load("willys_search.json")["results"]}


class FakeWillys:
    """Stands in for www.willys.se; records every request it receives."""

    def __init__(self, extra: int = 0):
        self.calls: list[httpx.Request] = []
        self.status = 200
        self.products = {MILK: load("willys_product_milk.json"), COFFEE: load("willys_product_coffee.json")}
        self.items = list(search_items().values())
        template = search_items()[OATS]
        self.items += [{**template, "code": f"{200000000 + n}_ST", "name": f"Havre {n}"} for n in range(extra)]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.status != 200:
            return httpx.Response(self.status)
        path = request.url.path
        if path == "/search":
            text = request.url.params["q"].casefold()
            hits = [i for i in self.items if text in i["name"].casefold()]
            page, size = int(request.url.params["page"]), int(request.url.params["size"])
            pages = max(1, -(-len(hits) // size))
            body = {"results": hits[page * size:(page + 1) * size] or None,
                    "pagination": {"currentPage": page, "numberOfPages": pages,
                                   "totalNumberOfResults": len(hits)}}
            return httpx.Response(200, content=json.dumps(body, default=float).encode())
        if path.startswith("/axfood/rest/p/"):
            product = self.products.get(path.rsplit("/", 1)[1])
            if product is None:
                return httpx.Response(404)
            return httpx.Response(200, content=json.dumps(product, default=float).encode())
        return httpx.Response(404)


def connector(site, max_requests=6):
    client = PoliteClient(["www.willys.se"], 0, transport=httpx.MockTransport(site))
    return WillysConnector({"max_requests_per_scan": max_requests}, client)


def scan(site_or_connector, request, store="online"):
    willys = site_or_connector if isinstance(site_or_connector, WillysConnector) else connector(site_or_connector)
    return willys.scan(store, "online", request)


# ---- parsing --------------------------------------------------------------------------------

def test_a_plain_product_has_the_shelf_price_unit_basis_and_availability():
    price, offers = parse_product(search_items()[OATS])
    assert (price.sku, price.name, price.current_price) == (OATS, "Havregryn Garant", Decimal("17.9"))
    assert (price.package_value, price.package_unit) == (Decimal("1.5"), "kg")
    assert price.availability == "in_stock" and price.regular_price is None and offers == []
    assert price.currency == "SEK" and price.channel == "online" and price.gtin is None  # search has no EAN


def test_the_product_detail_carries_the_barcode_that_search_lacks():
    price, _ = parse_product(load("willys_product_milk.json"))
    assert (price.sku, price.gtin, price.current_price) == (MILK, MILK_EAN, Decimal("16.9"))
    assert (price.package_value, price.package_unit) == (Decimal("1.5"), "L")


def test_items_sold_by_weight_are_priced_per_kilogram():
    price, _ = parse_product(search_items()[APPLE])
    assert (price.current_price, price.package_value, price.package_unit) == (Decimal("23.9"), 1, "kg")
    assert price.sku.endswith("_KG")  # "ca: 140g" is one piece, not the pricing basis


def test_a_member_multi_buy_is_an_offer_and_never_the_shelf_price():
    price, offers = parse_product(search_items()[COFFEE])
    assert price.current_price == Decimal("71.9") and price.regular_price is None
    (offer,) = offers
    assert offer.eligibility == "member" and offer.terms == "Klubbpris: 2 för 99 kr"
    assert offer.price == Decimal("49.5") and offer.skus == (COFFEE,)
    assert offer.identity_key == f"{COFFEE}:2500327409"
    assert offer.valid_to == datetime(2026, 11, 29, 22, 59, 59, tzinfo=UTC)


def test_a_public_single_item_campaign_lowers_the_price_and_keeps_the_regular_one():
    raw = json.loads(json.dumps(search_items()[OATS], default=float), parse_float=Decimal)
    raw["potentialPromotions"] = [{"code": "9", "campaignType": "ORDINARY", "qualifyingCount": 1,
                                   "price": {"value": 14.9}, "rewardLabel": "14,90"}]
    price, (offer,) = parse_product(raw)
    assert (price.current_price, price.regular_price) == (Decimal("14.9"), Decimal("17.9"))
    assert offer.eligibility == "public" and offer.terms == "14.9 kr"


@pytest.mark.parametrize("change", [
    lambda r: r.update(priceValue=None),
    lambda r: r.update(priceValue="free"),
    lambda r: r.update(online=False),
    lambda r: r.update(code="not-a-code"),
])
def test_an_unknown_price_or_unsellable_item_is_skipped_not_zero(change):
    raw = json.loads(json.dumps(search_items()[OATS], default=float), parse_float=Decimal)
    change(raw)
    assert parse_product(raw) is None


def test_an_unreadable_campaign_is_ignored_without_dropping_the_product():
    raw = json.loads(json.dumps(search_items()[OATS], default=float), parse_float=Decimal)
    raw["potentialPromotions"] = [{"code": None}, {"code": "7"}]   # no id; no price or percentage
    price, offers = parse_product(raw)
    assert price.current_price == Decimal("17.9") and offers == []


# ---- the connector --------------------------------------------------------------------------

def test_search_by_text_finds_products_and_makes_one_request():
    site = FakeWillys()
    snapshot = scan(site, ScanRequest(query="äpple"))
    assert [p.sku for p in snapshot.prices] == [APPLE, "100142221_KG"]
    assert snapshot.effective_status == "success" and snapshot.requests == 1
    assert site.calls[0].url.params["q"] == "äpple"


def test_search_pages_until_a_short_page_and_reports_truncation_at_the_cap():
    site = FakeWillys(extra=40)               # 41 matching "havre*": two pages of 30
    snapshot = scan(site, ScanRequest(query="havre"))
    assert len(snapshot.prices) == 41 and snapshot.requests == 2 and not snapshot.truncated
    site = FakeWillys(extra=100)              # more than the 60 the connector will read
    snapshot = scan(site, ScanRequest(query="havre"))
    assert len(snapshot.prices) == 60 and snapshot.truncated and snapshot.requests == 2


def test_a_lookup_by_id_returns_the_detail_with_its_barcode_and_reports_what_is_missing():
    site = FakeWillys()
    snapshot = scan(site, ScanRequest(skus=(MILK, "100000001_ST", "nonsense")))
    assert [(p.sku, p.gtin) for p in snapshot.prices] == [(MILK, MILK_EAN)]
    assert snapshot.missing == ("100000001_ST", "nonsense") and snapshot.effective_status == "partial"
    assert [c.url.path for c in site.calls] == [f"/axfood/rest/p/{MILK}", "/axfood/rest/p/100000001_ST"]  # no call for garbage


def test_a_barcode_request_is_not_found_here_and_costs_nothing():
    site = FakeWillys()
    snapshot = scan(site, ScanRequest(gtins=(MILK_EAN,)))
    assert snapshot.effective_status == "not_found" and snapshot.missing == (MILK_EAN,)
    assert site.calls == []   # anonymous search cannot find a barcode, so we do not even ask


def test_there_is_exactly_one_store_called_online():
    assert scan(FakeWillys(), ScanRequest(query="havre"), store="2149").effective_status == "invalid_data"


def test_willys_passes_the_contract_including_the_manifest_rules():
    manifest = load_manifest(BUILTIN_DIR / "se.willys" / "manifest.yaml")

    def make(transport, secrets):
        return WillysConnector(manifest.model_dump(), client_for(manifest, transport))

    passed = check_connector(make, FakeWillys(), manifest=manifest, store="online",
                             request=ScanRequest(query="havre"))
    assert {"identity", "targeted_only", "happy_path", "refusals", "product_urls"} <= set(passed)


def test_the_manifest_honours_robots_txt_spacing_and_window():
    info = registry.retailer("se.willys")
    manifest = load_manifest(BUILTIN_DIR / "se.willys" / "manifest.yaml")
    assert manifest.minimum_request_interval_seconds >= 10     # Crawl-delay: 10
    assert info["scheduled_window_utc"] == "04:00-08:45"        # Visit-time: 0400-0845
    assert info["allowed_hosts"] == ["www.willys.se"]          # nothing else, in particular not /sok pages' host variants
    assert info["stores"] == [{"external_id": "online", "name": "Willys (online)"}]


def test_product_page_links_are_read_and_never_fetched():
    rules = registry.retailer("se.willys")["product_urls"]
    assert match_product_url(rules, LINK) == MILK
    assert match_product_url(rules, "https://www.willys.se/produkt/Aepple-Royal-Gala-Klass-1-100144504_KG") == APPLE
    assert match_product_url(rules, "https://www.willys.se/sok?q=mjolk") is None
    assert match_product_url(rules, "https://www.willys.se/varukorg") is None
    assert match_product_url(rules, "https://example.com/produkt/x-101233933_ST") is None


# ---- inside the service ---------------------------------------------------------------------

@pytest.fixture
def willys(fresh_db, monkeypatch):
    retrieval.clear_cache()
    site = FakeWillys()
    monkeypatch.setitem(CONNECTORS, "se.willys", connector(site))
    client = admin_client()
    store = next(s for s in client.get("/v1/stores").json()["items"] if s["retailer_id"] == "se.willys")
    return SimpleNamespace(site=site, client=client, store_id=store["id"])


def finish(env, response):
    assert response.status_code in (200, 202), response.text
    assert run_job()
    return env.client.get(f"/v1/jobs/{response.json()['job_id']}").json()


def test_the_store_exists_without_discovery_and_is_not_duplicated(willys):
    from app import declared_stores
    with SessionLocal.begin() as db:
        assert declared_stores.seed(db) == 0                      # already there
        rows = db.scalars(select(Store).where(Store.retailer_id == "se.willys")).all()
    assert [(r.external_id, r.name) for r in rows] == [("online", "Willys (online)")]


def test_adding_by_link_records_the_barcode_the_retailer_publishes(willys):
    job = finish(willys, willys.client.post("/v1/monitors/from-url", json={"url": LINK, "store_id": willys.store_id}))
    assert job["state"] == "succeeded", job
    assert job["result"]["product"]["sku"] == MILK
    (monitor,) = willys.client.get("/v1/monitors").json()["items"]
    assert monitor["barcode"]["source"] == "retailer" and monitor["barcode"]["gtin14"] == "0" + MILK_EAN
    assert monitor["latest"]["current_price"] == "16.90"
    with SessionLocal() as db:
        assert db.scalar(select(Product).where(Product.gtin14 == "0" + MILK_EAN)).verified is True


def test_a_member_offer_is_stored_for_members_only(willys):
    job = finish(willys, willys.client.post("/v1/monitors/from-url", json={
        "url": "https://www.willys.se/produkt/Bryggkaffe-Mellanrost-101261204_ST", "store_id": willys.store_id,
        "notify_existing_offers": True}))
    assert job["state"] == "succeeded", job
    monitor_id = job["result"]["monitor_id"]
    assert finish(willys, willys.client.post(f"/v1/monitors/{monitor_id}/check"))["state"] == "succeeded"
    with SessionLocal() as db:
        offer = db.scalar(select(Offer))
        assert (offer.eligibility, offer.terms) == ("member", "Klubbpris: 2 för 99 kr")


def test_comparing_a_barcode_uses_the_listing_linked_to_it_because_willys_cannot_search_barcodes(willys):
    finish(willys, willys.client.post("/v1/monitors/from-url", json={"url": LINK, "store_id": willys.store_id}))
    willys.site.calls.clear()
    retrieval.clear_cache()                  # otherwise the add-by-link fetch answers from cache
    queued = willys.client.post("/v1/compare/barcode", json={"gtin": MILK_EAN, "store_ids": [willys.store_id]})
    row = queued.json()["known"][0]
    assert row["status"] == "found" and row["current_price"] == "16.90"     # known at once
    result = finish(willys, queued)["result"]
    assert result["rows"][0]["source"] == "live" and result["rows"][0]["current_price"] == "16.90"
    assert [c.url.path for c in willys.site.calls] == [f"/axfood/rest/p/{MILK}"]   # by id, never by barcode


# ---- scheduled window -----------------------------------------------------------------------

@pytest.mark.parametrize(("hour", "minute", "expected"), [
    (3, 59, False), (4, 0, True), (6, 30, True), (8, 45, True), (8, 46, False), (14, 23, False), (23, 59, False)])
def test_the_scheduled_window_is_open_only_between_the_stated_times(hour, minute, expected):
    now = datetime(2026, 11, 26, hour, minute, tzinfo=UTC)
    assert registry.in_scheduled_window("se.willys", now) is expected


def test_connectors_without_a_window_are_always_open_and_midnight_windows_wrap():
    assert registry.in_scheduled_window("se.coop", datetime(2026, 11, 26, 3, 0, tzinfo=UTC)) is True
    assert registry.in_scheduled_window("no.such", datetime(2026, 11, 26, 3, 0, tzinfo=UTC)) is True
    original = registry.retailer("test.demoshop")
    try:
        original["scheduled_window_utc"] = "22:00-02:00"
        assert registry.in_scheduled_window("test.demoshop", datetime(2026, 11, 26, 23, 0, tzinfo=UTC)) is True
        assert registry.in_scheduled_window("test.demoshop", datetime(2026, 11, 26, 1, 0, tzinfo=UTC)) is True
        assert registry.in_scheduled_window("test.demoshop", datetime(2026, 11, 26, 12, 0, tzinfo=UTC)) is False
    finally:
        original["scheduled_window_utc"] = None


def test_a_due_monitor_waits_for_the_window_and_a_manual_check_does_not(willys, monkeypatch):
    finish(willys, willys.client.post("/v1/monitors/from-url", json={"url": LINK, "store_id": willys.store_id}))
    with SessionLocal.begin() as db:
        monitor = db.scalar(select(Monitor))
        monitor.next_due_at = datetime(2020, 1, 1, tzinfo=UTC)
        monitor_id, before = monitor.id, monitor.next_due_at
    monkeypatch.setattr(registry, "in_scheduled_window", lambda retailer_id, now=None: False)
    assert schedule_due() == 0                                   # closed: nothing queued, still due
    with SessionLocal() as db:
        assert db.get(Monitor, monitor_id).next_due_at.replace(tzinfo=UTC) == before
    assert finish(willys, willys.client.post(f"/v1/monitors/{monitor_id}/check"))["state"] == "succeeded"
    monkeypatch.setattr(registry, "in_scheduled_window", lambda retailer_id, now=None: True)
    with SessionLocal.begin() as db:
        db.get(Monitor, monitor_id).next_due_at = before          # the manual check moved it on
    assert schedule_due() == 1                                   # open: it is picked up

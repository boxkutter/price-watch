"""Coop connector: parsing trimmed real responses, the key handling, and the polite behaviour."""
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import retrieval
from app.connectors.base import (
    AuthRequired,
    ConnectorError,
    ExtractionError,
    ScanRequest,
    TransientError,
)
from app.connectors.builtin.coop.connector import CoopConnector, parse_item, parse_package
from app.connectors.http import PoliteClient
from app.connectors.registry import CONNECTORS
from app.db import SessionLocal
from app.models import ConnectorState, Store
from app.worker import run_job
from tests.test_politeness import check, job_of, listing_id, monitor, search
from tests.test_regressions import admin_client

FIXTURES = Path(__file__).parent / "fixtures"
KEY = "k" * 32
HAVREGRYN, MJOLK, PEPSI, INTERNAL = "7300156599452", "7310865067883", "7310074715087", "2097825700008"


def items():
    data = json.loads((FIXTURES / "coop_search.json").read_text(), parse_float=Decimal)
    return {i["ean"]: i for i in data["results"]["items"]}


class FakeCoop:
    """Stands in for external.api.coop.se; records every request it receives."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.status = 200
        self.headers: dict = {}
        self.body = None
        self.items = list(items().values())

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.status != 200:
            return httpx.Response(self.status, headers=self.headers)
        if self.body is not None:
            return httpx.Response(200, content=self.body)
        path = request.url.path
        if path.endswith("/coopstore"):
            if request.url.params["postCode"] == "00000":
                return httpx.Response(400, json={"errors": [{"type": "StoreNotFoundError"}]})
            return httpx.Response(200, content=(FIXTURES / "coop_store.json").read_bytes())
        body = json.loads(request.content)
        if path.endswith("/entities/by-id"):
            hits = [i for i in self.items if i["ean"] in body]
        else:
            text = body["query"].casefold()
            hits = [i for i in self.items if text in i["name"].casefold()]
            skip, take = body["resultsOptions"]["skip"], body["resultsOptions"]["take"]
            return httpx.Response(200, content=json.dumps(
                {"results": {"count": len(hits), "items": hits[skip:skip + take]}},
                default=float).encode())
        return httpx.Response(200, content=json.dumps(
            {"results": {"count": len(hits), "items": hits}}, default=float).encode())


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("COOP_API_KEY", KEY)  # what the registry resolves for the end-to-end tests


def connector(site, max_requests=6, key=KEY):
    client = PoliteClient(["external.api.coop.se"], 0, transport=httpx.MockTransport(site))
    coop = CoopConnector({"max_requests_per_scan": max_requests}, client)
    if key:
        coop.configure({"COOP_API_KEY": key})  # unit tests bypass the registry
    return coop


# ---- parsing ---------------------------------------------------------------------------

def test_plain_product_uses_the_consumer_price_and_the_ean_as_sku_and_gtin():
    price = parse_item(items()[HAVREGRYN])
    assert (price.sku, price.gtin, price.current_price) == (HAVREGRYN, HAVREGRYN, Decimal("18.88"))
    assert price.name == "Havregryn Coop 1500 g" and price.brand == "Coop"
    assert (price.package_value, price.package_unit) == (Decimal(1500), "g")
    assert price.availability == "in_stock" and price.regular_price is None
    assert price.currency == "SEK" and price.channel == "online"


def test_size_comes_from_label_or_numbers_and_multipacks_are_totals():
    assert parse_package(items()[MJOLK]) == (Decimal(1000), "ml")        # "1 liter": numeric fallback
    assert parse_package(items()[PEPSI]) == (Decimal(6), "L")            # label "4x1.5L"
    assert parse_package(items()[INTERNAL]) is None                      # approximate weight: unknown
    raw = dict(items()[PEPSI], packageSizeInformation="4 pack stora", packageSize=1500.0)
    assert parse_package(raw) is None  # an unreadable multipack label never falls back to a unit size


def test_store_internal_ean_never_becomes_a_verified_global_barcode(fresh_db):
    """2-prefixed EANs are store-internal codes for loose items; matching on them would link
    unrelated products, so they stay provisional even though the check digit is valid."""
    from app.ingest import ensure_listing
    from app.models import Product

    internal, normal = parse_item(items()[INTERNAL]), parse_item(items()[HAVREGRYN])
    assert internal.gtin == INTERNAL  # the connector reports what the retailer published
    with SessionLocal.begin() as db:
        store = Store(retailer_id="se.coop", external_id="255700", name="s")
        db.add(store)
        db.flush()
        internal_listing = ensure_listing(db, store, internal)
        normal_listing = ensure_listing(db, store, normal)
        unmapped = db.get(Product, internal_listing.product_id)
        mapped = db.get(Product, normal_listing.product_id)
        assert (unmapped.gtin14, unmapped.verified) == (None, False)
        assert (mapped.gtin14, mapped.verified) == ("0" + HAVREGRYN, True)


@pytest.mark.parametrize("change", [
    lambda r: r.update(salesPriceData={"b2bPrice": 17.0}),
    lambda r: r.update(salesPriceData=None),
    lambda r: [r.pop("ean"), r.pop("id")],
])
def test_missing_consumer_price_is_unknown_not_zero_and_b2b_is_never_used(change):
    raw = json.loads(json.dumps(items()[HAVREGRYN], default=float))
    change(raw)
    assert parse_item(raw) is None


def test_not_orderable_online_is_unknown_not_out_of_stock():
    raw = dict(items()[HAVREGRYN], availableOnline=False)
    assert parse_item(raw).availability is None


# ---- requests --------------------------------------------------------------------------

def test_search_is_a_store_scoped_post_with_the_key_and_no_personalisation():
    site = FakeCoop()
    snapshot = connector(site).scan("255700", "online", ScanRequest(query="havregryn"))
    assert snapshot.effective_status == "success" and snapshot.targeted and snapshot.offers == []
    (call,) = site.calls
    assert call.method == "POST" and call.url.host == "external.api.coop.se"
    assert call.headers["ocp-apim-subscription-key"] == KEY
    assert call.headers["user-agent"].startswith("Mozilla/5.0")
    assert dict(call.url.params)["store"] == "255700"
    body = json.loads(call.content)
    assert body["query"] == "havregryn" and body["customData"] == {"consent": False}
    assert body["resultsOptions"]["take"] == 40 and body["resultsOptions"]["facets"] == []
    assert KEY not in json.dumps([snapshot.source, snapshot.detail])  # the key never leaks


def test_sku_and_barcode_lookups_share_one_by_id_request():
    site = FakeCoop()
    snapshot = connector(site).scan("255700", "online", ScanRequest(
        skus=(HAVREGRYN,), gtins=("7310865067883",)))
    assert sorted(p.sku for p in snapshot.prices) == [HAVREGRYN, MJOLK]
    (call,) = site.calls
    assert call.url.path.endswith("/entities/by-id")
    assert HAVREGRYN in json.loads(call.content) and MJOLK in json.loads(call.content)


def test_unknown_sku_is_partial_and_unknown_barcode_is_not_found_never_unavailable():
    site = FakeCoop()
    partial = connector(site).scan("255700", "online", ScanRequest(skus=(HAVREGRYN, "123")))
    assert partial.effective_status == "partial" and partial.missing == ("123",)
    nothing = connector(site).scan("255700", "online", ScanRequest(gtins=("4006381333931",)))
    assert nothing.effective_status == "not_found" and nothing.missing == ("4006381333931",)


def test_search_pages_until_a_short_page_and_caps_results():
    site = FakeCoop()
    template = site.items[0]
    site.items = [{**template, "ean": f"{i:013d}", "name": f"Havregryn {i}"} for i in range(200)]
    snapshot = connector(site).scan("255700", "online", ScanRequest(query="havregryn"))
    assert len(snapshot.prices) == 80 and len(site.calls) == 2 and snapshot.truncated
    site.items = site.items[:30]
    site.calls.clear()
    short = connector(site).scan("255700", "online", ScanRequest(query="havregryn"))
    assert len(short.prices) == 30 and len(site.calls) == 1 and not short.truncated


def test_without_a_key_nothing_is_requested_and_it_is_not_a_failure():
    site = FakeCoop()
    snapshot = connector(site, key=None).scan("255700", "online", ScanRequest(query="havregryn"))
    assert snapshot.effective_status == "auth_required" and site.calls == []
    with pytest.raises(AuthRequired):
        connector(site, key=None).discover_stores(postcode="11120")


def test_untargeted_physical_and_non_numeric_store_make_no_request():
    site = FakeCoop()
    coop = connector(site)
    assert coop.scan("255700", "online", ScanRequest()).effective_status == "unsupported"
    assert coop.scan("255700", "physical", ScanRequest(query="x")).effective_status == "unsupported"
    assert coop.scan("../x", "online", ScanRequest(query="x")).effective_status == "invalid_data"
    assert site.calls == []


@pytest.mark.parametrize("status,headers,expected", [
    (401, {}, "access_blocked"), (403, {}, "access_blocked"),
    (302, {"Location": "https://elsewhere.example/"}, "access_blocked"),
    (429, {"Retry-After": "90"}, "rate_limited"), (404, {}, "extraction_failed"),
])
def test_refusals_are_reported_once_and_never_retried(status, headers, expected):
    site = FakeCoop()
    site.status, site.headers = status, headers
    snapshot = connector(site).scan("255700", "online", ScanRequest(query="havregryn"))
    assert snapshot.effective_status == expected and len(site.calls) == 1
    assert KEY not in (snapshot.detail or "")


@pytest.mark.parametrize("body", [b"<html>", b'{"unexpected": 1}', b'{"results": {"items": "x"}}'])
def test_unexpected_responses_are_extraction_failures(body):
    site = FakeCoop()
    site.body = body
    snapshot = connector(site).scan("255700", "online", ScanRequest(query="havregryn"))
    assert snapshot.effective_status == "extraction_failed"


def test_server_errors_raise_for_a_backoff_retry():
    site = FakeCoop()
    site.status = 503
    with pytest.raises(TransientError):
        connector(site).scan("255700", "online", ScanRequest(query="havregryn"))


def test_request_budget_returns_partial():
    site = FakeCoop()
    many = tuple(f"{i:013d}" for i in range(60))  # 60 ids need 3 by-id batches
    snapshot = connector(site, max_requests=2).scan("255700", "online", ScanRequest(skus=many))
    assert len(site.calls) == 2 and snapshot.effective_status == "partial"
    assert "budget" in snapshot.detail


def test_only_the_api_host_is_ever_contacted():
    client = PoliteClient(["external.api.coop.se"], 0, transport=httpx.MockTransport(FakeCoop()))
    for url in ("https://www.coop.se/handla/sok/?q=x", "http://external.api.coop.se/x"):
        with pytest.raises(ConnectorError, match="Host not allowed"):
            client.get_json(url)


# ---- store resolution ------------------------------------------------------------------

def test_a_postcode_resolves_to_the_serving_store():
    site = FakeCoop()
    (store,) = connector(site).discover_stores(postcode="111 20")
    assert (store.external_id, store.name) == ("255700", "Stora Coop Häggvik")
    (call,) = site.calls
    assert dict(call.url.params)["postCode"] == "11120" and call.headers["ocp-apim-subscription-key"] == KEY


def test_bad_postcodes_are_clear_errors():
    coop = connector(FakeCoop())
    with pytest.raises(ExtractionError, match="five-digit"):
        coop.discover_stores(postcode="12")
    with pytest.raises(ExtractionError, match="No Coop store serves that postcode"):
        coop.discover_stores(postcode="00000")


# ---- end to end through the API and worker ----------------------------------------------

@pytest.fixture
def coop(fresh_db, monkeypatch):
    retrieval.clear_cache()
    site = FakeCoop()
    monkeypatch.setitem(CONNECTORS, "se.coop", connector(site))
    with SessionLocal.begin() as db:
        store = Store(retailer_id="se.coop", external_id="255700", name="Stora Coop Häggvik")
        db.add(store)
        db.flush()
        store_id = store.id
    return SimpleNamespace(site=site, client=admin_client(), store_id=store_id)


def test_barcode_search_records_a_verified_mapping_and_the_monitor_sees_a_price_drop(coop):
    result = search(coop, gtin=HAVREGRYN)
    assert result["result"]["barcode_mapping"] == "verified"
    (item,) = result["result"]["items"]
    assert item["current_price"] == "18.88"
    assert coop.client.get(f"/v1/products/barcode/{HAVREGRYN}").json()["id"] == item["product_id"]

    created = monitor(coop, listing_id=item["listing_id"])
    assert created.status_code == 201
    mid = created.json()["id"]
    assert check(coop, mid)["state"] == "succeeded"
    retrieval.clear_cache()
    next(i for i in coop.site.items if i["ean"] == HAVREGRYN)["salesPriceData"]["b2cPrice"] = 15.5
    assert check(coop, mid)["state"] == "succeeded"
    events = [e["type"] for e in coop.client.get("/v1/events").json()["items"]]
    assert "price.decreased" in events


def test_search_by_text_lists_products_and_creates_listings_per_store(coop):
    found = search(coop, query="havregryn")["result"]
    assert [i["name"] for i in found["items"]] == ["Havregryn Coop 1500 g"]
    assert listing_id(coop, "havregryn")


def test_a_missing_key_is_reported_without_pausing_or_counting_a_failure(coop, monkeypatch):
    monkeypatch.delenv("COOP_API_KEY")
    mid = monitor(coop, "generic_product", query="havregryn").json()["id"]
    assert check(coop, mid)["state"] == "auth_required" and coop.site.calls == []
    with SessionLocal() as db:
        state = db.get(ConnectorState, "se.coop")
        assert state is None or (state.state == "active" and state.consecutive_failures == 0)


def test_a_rotated_key_pauses_the_connector_until_resumed(coop):
    mid = monitor(coop, "generic_product", query="havregryn").json()["id"]
    coop.site.status = 401
    assert check(coop, mid)["state"] == "access_blocked"
    coop.site.status = 200
    calls = len(coop.site.calls)
    assert check(coop, mid)["state"] == "paused" and len(coop.site.calls) == calls
    assert coop.client.post("/v1/admin/connectors/se.coop/resume").status_code == 204
    assert check(coop, mid)["state"] == "succeeded"


def test_no_offers_are_invented_for_coop_and_store_wide_monitors_are_refused(coop):
    mid = monitor(coop, "generic_product", query="havregryn").json()["id"]
    assert check(coop, mid)["result"]["offers"] == 0
    refused = coop.client.post("/v1/monitors", json={
        "name": "all", "type": "store", "store_id": coop.store_id, "interval_minutes": 60})
    assert refused.status_code == 422


def test_store_discovery_needs_a_postcode_and_runs_in_the_worker(coop):
    missing = coop.client.post("/v1/admin/retailers/se.coop/stores/discover")
    assert missing.status_code == 422 and "postcode" in missing.json()["message"]
    bad = coop.client.post("/v1/admin/retailers/se.coop/stores/discover", json={"postcode": "abc"})
    assert bad.status_code == 422
    first = coop.client.post("/v1/admin/retailers/se.coop/stores/discover", json={"postcode": "11120"})
    again = coop.client.post("/v1/admin/retailers/se.coop/stores/discover", json={"postcode": "111 20"})
    assert first.status_code == 202 and again.json()["job_id"] == first.json()["job_id"]
    assert coop.site.calls == []  # the API never contacts the retailer itself
    result = job_of(coop, first)["result"]
    assert result["status"] == "success" and result["stores"] == 1 and result["created"] == 0
    wrong = coop.client.post("/v1/admin/retailers/se.coop/stores/discover", json={"postcode": "00000"})
    outcome = job_of(coop, wrong)["result"]
    assert outcome["status"] == "extraction_failed" and "postcode" in outcome["detail"]
    with SessionLocal() as db:  # a mistyped postcode is not a layout failure and does not count
        assert (db.get(ConnectorState, "se.coop").consecutive_failures) == 0
    assert run_job() is False

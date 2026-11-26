"""City Gross connector: parsing real (trimmed) responses and the polite HTTP behaviour."""
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.connectors.base import ConnectorError, ScanRequest, TransientError
from app.connectors.citygross import CityGrossConnector, parse_product
from app.connectors.http import PoliteClient, RateLimiter, host_allowed
from app.pricing import parse_size

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(), parse_float=Decimal)


def products():
    return {p["id"]: p for p in load("citygross_search.json")["searchResults"]["products"]}


class FakeSite:
    """Stands in for citygross.se; records every request it receives."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.status = 200
        self.headers: dict = {}
        self.body = None  # overrides the generated response when set
        self.reported_total = None  # lets a test make totalCount overstate, as the real API does
        self.items = list(products().values())

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.status != 200:
            return httpx.Response(self.status, headers=self.headers)
        if self.body is not None:
            return httpx.Response(200, content=self.body)
        if request.url.path.endswith("/sites"):
            return httpx.Response(200, content=(FIXTURES / "citygross_sites.json").read_bytes())
        text = request.url.params["SearchQuery"].casefold()
        hits = [p for p in self.items if text in (p["id"].casefold(), str(p["gtin"]))
                or text in f"{p['name']} {p['subtitle']}".casefold()]
        skip, take = int(request.url.params["skip"]), int(request.url.params["take"])
        page = hits[skip:skip + take]
        total = self.reported_total if self.reported_total is not None else len(hits)
        body = {"searchResults": {"products": page, "totalCount": total}}
        return httpx.Response(200, content=json.dumps(body, default=float).encode())


def connector(site, max_requests=6, interval=0):
    client = PoliteClient(["www.citygross.se"], interval, transport=httpx.MockTransport(site))
    return CityGrossConnector({"max_requests_per_scan": max_requests}, client)


# ---- parsing ---------------------------------------------------------------------------

def test_plain_product_has_price_gtin_size_and_stock():
    price, offers = parse_product(products()["100132321_ST"])
    assert (price.sku, price.current_price, price.regular_price) == (
        "100132321_ST", Decimal("19.95"), Decimal("19.95"))
    assert price.gtin == "7310130321085" and price.brand == "AXA"
    assert (price.package_value, price.package_unit) == (Decimal("1.5"), "kg")
    assert price.availability == "in_stock" and price.name == "Havregryn 1,5KG AXA"
    assert price.source_url.startswith("https://www.citygross.se/matvaror/")
    assert offers == []


def test_single_item_promotion_sets_the_price_and_is_also_an_offer():
    price, offers = parse_product(products()["100132394_ST"])
    assert (price.current_price, price.regular_price) == (Decimal("17.95"), Decimal("20.75"))
    (offer,) = offers
    assert offer.terms == "17.95 kr" and offer.price == Decimal("17.95")
    assert offer.eligibility == "public" and offer.skus == ("100132394_ST",)
    assert offer.valid_from.tzinfo is not None and offer.valid_to > offer.valid_from
    assert offer.identity_key.startswith("100132394_ST:")  # SKU keeps shared campaign IDs apart


def test_multi_buy_stays_an_offer_and_does_not_change_the_single_price():
    price, offers = parse_product(products()["101231503_ST"])
    assert price.current_price == Decimal("19.5")
    assert [o.terms for o in offers] == ["2 för 32 kr"] and offers[0].price == Decimal(32)
    price, offers = parse_product(products()["101892451_ST"])
    assert offers[0].terms.startswith("9 för ") and price.package_unit == "L"


def test_member_only_promotion_is_a_member_offer_not_a_public_price():
    raw = json.loads(json.dumps(products()["100132394_ST"], default=float))
    raw["productStoreDetails"]["prices"]["promotions"][0]["membersOnly"] = True
    price, (offer,) = parse_product(raw)
    assert price.current_price == Decimal("20.75")
    assert offer.eligibility == "member" and offer.terms.startswith("Medlemspris:")


@pytest.mark.parametrize("change", [
    lambda r: r.update(sellable=False),
    lambda r: r["productStoreDetails"].update(p_has_price=False),
    lambda r: r["productStoreDetails"]["prices"]["currentPrice"].update(price=None),
    lambda r: r["productStoreDetails"].update(prices=None),
    lambda r: r.pop("id"),
])
def test_unknown_prices_are_skipped_not_zero(change):
    raw = json.loads(json.dumps(products()["100132321_ST"], default=float))
    change(raw)
    assert parse_product(raw) is None


def test_bad_gtin_and_unknown_stock_and_size_stay_unknown():
    raw = json.loads(json.dumps(products()["100132321_ST"], default=float))
    raw.update(gtin="7310130321086", descriptiveSize="6-PACK")  # wrong check digit; not a quantity
    raw["productStoreDetails"]["stockStatus"] = 7
    price, _ = parse_product(raw)
    assert price.gtin is None and price.availability is None and price.package_value is None


def test_swedish_package_labels():
    assert parse_size("1,5KG") == (Decimal("1.5"), "kg")
    assert parse_size("33CL") == (Decimal(330), "ml")
    assert parse_size("6X33CL") == (Decimal(1980), "ml")
    assert parse_size("CA 500G") is None and parse_size(None) is None


# ---- requests --------------------------------------------------------------------------

def test_search_request_is_store_scoped_and_uses_a_generic_browser_header():
    site = FakeSite()
    snapshot = connector(site).scan("3204", "online", ScanRequest(query="havregryn"))
    assert snapshot.effective_status == "success" and snapshot.targeted
    assert [p.sku for p in snapshot.prices] == ["100132321_ST", "100132394_ST"]
    (call,) = site.calls
    assert dict(call.url.params) == {"SearchQuery": "havregryn", "skip": "0", "take": "40",
                                     "store": "3204"}
    assert call.url.host == "www.citygross.se" and call.headers["user-agent"].startswith("Mozilla/5.0")
    assert snapshot.requests == 1


def test_query_pagination_is_capped_and_reported_as_truncated():
    site = FakeSite()
    template = site.items[0]
    site.items = [{**template, "id": f"{i}_ST", "name": "Havregryn", "subtitle": str(i)}
                  for i in range(200)]
    snapshot = connector(site).scan("3204", "online", ScanRequest(query="havregryn"))
    assert len(snapshot.prices) == 80 and len(site.calls) == 2  # two pages, then stop
    assert snapshot.truncated and snapshot.effective_status == "success"
    assert "first 80" in snapshot.detail


def test_an_overstated_total_count_does_not_cause_extra_requests_or_a_false_truncation():
    site = FakeSite()
    site.reported_total = 25  # the live API reports 25 while returning 22 products
    snapshot = connector(site).scan("3204", "online", ScanRequest(query="havregryn"))
    assert len(site.calls) == 1 and not snapshot.truncated


def test_lookup_by_sku_and_barcode_and_missing_is_unknown_not_unavailable():
    site = FakeSite()
    snapshot = connector(site).scan("3204", "online", ScanRequest(
        skus=("100132321_ST",), gtins=("7310130321283",)))
    assert sorted(p.sku for p in snapshot.prices) == ["100132321_ST", "100132394_ST"]
    gone = connector(site).scan("3204", "online", ScanRequest(skus=("100132321_ST", "999_ST")))
    assert gone.effective_status == "partial" and gone.missing == ("999_ST",) and not gone.complete
    none = connector(site).scan("3204", "online", ScanRequest(gtins=("4006381333931",)))
    assert none.effective_status == "not_found" and none.missing == ("4006381333931",)


def test_untargeted_physical_and_invalid_store_requests_make_no_request():
    site = FakeSite()
    cg = connector(site)
    assert cg.scan("3204", "online", ScanRequest()).effective_status == "unsupported"
    assert cg.scan("3204", "online", None).effective_status == "unsupported"
    assert cg.scan("3204", "physical", ScanRequest(query="x")).effective_status == "unsupported"
    assert cg.scan("../x", "online", ScanRequest(query="x")).effective_status == "invalid_data"
    assert site.calls == []


@pytest.mark.parametrize("status,headers,expected", [
    (403, {}, "access_blocked"), (401, {}, "access_blocked"),
    (302, {"Location": "https://elsewhere.example/"}, "access_blocked"),
    (429, {"Retry-After": "120"}, "rate_limited"), (404, {}, "extraction_failed"),
])
def test_refusals_are_reported_and_never_retried(status, headers, expected):
    site = FakeSite()
    site.status, site.headers = status, headers
    snapshot = connector(site).scan("3204", "online", ScanRequest(query="havregryn"))
    assert snapshot.effective_status == expected and len(site.calls) == 1
    if status == 429:
        assert snapshot.retry_after == 120


@pytest.mark.parametrize("body", [b"<html>challenge</html>", b'{"unexpected": true}',
                                  b'{"searchResults": {"products": "nope"}}'])
def test_unexpected_responses_are_extraction_failures(body):
    site = FakeSite()
    site.body = body
    snapshot = connector(site).scan("3204", "online", ScanRequest(query="havregryn"))
    assert snapshot.effective_status == "extraction_failed"


@pytest.mark.parametrize("status", [500, 503])
def test_server_errors_raise_so_the_worker_retries_with_backoff(status):
    site = FakeSite()
    site.status = status
    with pytest.raises(TransientError):
        connector(site).scan("3204", "online", ScanRequest(query="havregryn"))


def test_network_timeouts_are_transient():
    def timeout(request):
        raise httpx.ReadTimeout("slow")

    cg = CityGrossConnector({}, PoliteClient(["www.citygross.se"], 0,
                                             transport=httpx.MockTransport(timeout)))
    with pytest.raises(TransientError):
        cg.scan("3204", "online", ScanRequest(query="x"))


def test_per_scan_request_budget_returns_partial_instead_of_hammering():
    site = FakeSite()
    snapshot = connector(site, max_requests=2).scan("3204", "online", ScanRequest(
        skus=("100132321_ST", "100132394_ST", "101231503_ST", "101892451_ST")))
    assert len(site.calls) == 2 and snapshot.effective_status == "partial"
    assert "budget" in snapshot.detail


def test_requests_are_spaced_by_the_minimum_interval():
    now, sleeps = [100.0], []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(3, clock=lambda: now[0], sleep=sleep)
    site = FakeSite()
    client = PoliteClient(["www.citygross.se"], 3, transport=httpx.MockTransport(site),
                          limiter=limiter)
    cg = CityGrossConnector({}, client)
    cg.scan("3204", "online", ScanRequest(skus=("100132321_ST", "100132394_ST", "101892451_ST")))
    assert sleeps == [3, 3]  # the first request is immediate; each later one waits the interval


def test_host_allowlist_refuses_other_hosts_and_plain_http():
    client = PoliteClient(["www.citygross.se", "*.example.org"], 0,
                          transport=httpx.MockTransport(FakeSite()))
    for url in ("https://evil.example/x", "http://www.citygross.se/x", "https://127.0.0.1/x"):
        with pytest.raises(ConnectorError, match="Host not allowed"):
            client.get_json(url)
    assert host_allowed("a.example.org", ["*.example.org"])
    assert not host_allowed("example.org.evil.com", ["*.example.org"])


def test_store_directory_is_one_request_and_uses_store_numbers():
    site = FakeSite()
    stores = connector(site).discover_stores()
    assert len(site.calls) == 1 and dict(site.calls[0].url.params) == {"siteTypeId": "3"}
    assert (stores[0].external_id, stores[0].name) == ("3204", "City Gross Borås")
    assert stores[0].address == "Göteborgsvägen 181, Borås"

"""Dollarstore connector: reading the data the site embeds in its pages, offers, per-store stock."""
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import ClassVar
from urllib.parse import unquote

import httpx
import pytest

from app.connectors import registry
from app.connectors.base import ExtractionError, ScanRequest
from app.connectors.contract import check_connector, client_for
from app.connectors.http import PoliteClient
from app.connectors.manifest import load_manifest
from app.connectors.registry import BUILTIN_DIR
from app.connectors.urls import match_product_url
from tests.connector_modules import module_of

_module = module_of("se.dollarstore")
DollarstoreConnector, parse_product, parse_offer = (
    _module.DollarstoreConnector, _module.parse_product, _module.parse_offer)
FIXTURES = Path(__file__).parent / "fixtures"
IKAFFE = "8068-12530"
HOOR, ALINGSAS = "Store226", "Store205"
IKAFFE_PAGE = "/mat-dryck/lask-dryck/8068-12530--oatly-ikaffe"
LINK = "https://dollarstore.se" + IKAFFE_PAGE


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def flight(*rows) -> str:
    """A page that carries its data the way the site does (Next.js flight rows in one script)."""
    text = "\n".join(f"{i}:{json.dumps(row, ensure_ascii=False)}" for i, row in enumerate(rows, start=7)) + "\n"
    return f"<html><body><script>self.__next_f.push([1,{json.dumps(text, ensure_ascii=False)}])</script></body></html>"


def products(html_name: str) -> list[dict]:
    html = fixture(html_name)
    return next(_module.find_all(html, lambda d: isinstance(d.get("productSearch"), dict)))["productSearch"]["nodes"]


class FakeDollarstore:
    """Stands in for dollarstore.se: search pages, one product page whose stock depends on the
    store cookie (like the real site), and the store list. Records every request."""
    STOCK: ClassVar[dict[str, int]] = {HOOR: 207, ALINGSAS: 106}

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.status != 200:
            return httpx.Response(self.status)
        path, text = request.url.path, request.url.params.get("q", "").casefold()
        if path == "/sokresultat":
            if "oatly" in text or text == IKAFFE:
                return httpx.Response(200, text=fixture("dollarstore_search_oatly.html"))
            if text in ("choklad", "2874clo", "2767clo"):
                return httpx.Response(200, text=fixture("dollarstore_search_choklad.html"))
            return httpx.Response(200, text=self.empty())
        if path == IKAFFE_PAGE:
            cookie = unquote(request.headers.get("cookie", ""))
            store = re.search(r"selectedStore=\|?(Store\d+)", cookie)
            quantity = self.STOCK.get(store.group(1), 8000) if store else 8000
            node = next(_module.find_all(fixture("dollarstore_product_ikaffe.html"),
                                         lambda d: d.get("articleNumber") == IKAFFE))
            return httpx.Response(200, text=flight({**node, "stockStatus": {"inStockQuantity": quantity}}))
        if path == "/hitta-till-butik":
            return httpx.Response(200, text=fixture("dollarstore_stores.html"))
        return httpx.Response(404)

    @staticmethod
    def empty() -> str:
        return flight({"result": {"productSearch": {"totalCount": 0, "pageInfo": {"hasNextPage": False}, "nodes": []}}})


def connector(site, max_requests=8):
    client = PoliteClient(["dollarstore.se"], 0, transport=httpx.MockTransport(site))
    return DollarstoreConnector({"max_requests_per_scan": max_requests}, client)


def scan(site, request, store=HOOR):
    return connector(site).scan(store, "online", request)


# ---- reading products ------------------------------------------------------------------------

def test_a_product_has_the_consumer_price_a_real_name_a_size_and_its_offer():
    (node,) = products("dollarstore_search_oatly.html")
    price, (offer,) = parse_product(node)
    assert (price.sku, price.name, price.current_price, price.regular_price) == (
        IKAFFE, "Oatly iKaffe 1L", Decimal(20), None)
    assert (price.package_value, price.package_unit, price.brand, price.currency) == (
        Decimal(1), "L", "Oatly", "SEK")
    assert price.source_url == LINK and price.gtin is None
    assert (offer.terms, offer.price, offer.eligibility, offer.identity_key) == (
        "2 för 35 kr", Decimal(35), "public", f"{IKAFFE}:mixmatch")
    assert offer.skus == (IKAFFE,) and offer.valid_to is None            # no dates are published


def test_the_real_name_wins_over_the_category_and_a_size_is_added_once():
    by_sku = {parse_product(n)[0].sku: parse_product(n) for n in products("dollarstore_search_choklad.html")}
    lion = by_sku["14409-12328784"][0]
    assert lion.name == "Lion White 42g Nestle" and (lion.package_value, lion.package_unit) == (Decimal(42), "g")
    assert by_sku["14409-12243570"][0].package_value is None              # no size stated: unknown
    clo = by_sku["2874clo"]                                               # the address spells it lower case
    assert clo[0].name == "Sportlunch" and clo[1][0].terms == "2 för 22 kr"
    assert by_sku["2767clo"][1][0].terms == "3 för 20 kr"
    assert all(price.name != "Choklad" for price, _ in by_sku.values())


@pytest.mark.parametrize("text, terms, amount", [
    ("2 för 35", "2 för 35 kr", Decimal(35)), ("3 för 20 kr", "3 för 20 kr", Decimal(20)),
    ("2 FOR 12,50", "2 för 12.50 kr", Decimal("12.50")), ("Köp 2 betala för 1", "Köp 2 betala för 1", None)])
def test_offer_wording_is_read_when_it_has_an_amount_and_kept_as_text_when_it_does_not(text, terms, amount):
    offer = parse_offer("x1", "T", text, LINK)
    assert (offer.terms, offer.price) == (terms, amount)
    assert parse_offer("x1", "T", "", LINK) is None and parse_offer("x1", "T", None, LINK) is None


def test_an_unknown_price_is_skipped_and_a_campaign_price_becomes_the_price():
    (node,) = products("dollarstore_search_oatly.html")
    assert parse_product({**node, "price": {"unitPriceIncludingVat": None}}) is None
    assert parse_product({**node, "price": {"unitPriceIncludingVat": 20, "currency": {"code": "EUR"}}}) is None
    assert parse_product({**node, "articleNumber": ""}) is None
    price, _ = parse_product({**node, "price": {"unitPriceIncludingVat": 20, "discountPriceIncludingVat": 17}})
    assert (price.current_price, price.regular_price) == (Decimal(17), Decimal(20))
    price, _ = parse_product({**node, "price": {"unitPriceIncludingVat": 20, "discountPriceIncludingVat": 25}})
    assert (price.current_price, price.regular_price) == (Decimal(20), None)   # never "discounted" upwards


# ---- stock depends on the store -------------------------------------------------------------------

def test_a_search_reads_the_stock_of_the_store_it_is_for():
    site = FakeDollarstore()
    snapshot = scan(site, ScanRequest(query="oatly"), HOOR)
    (price,) = snapshot.prices
    assert (price.availability, price.stock_quantity) == ("in_stock", 207)
    assert [o.terms for o in snapshot.offers] == ["2 för 35 kr"]
    assert [c.url.path for c in site.calls] == ["/sokresultat", IKAFFE_PAGE]
    assert {c.url.host for c in site.calls} == {"dollarstore.se"}
    assert all("selectedStore=%7CStore226" in c.headers["cookie"] for c in site.calls)   # the store, every time
    other = scan(FakeDollarstore(), ScanRequest(query="oatly"), ALINGSAS).prices[0]
    assert (other.availability, other.stock_quantity) == ("in_stock", 106)


def test_the_national_default_is_unknown_stock_and_zero_is_out_of_stock(monkeypatch):
    unknown = scan(FakeDollarstore(), ScanRequest(query="oatly"), "Store999").prices[0]
    assert (unknown.availability, unknown.stock_quantity) == (None, None)        # an unknown store: 8000
    monkeypatch.setitem(FakeDollarstore.STOCK, HOOR, 0)
    empty = scan(FakeDollarstore(), ScanRequest(query="oatly"), HOOR).prices[0]
    assert (empty.availability, empty.stock_quantity) == ("out_of_stock", 0)


def test_a_broad_search_reads_stock_for_a_few_and_says_so_and_says_when_there_is_more():
    site = FakeDollarstore()
    snapshot = scan(site, ScanRequest(query="choklad"))
    assert len(snapshot.prices) == 6 and snapshot.truncated                      # the site has more pages
    pages = [c for c in site.calls if c.url.path != "/sokresultat"]
    assert len(pages) == 5                                                        # capped, one request each
    assert "stock was read for 5 of 6 products" in snapshot.detail and "first 6" in snapshot.detail
    assert all(p.availability is None for p in snapshot.prices)                   # those pages are unavailable here


# ---- looking a product up again -------------------------------------------------------------------

def test_a_product_is_found_again_by_its_article_number_in_any_case():
    site = FakeDollarstore()
    snapshot = scan(site, ScanRequest(skus=(IKAFFE,)))
    assert [p.sku for p in snapshot.prices] == [IKAFFE] and snapshot.effective_status == "success"
    assert snapshot.prices[0].stock_quantity == 207
    assert [c.url.path for c in site.calls] == ["/sokresultat", IKAFFE_PAGE]
    assert [p.sku for p in scan(FakeDollarstore(), ScanRequest(skus=("2874CLO",))).prices] == ["2874clo"]


def test_a_product_that_is_not_there_is_missing_not_discontinued_and_a_barcode_is_not_searchable():
    snapshot = scan(FakeDollarstore(), ScanRequest(skus=("no-such-product", "bad sku!")))
    assert snapshot.effective_status == "not_found" and set(snapshot.missing) == {"no-such-product", "bad sku!"}
    snapshot = scan(FakeDollarstore(), ScanRequest(gtins=("7394376616037",)))
    assert snapshot.effective_status == "not_found" and snapshot.prices == []


def test_a_malformed_store_is_refused_before_any_request():
    site = FakeDollarstore()
    assert connector(site).scan("Höör", "online", ScanRequest(query="oatly")).effective_status == "invalid_data"
    assert site.calls == []


def test_a_refusal_is_reported_and_never_worked_around():
    site = FakeDollarstore()
    site.status = 403
    assert scan(site, ScanRequest(query="oatly")).effective_status == "access_blocked"
    assert len(site.calls) == 1


# ---- stores ----------------------------------------------------------------------------------------

def test_the_stores_come_from_the_sites_own_list_with_their_addresses():
    site = FakeDollarstore()
    stores = connector(site).discover_stores()
    assert {s.external_id: s.name for s in stores} == {
        "Store205": "Dollarstore Alingsås", "Store231": "Dollarstore Alvesta", "Store226": "Dollarstore Höör"}
    assert next(s for s in stores if s.external_id == HOOR).address.endswith("Höör")
    assert [c.url.path for c in site.calls] == ["/hitta-till-butik"]


@pytest.mark.parametrize("place, ids", [("Höör", {HOOR}), ("höör", {HOOR}), ("243", {HOOR}), ("2439", {HOOR}),
                                          ("alv", {"Store231"}), ("  ", {HOOR, ALINGSAS, "Store231"})])
def test_a_town_or_postcode_narrows_the_stores(place, ids):
    assert {s.external_id for s in connector(FakeDollarstore()).discover_stores(place=place)} == ids


def test_no_matching_store_is_an_error_not_an_empty_success():
    with pytest.raises(ExtractionError, match="zzz"):
        connector(FakeDollarstore()).discover_stores(place="zzz")


# ---- how it fits ----------------------------------------------------------------------------------------

def test_the_shared_contract_passes():
    manifest = load_manifest(BUILTIN_DIR / "se.dollarstore" / "manifest.yaml")

    def make(transport, secrets):
        return DollarstoreConnector(manifest.model_dump(), client_for(manifest, transport))

    passed = check_connector(make, FakeDollarstore(), manifest=manifest, store=HOOR,
                             request=ScanRequest(query="oatly"), discover_inputs={"place": "Höör"})
    assert {"identity", "targeted_only", "happy_path", "refusals", "product_urls", "store_discovery"} <= set(passed)


def test_product_page_links_are_read_and_never_fetched():
    rules = registry.retailer("se.dollarstore")["product_urls"]
    assert match_product_url(rules, LINK) == IKAFFE
    assert match_product_url(rules, "https://www.dollarstore.se/mat-dryck/14409-12243570--smarties") == "14409-12243570"
    assert match_product_url(rules, "https://dollarstore.se/mat-dryck/2874clo--sportlunch") == "2874clo"
    assert match_product_url(rules, "https://dollarstore.se/hitta-till-butik/hoor") is None
    assert match_product_url(rules, "https://dollarstore.se/mat-dryck") is None


def test_the_manifest_names_only_its_own_host_and_stays_polite():
    info = registry.retailer("se.dollarstore")
    assert info["allowed_hosts"] == ["dollarstore.se"]
    assert info["capabilities"]["barcode_search"] == "unsupported"
    assert info["capabilities"]["store_discovery"] == "supported" and info["max_requests_per_day"] <= 400
    assert [i["name"] for i in info["store_discovery_inputs"]] == ["place"] and not info["store_discovery_inputs"][0]["required"]


# ---- through the service: offers and stock in a search -------------------------------------------------

@pytest.fixture
def shop(fresh_db, monkeypatch):
    from types import SimpleNamespace

    from app import retrieval
    from app.connectors.registry import CONNECTORS
    from app.db import SessionLocal
    from app.models import Store
    from tests.test_regressions import admin_client

    retrieval.clear_cache()
    site = FakeDollarstore()
    monkeypatch.setitem(CONNECTORS, "se.dollarstore", connector(site))
    with SessionLocal.begin() as db:
        hoor = Store(retailer_id="se.dollarstore", external_id=HOOR, name="Dollarstore Höör")
        alingsas = Store(retailer_id="se.dollarstore", external_id=ALINGSAS, name="Dollarstore Alingsås")
        db.add_all([hoor, alingsas])
        db.flush()
        ids = {HOOR: hoor.id, ALINGSAS: alingsas.id}
    return SimpleNamespace(site=site, client=admin_client(), ids=ids)


def test_a_search_shows_the_offer_and_the_stock_at_each_store(shop):
    from tests.test_politeness import job_of
    for store, quantity in ((HOOR, 207), (ALINGSAS, 106)):
        result = job_of(shop, shop.client.post("/v1/search/jobs", json={"query": "oatly", "store_ids": [shop.ids[store]]}))
        assert result["state"] == "succeeded"
        (item,) = result["result"]["items"]
        assert item["offers"] == [{"terms": "2 för 35 kr", "title": "Oatly iKaffe 1L", "price": "35",
                                   "eligibility": "public", "valid_to": None}]
        assert (item["availability"], item["stock_quantity"]) == ("in_stock", quantity)
        assert (item["display_name"], item["brand"], item["size"]) == ("iKaffe", "Oatly", "1 L")
        assert item["current_price"] == "20.00" and item["url"] == LINK


def test_willys_offers_reach_the_search_results_too():
    willys = module_of("se.willys")
    raw = {"code": "101231662_ST", "name": "Ikaffe Barista Edition Havredryck 3%", "priceValue": 17.9,
           "manufacturer": "Oatly", "displayVolume": "1 liter",
           "potentialPromotions": [{"code": "2500330117", "campaignType": "GENERAL", "qualifyingCount": 2,
                                    "rewardLabel": "30,00", "price": {"value": 15.0}}]}
    _price, offers = willys.parse_product(raw)
    assert [(o.terms, o.price, o.eligibility) for o in offers] == [("2 för 30 kr", Decimal("15.0"), "public")]

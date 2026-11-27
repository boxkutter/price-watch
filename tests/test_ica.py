"""ICA connector. Fixtures are synthetic, shaped like the 2026-11-26 investigation; not live data."""
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.connectors.base import ExtractionError, ScanRequest
from app.connectors.contract import check_connector, client_for
from app.connectors.http import PoliteClient
from app.connectors.manifest import load_manifest
from app.connectors.registry import BUILTIN_DIR
from app.connectors.urls import match_product_url
from tests.connector_modules import module_of

_module = module_of("se.ica")
IcaConnector, parse_page, parse_state = _module.IcaConnector, _module.parse_page, _module.parse_state
manifest = load_manifest(Path(BUILTIN_DIR) / "se.ica" / "manifest.yaml")
STORE = "1003395"
MILK = {"retailerProductId": "100200", "name": "Mellanmjölk 1,5%", "brand": "ICA", "size": "1.5l",
        "price": {"current": {"amount": "17.90", "currency": "SEK"}}, "available": True,
        "promotions": [{"retailerPromotionId": "p77", "description": "2 för 30 kr"},
                       {"retailerPromotionId": "p78", "description": "Stammispris 2 för 28 kr"},
                       {"retailerPromotionId": "p79", "description": "Nyhet"}]}
OATS = {"retailerProductId": "100300", "name": "Havregryn 750 g", "size": "0.75kg",
        "price": {"current": {"amount": "24.5", "currency": "SEK"}}}
NOPRICE = {"retailerProductId": "100400", "name": "Slut vara", "size": "500g"}
PRODUCTS = {r["retailerProductId"]: r for r in (MILK, OATS, NOPRICE)}


def page(*records, ld=None):
    state = json.dumps({"session": {"metadata": {"retailerRegionId": STORE}},
                        "data": {"products": {"productEntities": {r["retailerProductId"]: r for r in records}}}})
    html = f"<script>window.__INITIAL_STATE__ = {state};window.other=1</script>"
    if ld:
        html += f'<script type="application/ld+json">{json.dumps(ld)}</script>'
    return html


def site(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    assert request.url.host == "handlaprivatkund.ica.se"
    if path == f"/stores/{STORE}/search":
        hits = [MILK, OATS, NOPRICE] if request.url.params.get("q") == "mjölk" else []
        return httpx.Response(200, text=page(*hits))
    sku = path.rsplit("/", 1)[-1]
    if path.startswith(f"/stores/{STORE}/products/") and sku in PRODUCTS:
        return httpx.Response(200, text=page(PRODUCTS[sku]))
    return httpx.Response(404)


def make(transport, secrets):
    return IcaConnector(manifest.model_dump(), client_for(manifest, transport))


def test_the_connector_passes_the_contract():
    passed = check_connector(make, site, manifest=manifest, store=STORE, request=ScanRequest(query="mjölk"))
    assert "happy_path" in passed


def connector(handler):
    return IcaConnector({"max_requests_per_scan": 6},
                        PoliteClient(["handlaprivatkund.ica.se"], 0, transport=httpx.MockTransport(handler)))


def test_a_search_gives_exact_prices_sizes_and_skips_unpriced_products():
    snap = connector(site).scan(STORE, "online", ScanRequest(query="mjölk"))
    prices = {p.sku: p for p in snap.prices}
    assert set(prices) == {"100200", "100300"}  # the product without a price is skipped, never 0
    assert prices["100200"].current_price == Decimal("17.90") and prices["100200"].currency == "SEK"
    assert (prices["100300"].package_value, prices["100300"].package_unit) == (Decimal("0.75"), "kg")
    assert prices["100200"].availability is None and prices["100200"].gtin is None  # orderable != in stock


def test_multibuy_promotions_become_offers_and_wording_without_an_amount_does_not():
    snap = connector(site).scan(STORE, "online", ScanRequest(query="mjölk"))
    offers = {o.identity_key: o for o in snap.offers}
    assert set(offers) == {"100200:p77", "100200:p78"}
    assert offers["100200:p77"].price == Decimal(30) and offers["100200:p77"].eligibility == "public"
    assert offers["100200:p78"].eligibility == "member" and offers["100200:p78"].price == Decimal(28)
    assert all(o.skus == ("100200",) for o in offers.values())


def test_lookup_by_sku_and_an_unknown_sku_is_missing_not_discontinued():
    snap = connector(site).scan(STORE, "online", ScanRequest(skus=("100300", "999999", "100400")))
    assert [p.sku for p in snap.prices] == ["100300"]
    assert set(snap.missing) == {"999999", "100400"} and snap.effective_status == "partial"


def test_a_barcode_is_unsupported_and_makes_no_request():
    calls = []
    snap = connector(lambda r: calls.append(r) or httpx.Response(404)).scan(
        STORE, "online", ScanRequest(gtins=("7310130321085",)))
    assert calls == [] and snap.missing == ("7310130321085",)


def test_json_ld_supplies_stock_and_is_enough_alone():
    ld = {"@type": "Product", "sku": "100300", "name": "Havregryn 750 g",
          "offers": {"price": "24.50", "availability": "https://schema.org/OutOfStock"}}
    price, _, _ = parse_page(page(OATS, ld=ld), "100300", STORE)
    assert price.availability == "out_of_stock" and price.current_price == Decimal("24.5")
    only_ld = '<script type="application/ld+json">' + json.dumps(ld) + "</script>"
    price, _, name = parse_page(only_ld, "100300", STORE)
    assert (price.current_price, name) == (Decimal("24.50"), "Havregryn 750 g")


@pytest.mark.parametrize("html", ["<html>Access denied</html>", "<script>window.__INITIAL_STATE__ = {broken</script>",
                                  page(OATS)])
def test_a_page_without_the_product_is_an_extraction_failure(html):
    with pytest.raises(ExtractionError):
        parse_page(html, "424242", STORE)


def test_a_refusal_pauses_the_connector_and_is_not_worked_around():
    calls = []

    def refuse(request):
        calls.append(request)
        return httpx.Response(403, text="forbidden")
    snap = connector(refuse).scan(STORE, "online", ScanRequest(query="mjölk"))
    assert snap.effective_status == "access_blocked" and len(calls) == 1


def test_state_parsing_ignores_text_after_the_json():
    assert set(parse_state(page(MILK, OATS))) == {"100200", "100300"}


def test_a_pasted_link_gives_the_sku():
    rules = [r.model_dump() for r in manifest.product_urls]
    base = "https://handlaprivatkund.ica.se/stores/1003395/products/"
    assert match_product_url(rules, base + "mellanmjolk-1-5/100200") == "100200"
    assert match_product_url(rules, base + "100200") == "100200"
    assert match_product_url(rules, "https://www.ica.se/recept/") is None

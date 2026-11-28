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
    if path == "/butiker/":
        assert request.url.host == "www.ica.se"
        return httpx.Response(200, text=FINDER_PAGE)
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
    passed = check_connector(make, site, manifest=manifest, store=STORE, request=ScanRequest(query="mjölk"),
                             discover_inputs={"place": "24372"})
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


# ---- the store-shop tile, from a saved copy of the real store start page (2026-11-27) -----------

TILE = (Path(__file__).parent / "fixtures" / "ica_tile.html").read_text(encoding="utf-8")


def test_a_real_tile_gives_price_original_price_size_and_sku():
    price, offers, name = parse_page(TILE, "1489244", STORE)
    assert name == "Gurka ca 320g Klass 1 ICA"
    assert price.current_price == Decimal(12) and price.regular_price == Decimal("14.90")
    assert (price.package_value, price.package_unit) == (Decimal("0.38"), "kg")
    assert price.source_url.endswith("/stores/1003395/products/1489244") and offers == []


def test_a_multibuy_tile_gives_an_offer_and_a_member_promotion_is_not_the_price():
    multi = TILE.replace(">12 kr/st<", ">2 för 25 kr<").replace("Ord.pris 14,90", "Ord.pris 14,90")
    _, offers, _ = parse_page(multi, "1489244", STORE)
    assert [(o.price, o.eligibility) for o in offers] == [(Decimal(25), "public")]
    member = TILE.replace(">12 kr/st<", ">Stammispris 12 kr/st<")
    price, _, _ = parse_page(member, "1489244", STORE)
    assert price.current_price == Decimal("14.90") and price.regular_price is None


def test_a_tile_without_a_price_is_skipped():
    unpriced = TILE.replace('data-test="fop-price"', 'data-test="fop-x"')
    assert parse_page(unpriced, "1489244", STORE)[0] is None


def test_the_approved_request_headers_are_sent_on_every_request():
    seen = []

    def handler(request):
        seen.append(request.headers)
        return httpx.Response(200, text=TILE)
    client = client_for(manifest, httpx.MockTransport(handler))
    IcaConnector(manifest.model_dump(), client).scan(STORE, "online", ScanRequest(query="gurka"))
    headers = seen[0]
    assert "Chrome/158" in headers["user-agent"] and headers["sec-fetch-mode"] == "navigate"
    assert headers["accept"].startswith("text/html") and "image/avif" in headers["accept"]
    assert "cookie" not in headers and "authorization" not in headers


@pytest.mark.parametrize("name", ["Cookie", "Authorization", "Host", "Proxy-Authorization"])
def test_a_manifest_cannot_set_credential_or_connection_headers(name):
    from app.connectors.manifest import parse_manifest
    text = (Path(BUILTIN_DIR) / "se.ica" / "manifest.yaml").read_text().replace(
        "request_headers:\n", f"request_headers:\n  {name}: x\n")
    with pytest.raises(Exception, match="request_headers"):
        parse_manifest(text)


# ---- a real search page: /stores/1003395/search?q=mjölk (saved 2026-11-27) ---------------------

SEARCH = (Path(__file__).parent / "fixtures" / "ica_search.html").read_text(encoding="utf-8")


def test_a_real_search_page_gives_its_products_with_exact_prices_and_sizes():
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, text=SEARCH)
    snap = connector(handler).scan(STORE, "online", ScanRequest(query="mjölk"))
    assert seen[0].path == "/stores/1003395/search" and seen[0].params["q"] == "mjölk"
    prices = {p.sku: p for p in snap.prices}
    assert set(prices) == {"2095219", "1407322", "1354730", "1520842", "1487070", "1527637"}
    milk = prices["2095219"]  # "20,93 kr" with a non-breaking space; the size label is "1.5L"
    assert milk.current_price == Decimal("20.93") and milk.regular_price is None
    assert (milk.package_value, milk.package_unit) == (Decimal("1.5"), "L")
    assert (prices["1354730"].package_value, prices["1354730"].package_unit) == (Decimal("0.7"), "kg")
    assert len(snap.offers) == 1 and snap.effective_status == "success"


def test_a_real_multibuy_member_tile_keeps_the_ordinary_price_and_makes_an_offer():
    snap = connector(lambda r: httpx.Response(200, text=SEARCH)).scan(STORE, "online", ScanRequest(query="mjölk"))
    valio = next(p for p in snap.prices if p.sku == "1487070")
    assert valio.current_price == Decimal("21.60") and valio.regular_price is None  # not the 50 kr bundle
    (offer,) = snap.offers
    assert offer.identity_key == "1487070:f80ac37e-0faf-4c48-9875-193d913e84d2"
    assert offer.price == Decimal(50) and offer.eligibility == "member" and "3 för 50 kr" in offer.terms


# ---- a real product page (saved 2026-11-27): the main product is not a tile -------------------

PRODUCT = (Path(__file__).parent / "fixtures" / "ica_product.html").read_text(encoding="utf-8")
MILK_PAGE = "/stores/1003395/products/mellanmj%C3%B6lkdryck-laktosfri-1-5-1l-ica/1407322"


def test_a_real_product_page_gives_the_main_product_not_a_related_one():
    price, offers, name = parse_page(PRODUCT, "1407322", STORE)
    assert name == "Mellanmjölkdryck Laktosfri 1,5% 1l ICA" and offers == []
    assert price.sku == "1407322" and price.current_price == Decimal("14.42") and price.regular_price is None
    assert (price.package_value, price.package_unit) == (Decimal(1), "L")
    related = next(iter(parse_state(PRODUCT)))  # the carousel tile is another product
    assert related != "1407322"


def test_a_lookup_by_sku_reads_the_product_page_and_agrees_with_search():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, text=PRODUCT)
    snap = connector(handler).scan(STORE, "online", ScanRequest(skus=("1407322",)))
    assert paths == ["/stores/1003395/products/1407322"]
    assert [(p.sku, p.current_price) for p in snap.prices] == [("1407322", Decimal("14.42"))]
    search = connector(lambda r: httpx.Response(200, text=SEARCH)).scan(STORE, "online", ScanRequest(query="mjölk"))
    assert {p.sku: p.current_price for p in search.prices}["1407322"] == Decimal("14.42")


def test_the_bare_id_address_redirects_to_the_named_product_page_and_is_followed():
    """Confirmed in a browser (2026-11-27): /products/<id> sends a person to /products/<name>/<id>."""
    paths = []

    def handler(request):
        paths.append(request.url.raw_path.decode())
        if request.url.path == "/stores/1003395/products/1407322":
            return httpx.Response(301, headers={"Location": "https://handlaprivatkund.ica.se" + MILK_PAGE})
        return httpx.Response(200, text=PRODUCT) if request.url.raw_path.decode() == MILK_PAGE else httpx.Response(404)
    client = PoliteClient(["handlaprivatkund.ica.se"], 0, transport=httpx.MockTransport(handler), follow_redirects=True)
    snap = IcaConnector({"max_requests_per_scan": 6}, client).scan(STORE, "online", ScanRequest(skus=("1407322",)))
    assert paths == ["/stores/1003395/products/1407322", MILK_PAGE]  # both hops are requests
    assert [p.current_price for p in snap.prices] == [Decimal("14.42")]
    assert manifest.follow_redirects is True and set(manifest.allowed_hosts) == {"handlaprivatkund.ica.se", "www.ica.se"}


# ---- a real product page with a promotion, "2 för 50 kr" with dates (saved 2026-11-27) ----------

OFFER_PAGE = (Path(__file__).parent / "fixtures" / "ica_product_offer.html").read_text(encoding="utf-8")


def test_a_product_page_with_a_multibuy_keeps_the_single_price_and_adds_a_dated_offer():
    price, offers, name = parse_page(OFFER_PAGE, "1025999", STORE)
    assert name == "Gourmetskinka Rökt Tunna Skivor 200g Pärsons"
    assert price.current_price == Decimal("28.40")  # 142 kr/kg x 0.2 kg: the single-item price, not 50
    assert (price.package_value, price.package_unit) == (Decimal("0.2"), "kg")
    (offer,) = offers
    assert offer.identity_key == "1025999:4d0fde93-cd56-4b15-b194-fe27abb3a296"
    assert offer.price == Decimal(50) and offer.eligibility == "public"
    assert offer.terms == "2 för 50 kr -- Max 1 erbj/hushåll"
    assert (offer.valid_from.isoformat(), offer.valid_to.isoformat()) == (
        "2026-11-23T00:00:00+01:00", "2026-11-29T23:59:59+01:00")


def test_offer_periods_are_read_across_a_new_year_and_junk_gives_none():
    parse_period = _module.parse_period
    start, end = parse_period("28/12-3/1 2027, erbj jfr pris")
    assert (start.year, end.year, end.day) == (2026, 2027, 3)
    assert parse_period("ingen period") == (None, None) and parse_period("31/2-1/3 2026") == (None, None)


# ---- a real search with a shared offer: /search?q=Tunna Skivor (saved 2026-11-27) ----------------

OFFER_SEARCH = (Path(__file__).parent / "fixtures" / "ica_search_offer.html").read_text(encoding="utf-8")


def test_one_offer_on_several_search_tiles_is_one_offer_per_product_with_single_item_prices():
    seen = []

    def handler(request):
        seen.append(request.url.params["q"])
        return httpx.Response(200, text=OFFER_SEARCH)
    snap = connector(handler).scan(STORE, "online", ScanRequest(query="Tunna Skivor"))
    assert seen == ["Tunna Skivor"]
    prices = {p.sku: p.current_price for p in snap.prices}
    assert prices["1025999"] == Decimal("28.40") and prices["1026006"] == Decimal("35.86")  # never the bundle's 50
    assert len(prices) == 5 and all(p != Decimal(50) for p in prices.values())
    offers = {o.identity_key: o for o in snap.offers}
    promo = "4d0fde93-cd56-4b15-b194-fe27abb3a296"
    assert set(offers) == {f"{sku}:{promo}" for sku in ("1025999", "1026006", "1026000", "1026003")}
    assert all(o.price == Decimal(50) and o.eligibility == "public" and o.skus == (o.identity_key.split(":")[0],)
               for o in offers.values())
    assert all(o.valid_from is None and o.valid_to is None for o in offers.values())  # a tile states no dates


# ---- store discovery: ICA's store finder, /butiker/?q=24372 (saved 2026-11-27) -------------------

FINDER_PAGE = (Path(__file__).parent / "fixtures" / "ica_stores.html").read_text(encoding="utf-8")


def test_store_discovery_lists_the_nearby_stores_that_have_online_shopping():
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, text=FINDER_PAGE)
    client = PoliteClient(["handlaprivatkund.ica.se", "www.ica.se"], 0, transport=httpx.MockTransport(handler))
    stores = IcaConnector({}, client).discover_stores(place="24372")
    assert (seen[0].host, seen[0].path, seen[0].params["q"]) == ("www.ica.se", "/butiker/", "24372")
    assert {s.external_id for s in stores} == {"1003395", "1003545", "1003793", "1003966", "1004163", "1004334", "1004395"}
    hoor = next(s for s in stores if s.external_id == "1003395")
    assert (hoor.name, hoor.address) == ("ICA Supermarket Höör", "Friluftsvägen 14, Höör")
    assert "1004083" not in {s.external_id for s in stores}  # no shop of its own: the header's link is not its


def test_discovery_needs_a_place_and_an_empty_result_is_an_extraction_failure():
    client = PoliteClient(["www.ica.se"], 0, transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html/>")))
    ica = IcaConnector({}, client)
    with pytest.raises(ExtractionError):
        ica.discover(place="")
    with pytest.raises(ExtractionError):
        ica.discover(place="nowhere")


def test_the_manifest_declares_discovery_and_both_hosts():
    assert manifest.capabilities["store_discovery"] == "supported"
    assert set(manifest.allowed_hosts) == {"handlaprivatkund.ica.se", "www.ica.se"}
    assert manifest.store_discovery_inputs[0].name == "place"


def test_no_store_is_preselected_every_store_is_found_by_searching():
    assert manifest.stores == [] and manifest.store_discovery_inputs[0].required is True

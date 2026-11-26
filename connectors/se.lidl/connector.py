"""Lidl (se.lidl): price and stock of products on lidl.se's online shop, from their product pages.

Notes learned from the live site (2026-11-26):
- lidl.se publishes no usable search or product API (robots.txt disallows /q/search?id=*), and its
  grocery prices are only in the Lidl Plus app and the weekly leaflet. What exists is an online shop
  (mostly non-food, plus some seasonal food) whose product pages embed a schema.org `Product` as
  JSON-LD with `sku`, `gtin13` (a list) and `offers` (`price`, `availability`).
- A page is `/p/<name>/p<id>`. `/p/p<id>` redirects (301) to it, so this connector opts in to
  following a redirect to its own host (`follow_redirects`); the id is the only thing a monitor stores.
- `availability` is a schema.org term. `InStoreOnly` pages carry no price at all; some out-of-stock
  pages keep their last price and some have none. A missing price is unknown, never zero, so such
  a page is reported as having no online price instead of becoming a price.
- Prices are per product, not per store: one national "online shop".
"""
import re

from app.connectors.base import ExtractionError, HttpStatusError, PriceData, ScanRequest
from app.connectors.kit import Collected, HttpJsonConnector, json_ld, to_decimal, valid_gtin

BASE = "https://www.lidl.se"
PAGE_URL = BASE + "/p/p{sku}"
SKU = re.compile(r"^\d{6,12}$")
AVAILABILITY = {"InStock": "in_stock", "OutOfStock": "out_of_stock"}  # anything else stays unknown


def _availability(value) -> str | None:
    return AVAILABILITY.get(str(value or "").rsplit("/", 1)[-1])


def _first_gtin(value) -> str | None:
    for candidate in value if isinstance(value, list) else [value]:
        if candidate and valid_gtin(candidate):
            return str(candidate)
    return None


def parse_page(html: str, sku: str) -> tuple[PriceData | None, str]:
    """(price data, name). The price data is None when the page states no online price.

    Raises ExtractionError when the page holds no product at all (a layout change or an error page).
    """
    products = [item for item in json_ld(html) if item.get("@type") == "Product"]
    product = next((p for p in products if str(p.get("sku")) == sku), None)
    if product is None:
        raise ExtractionError("The page has no product data")
    name = str(product.get("name") or sku)
    offers = product.get("offers")
    offer = (offers[0] if isinstance(offers, list) and offers else offers) or {}
    price = to_decimal(offer.get("price")) if isinstance(offer, dict) else None
    if price is None or price < 0 or (isinstance(offer, dict) and offer.get("priceCurrency", "SEK") != "SEK"):
        return None, name
    brand = product.get("brand")
    return PriceData(
        sku=sku, name=name, current_price=price, regular_price=None, currency="SEK",
        channel="online", source_url=f"{BASE}/p/p{sku}",
        gtin=_first_gtin(product.get("gtin13") or product.get("gtin") or product.get("gtin14")),
        availability=_availability(offer.get("availability")) if isinstance(offer, dict) else None,
        brand=brand.get("name") if isinstance(brand, dict) and brand.get("name") else None,
    ), name


class LidlConnector(HttpJsonConnector):
    id = "se.lidl"
    store_id_pattern = r"online"

    def source_for(self, store: str) -> str:
        return BASE

    def collect(self, store: str, request: ScanRequest) -> Collected:
        collected = Collected()
        # Search and barcode lookup are not supported (the service does not ask; this is a guard).
        unsupported = ([request.query] if request.query else []) + list(request.gtins)
        missing = list(unsupported)
        if unsupported:
            collected.notes.append("Lidl can only be looked up by product page link")
        for sku in request.skus:
            if not SKU.match(sku):
                missing.append(sku)
                continue
            try:
                html = self.client.get_text(PAGE_URL.format(sku=sku))
            except HttpStatusError as exc:
                if exc.status not in (404, 410):
                    raise
                missing.append(sku)
                continue
            collected.fetched += 1
            parsed, name = parse_page(html, sku)
            if parsed is None:
                missing.append(sku)
                collected.notes.append(f"Lidl lists '{name}' without an online price (in store only "
                                       "or not for sale)")
                continue
            collected.prices.append(parsed)
        collected.missing = tuple(missing)
        return collected

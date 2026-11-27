"""ICA (se.ica): prices and promotions for products in ICA's online shop, for one chosen store.

ICA's online shop is an Ocado-based storefront at handlaprivatkund.ica.se/stores/<storeId>/.
Its pages are server-rendered with the data embedded as `window.__INITIAL_STATE__` (product
records with `retailerProductId`, name, brand, size, price and promotions); product pages also
carry schema.org JSON-LD. ICA has authorized this use (see the manifest's `access_note`).

Written from the 2026-11-26 investigation in docs/implementation-status.md and tested against
fixtures shaped like it. It has NOT been run against the live site: the exact paths inside the
embedded state are read tolerantly (a record is any object with a `retailerProductId`), and a
page whose layout does not match is an extraction failure, never a price.
- Prices are per store (the `<storeId>` in the address), online prices only.
- The price of a record is the single-item price; promotions are reported as offers.
- `available` means orderable, not in stock, so availability is only taken from JSON-LD.
- No barcode is published, so barcode search is unsupported and GTIN stays a manual mapping.
"""
import json
import re
from decimal import Decimal
from urllib.parse import quote

from app.connectors.base import ExtractionError, HttpStatusError, OfferData, PriceData, ScanRequest
from app.connectors.kit import Collected, HttpJsonConnector, json_ld, to_decimal
from app.pricing import parse_size

BASE = "https://handlaprivatkund.ica.se"
SKU = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
STATE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*")
MULTIBUY = re.compile(r"(\d+)\s*för\s*(\d+(?:[.,]\d+)?)\s*kr", re.IGNORECASE)
MEMBER = re.compile(r"stammis|medlem|ica\s*kort", re.IGNORECASE)
AVAILABILITY = {"InStock": "in_stock", "OutOfStock": "out_of_stock"}  # anything else stays unknown


def embedded_state(html: str) -> object:
    """The JSON assigned to window.__INITIAL_STATE__, or None when the page has none."""
    match = STATE.search(html)
    if not match:
        return None
    try:
        return json.JSONDecoder().raw_decode(html, match.end())[0]
    except ValueError:
        return None


def _records(node, found: dict[str, dict]) -> None:
    if isinstance(node, dict):
        if node.get("retailerProductId") is not None and node.get("name"):
            found.setdefault(str(node["retailerProductId"]), node)
        for value in node.values():
            _records(value, found)
    elif isinstance(node, list):
        for value in node:
            _records(value, found)


def _amount(value) -> Decimal | None:
    if isinstance(value, dict):
        value = value.get("amount", value.get("value"))
    return to_decimal(value)


def parse_state(html: str) -> dict[str, dict]:
    """Product records in a page's embedded state, by ICA SKU. Empty when there is none."""
    found: dict[str, dict] = {}
    _records(embedded_state(html), found)
    return found


def _availability(value) -> str | None:
    return AVAILABILITY.get(str(value or "").rsplit("/", 1)[-1])


def _jsonld_product(html: str, sku: str) -> dict | None:
    return next((p for p in json_ld(html) if p.get("@type") == "Product" and str(p.get("sku")) == sku), None)


def parse_record(raw: dict, store: str, availability: str | None = None) -> PriceData | None:
    """One product record -> PriceData, or None when its price is unknown."""
    price = raw.get("price")
    price = price.get("current") if isinstance(price, dict) else price
    amount = _amount(price)
    if amount is None or amount < 0:
        return None
    currency = price.get("currency") if isinstance(price, dict) else None
    sku = str(raw["retailerProductId"])
    size = parse_size(raw.get("size"))
    brand = raw.get("brand")
    return PriceData(
        sku=sku, name=str(raw["name"]), current_price=amount, regular_price=None,
        currency=currency or "SEK", channel="online", source_url=f"{BASE}/stores/{store}/products/{sku}",
        gtin=None, availability=availability, brand=brand if isinstance(brand, str) and brand else None,
        package_value=size[0] if size else None, package_unit=size[1] if size else None)


def parse_offers(raw: dict, store: str) -> list[OfferData]:
    """Promotions with a stated amount ("2 för 45 kr"). Wording without one produces no offer."""
    sku = str(raw["retailerProductId"])
    offers = []
    for promo in raw.get("promotions") or []:
        if not isinstance(promo, dict) or promo.get("retailerPromotionId") is None:
            continue
        text = str(promo.get("description") or promo.get("name") or "").strip()
        match = MULTIBUY.search(text)
        if not text or not match:
            continue
        offers.append(OfferData(
            identity_key=f"{sku}:{promo['retailerPromotionId']}", title=str(raw["name"]), terms=text,
            price=to_decimal(match.group(2).replace(",", ".")), currency="SEK", channel="online",
            source_url=f"{BASE}/stores/{store}/products/{sku}",
            eligibility="member" if MEMBER.search(text) else "public", skus=(sku,)))
    return offers


def parse_page(html: str, sku: str, store: str) -> tuple[PriceData | None, list[OfferData], str]:
    """(price, offers, name) for one product page. Raises ExtractionError when it has no such product."""
    ld = _jsonld_product(html, sku)
    raw = parse_state(html).get(sku)
    if raw is None and ld is None:
        raise ExtractionError("The page has no product data")
    if raw is None:  # JSON-LD only: sku, price and availability are all it has
        offers = ld.get("offers")
        offer = (offers[0] if isinstance(offers, list) and offers else offers) or {}
        raw = {"retailerProductId": sku, "name": ld.get("name") or sku,
               "price": offer.get("price") if isinstance(offer, dict) else None}
    stock = None
    if ld and isinstance(ld.get("offers"), dict):
        stock = _availability(ld["offers"].get("availability"))
    return parse_record(raw, store, stock), parse_offers(raw, store), str(raw["name"])


class IcaConnector(HttpJsonConnector):
    id = "se.ica"
    store_id_pattern = r"\d{4,10}"

    def source_for(self, store: str) -> str:
        return f"{BASE}/stores/{store}/"

    def collect(self, store: str, request: ScanRequest) -> Collected:
        collected = Collected()
        missing = list(request.gtins)  # ICA publishes no barcodes; the service does not ask
        if request.gtins:
            collected.notes.append("ICA does not publish barcodes; map the barcode to an ICA product by hand")
        found: dict[str, dict] = {}
        if request.query:
            html = self.client.get_text(f"{BASE}/stores/{store}/search?q={quote(request.query[:100])}")
            found.update(parse_state(html))
            collected.fetched += len(found)
            if not found and embedded_state(html) is None:
                raise ExtractionError("The search page has no embedded product data")
        for sku in request.skus:
            if not SKU.match(sku):
                missing.append(sku)
                continue
            try:
                html = self.client.get_text(f"{BASE}/stores/{store}/products/{sku}")
            except HttpStatusError as exc:
                if exc.status not in (404, 410):
                    raise
                missing.append(sku)
                continue
            collected.fetched += 1
            price, offers, name = parse_page(html, sku, store)
            if price is None:
                missing.append(sku)
                collected.notes.append(f"ICA lists '{name}' without a price")
                continue
            collected.prices.append(price)
            collected.offers.extend(offers)
        for sku, raw in found.items():
            if any(p.sku == sku for p in collected.prices):
                continue
            price = parse_record(raw, store)
            if price is None:
                collected.skipped += 1
                continue
            collected.prices.append(price)
            collected.offers.extend(parse_offers(raw, store))
        collected.missing = tuple(missing)
        return collected

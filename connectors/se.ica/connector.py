"""ICA (se.ica): prices and promotions for products in ICA's online shop, for one chosen store.

ICA's online shop is an Ocado-based storefront at handlaprivatkund.ica.se/stores/<storeId>/. A
page shows products as tiles (`data-test="fop-wrapper..."`: title, price, original price, offer
text, size, a link ending in the ICA SKU). The 2026-11-26 investigation also found the data embedded
as `window.__INITIAL_STATE__` and schema.org JSON-LD; all three are read. ICA's IT team approved
requesting the pages with the browser-style headers in the manifest (see its `access_note`).

The tile layout comes from a saved copy of the store start page (2026-11-27). The search and
product pages and the embedded state are NOT verified against the live site; the state is read
tolerantly (a record is any object with a `retailerProductId`), and a page whose layout does not
match is an extraction failure, never a price. Without a chosen store ICA shows only campaign prices
(www.ica.se), so this connector always works on a store's own pages.
- Prices are per store (the `<storeId>` in the address), online prices only.
- The price of a record is the single-item price; promotions are reported as offers.
- `available` means orderable, not in stock, so availability is only taken from JSON-LD.
- No barcode is published, so barcode search is unsupported and GTIN stays a manual mapping.
"""
import html as htmllib
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


TILE = re.compile(r'(?=data-test="fop-wrapper)')
HREF = re.compile(r'href="[^"]*/products/[^"/]*/(?P<sku>[A-Za-z0-9_-]+)"')
OFFER_ID = re.compile(r'href="[^"]*/offers/[^"/]*/(?P<id>[0-9a-f-]{8,})"')
MONEY = re.compile(r"(\d[\d\s]*(?:[.,]\d+)?)\s*kr", re.IGNORECASE)


def _text(chunk: str, key: str) -> str | None:
    found = re.search(rf'data-test="{key}"[^>]*>(.*?)</(?:span|h3|div)>', chunk, re.DOTALL)
    return htmllib.unescape(re.sub(r"<[^>]+>", "", found.group(1))).replace("\xa0", " ").strip() if found else None


def _money(text: str | None) -> Decimal | None:
    found = MONEY.search((text or "").replace("\xa0", " "))
    return to_decimal(re.sub(r"\s", "", found.group(1)).replace(",", ".")) if found else None


def parse_tiles(html: str) -> dict[str, dict]:
    """Product tiles (`data-test="fop-wrapper..."`) of a store-shop page, as records by ICA SKU.

    `fop-price` is what the shopper pays; `fop-original-price` ("Ord.pris 14,90 kr") is the regular
    price when a promotion applies. A member (stammis) promotion never becomes the current price.
    """
    found: dict[str, dict] = {}
    for chunk in TILE.split(html)[1:]:
        link, price = HREF.search(chunk), _money(_text(chunk, "fop-price"))
        name = _text(chunk, "fop-title")
        if not link or not name:
            continue
        original = re.search(r'data-test="fop-original-price"[^>]*aria-label="([^"]*)"', chunk)
        regular = _money(htmllib.unescape(original.group(1))) if original else None
        offer = _text(chunk, "fop-offer-text")
        promo = OFFER_ID.search(chunk)
        record = {"retailerProductId": link.group("sku"), "name": name, "size": _text(chunk, "fop-size"), "promotions": []}
        if price is not None:
            member = bool(offer and MEMBER.search(offer))
            record["price"] = {"current": {"amount": str(regular if member and regular else price)}}
            if regular and not member:
                record["regular_price"] = str(regular)
        if offer:
            record["promotions"].append({"retailerPromotionId": promo.group("id") if promo else offer,
                                         "description": offer})
        found.setdefault(link.group("sku"), record)
    return found


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
    return {**found, **parse_tiles(html)}


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
        sku=sku, name=str(raw["name"]), current_price=amount, regular_price=_amount(raw.get("regular_price")),
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

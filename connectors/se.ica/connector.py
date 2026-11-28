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
from datetime import datetime, time
from decimal import Decimal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from app.connectors.base import (
    ExtractionError,
    HttpStatusError,
    OfferData,
    PriceData,
    ScanRequest,
    StoreInfo,
)
from app.connectors.kit import Collected, HttpJsonConnector, json_ld, to_decimal
from app.pricing import parse_size

BASE = "https://handlaprivatkund.ica.se"
FINDER = "https://www.ica.se/butiker/"
STORE_LINK = re.compile(r'href="https://www\.ica\.se/butiker/[a-z]+/[a-z0-9-]+/[a-z0-9-]+-(?P<id>\d{6,8})/"')
SHOP_LINK = re.compile(r"handlaprivatkund\.ica\.se/stores/(?P<id>\d+)")
NAME_ADDRESS = re.compile(r"<span[^>]*>(?P<name>[^<]{3,80})</span>\s*<span[^>]*>(?P<address>[^<]{3,80},\s*[^<]{2,40})</span>")
SKU = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
STATE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*")
MULTIBUY = re.compile(r"(\d+)\s*för\s*(\d+(?:[.,]\d+)?)\s*kr", re.IGNORECASE)
DATES = re.compile(r"(\d{1,2})/(\d{1,2})\s*-\s*(\d{1,2})/(\d{1,2})\s+(\d{4})")
STOCKHOLM = ZoneInfo("Europe/Stockholm")
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


def parse_period(text: str | None) -> tuple[datetime | None, datetime | None]:
    """"23/11-29/11 2026" -> (start of the first day, end of the last day), Swedish time."""
    found = DATES.search(text or "")
    if not found:
        return None, None
    d1, m1, d2, m2, year = (int(g) for g in found.groups())
    try:
        end = datetime(year, m2, d2, tzinfo=STOCKHOLM)
        start = datetime(year - (1 if m1 > m2 else 0), m1, d1, tzinfo=STOCKHOLM)
    except ValueError:
        return None, None
    return start, datetime.combine(end.date(), time(23, 59, 59), STOCKHOLM)


def _record(sku: str, name: str, size: str | None, price_text: str | None, offer: str | None,
            ordinary: Decimal | None, promo_id: str | None, loyalty: bool = False,
            period: str | None = None) -> dict:
    """One product record from the texts a store-shop page shows (a tile or a product page).

    The shown price is what the shopper pays for a single item *unless* a multibuy applies, when
    it reads "3 för 50 kr" (the bundle). A multibuy or member (stammis) promotion never becomes the
    current price: the current price is then the ordinary one, and the promotion becomes an offer.
    """
    promo_text = offer or (price_text if price_text and MULTIBUY.search(price_text) else None)
    bundle_shown = bool(price_text and MULTIBUY.search(price_text))  # "3 för 50 kr" instead of a price
    member = bool(promo_text and MEMBER.search(promo_text)) or loyalty
    record: dict = {"retailerProductId": sku, "name": name, "size": size, "promotions": []}
    current = ordinary if bundle_shown or member else _money(price_text)
    if current is not None:
        record["price"] = {"current": {"amount": str(current)}}
        if ordinary and current != ordinary:
            record["regular_price"] = str(ordinary)
    if promo_text:
        record["promotions"].append({"retailerPromotionId": promo_id or promo_text,
                                     "description": promo_text, "member": member,
                                     "period": period})
    return record


def _ordinary(chunk: str) -> Decimal | None:
    found = re.search(r'aria-label="Ordinarie Pris ([^"]*)"', chunk)
    return _money(htmllib.unescape(found.group(1))) if found else None


def parse_tiles(html: str) -> dict[str, dict]:
    """Product tiles (`data-test="fop-wrapper..."`) of a store-shop page, as records by ICA SKU."""
    found: dict[str, dict] = {}
    for chunk in TILE.split(html)[1:]:
        link, name = HREF.search(chunk), _text(chunk, "fop-title")
        if not link or not name:
            continue
        promo = OFFER_ID.search(chunk)
        found.setdefault(link.group("sku"), _record(
            link.group("sku"), name, _text(chunk, "fop-size"), _text(chunk, "fop-price"),
            _text(chunk, "fop-offer-text"), _ordinary(chunk), promo.group("id") if promo else None,
            'data-ica-class="offer-loyalty"' in chunk))
    return found


def parse_product_view(html: str, sku: str) -> dict[str, dict]:
    """The main product of a product page (`data-test="bop-view"`), as a record keyed by `sku`.

    The page address carries the SKU, the page itself does not. The related-product carousels
    further down are tiles of *other* products and are left out. Empty when there is no such view.
    """
    start = html.find('data-test="bop-view"')
    if start < 0:
        return {}
    ends = [i for i in (html.find(f'data-test="{k}"', start) for k in
                        ("related-products-carousel", "similar-products-carousel", "fop-carousel")) if i > 0]
    view = html[start:min(ends) if ends else len(html)]
    name = re.search(r"<h1[^>]*>(.*?)</h1>", view, re.DOTALL)
    name = htmllib.unescape(re.sub(r"<[^>]+>", "", name.group(1))).strip() if name else None
    if not name:
        return {}
    spans = re.search(r'data-test="size-container"[^>]*>(.*?)</div>', view, re.DOTALL)
    labels = [htmllib.unescape(t).replace("\xa0", " ").strip()
              for t in re.findall(r"<span[^>]*>(.*?)</span>", spans.group(1), re.DOTALL)] if spans else []
    size = next((label for label in labels if "kr" not in label and parse_size(label)), None)
    card = re.search(r'data-test="offer-card-promotion"[^>]*>(.*?)</a>', view, re.DOTALL)
    promo = OFFER_ID.search(view[view.find('data-test="offer-card-promotion"'):]) if card else None
    lines = [htmllib.unescape(t).replace("\xa0", " ").strip()
             for t in re.findall(r"<span[^>]*>(.*?)</span>", card.group(1), re.DOTALL)] if card else []
    # The card reads: the offer ("2 för 50 kr -- Max 1 erbj/hushåll"), its name again, then the dates.
    period = next((line for line in lines if DATES.search(line)), None)
    return {sku: _record(sku, name, size, _text(view, "price-container"), lines[0] if lines else None,
                         _ordinary(view), promo.group("id") if promo else None, period=period)}


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
            eligibility="member" if promo.get("member") or MEMBER.search(text) else "public", skus=(sku,),
            valid_from=parse_period(promo.get("period"))[0], valid_to=parse_period(promo.get("period"))[1]))
    return offers


def parse_page(html: str, sku: str, store: str) -> tuple[PriceData | None, list[OfferData], str]:
    """(price, offers, name) for one product page. Raises ExtractionError when it has no such product."""
    ld = _jsonld_product(html, sku)
    raw = parse_product_view(html, sku).get(sku) or parse_state(html).get(sku)
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


def parse_stores(html: str) -> list[StoreInfo]:
    """Stores on ICA's store finder (`www.ica.se/butiker/?q=...`) that have an online shop.

    Each result card names the store and its address, links to its page (`.../<name>-<id>/`) and,
    when it has an online shop, to `handlaprivatkund.ica.se/stores/<id>`. The id is the same in both.
    A shop link whose id is not the card's own (the page header shows the visitor's own store) is
    ignored, and a card without one is a store without online shopping.
    """
    links = [m for m in STORE_LINK.finditer(html)]
    stores: dict[str, StoreInfo] = {}
    for n, link in enumerate(links):
        store_id = link.group("id")
        if store_id in stores:
            continue
        end = next((m.start() for m in links[n + 1:] if m.group("id") != store_id), len(html))
        if not any(m.group("id") == store_id for m in SHOP_LINK.finditer(html, link.start(), end)):
            continue
        card = list(NAME_ADDRESS.finditer(html, max(0, link.start() - 2500), link.start()))
        if card:
            stores[store_id] = StoreInfo(store_id, htmllib.unescape(card[-1].group("name")).strip(),
                                         htmllib.unescape(card[-1].group("address")).strip())
    return list(stores.values())


class IcaConnector(HttpJsonConnector):
    id = "se.ica"
    store_id_pattern = r"\d{4,10}"

    def discover(self, **inputs) -> list[StoreInfo]:
        place = str(inputs.get("place") or "").strip()
        if not place:
            raise ExtractionError("Give a postcode or a town to look for ICA stores")
        stores = parse_stores(self.client.get_text(f"{FINDER}?q={quote(place[:40])}"))
        if not stores:
            raise ExtractionError("No ICA stores with online shopping were found there")
        return stores

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

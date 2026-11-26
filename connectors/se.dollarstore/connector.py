"""Dollarstore (se.dollarstore): targeted product search, offers and per-store stock from dollarstore.se.

Notes learned from the live site (2026-11-26):
- dollarstore.se is a Next.js app with no separate JSON API: each page embeds its data as "flight"
  rows (`self.__next_f.push([1, "..."])`), one `id:json` row per line. Rows are parsed as JSON and
  searched for the objects we want; nothing is scraped from the visible HTML.
- Search is `GET /sokresultat?q=...` (25 results, `productSearch.pageInfo.hasNextPage`; a `page`
  parameter changes nothing, so a search is capped at the first page). It also finds a product by its
  article number, case-insensitively, which is how a monitor looks a product up again.
- A result has `articleNumber` (the product id; the address carries it in lower case),
  `price.unitPriceIncludingVat` (the consumer price, SEK), `price.discountPriceIncludingVat`
  (always empty in what was seen), `fields.mixMatch` (the offer, "2 för 35"), `fields.brand`,
  `fields.customName` (the real name: `name` is often just a category such as "Choklad") and
  `fields.measurement` (a size such as "42g"). There is no barcode.
- The chosen store is the `selectedStore` cookie, `<name>|<StoreId>` where only the id matters
  (`|Store226` works). It changes a product's `stockStatus.inStockQuantity`, which only the product
  page carries. With no store, or an unknown one, the figure silently becomes 8000: treated as
  unknown. A store is therefore validated when it is registered, never trusted from "no results".
- Stores: `/hitta-till-butik` lists every store (id `Store226`, city, postcode, address).
"""
import json
import re
from collections.abc import Iterator
from decimal import Decimal
from urllib.parse import quote

from app.connectors.base import (
    ExtractionError,
    HttpStatusError,
    OfferData,
    PriceData,
    ScanRequest,
    StoreInfo,
)
from app.connectors.kit import Collected, HttpJsonConnector, to_decimal
from app.pricing import parse_size

BASE = "https://dollarstore.se"
SEARCH_URL = BASE + "/sokresultat"
STORES_URL = BASE + "/hitta-till-butik"
NATIONAL_DEFAULT_STOCK = 8000        # what the site reports when it has no store to answer for
MAX_STOCK_LOOKUPS = 5                # product pages read for one text search (one request each)
SKU = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")

_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
_BUNDLE = re.compile(r"^\s*(\d+)\s*f[öo]r\s*(\d+(?:[.,]\d+)?)\s*(?:kr|:-)?\s*$", re.IGNORECASE)
_SIZE_IN_NAME = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?\s*(?:kg|g|l|dl|cl|ml))(?![a-z0-9])", re.IGNORECASE)


# ---- reading the embedded data ----------------------------------------------------------------

def flight_values(html: str) -> Iterator[object]:
    """Every JSON value the page embeds, one per flight row."""
    text = "".join(json.loads(chunk) for chunk in _PUSH.findall(html))
    for line in text.split("\n"):
        _head, colon, body = line.partition(":")
        if colon and body[:1] in ("[", "{"):
            try:
                yield json.loads(body, parse_float=Decimal)
            except ValueError:
                continue


def objects(value) -> Iterator[dict]:
    """Every dict inside a JSON value, outermost first."""
    if isinstance(value, dict):
        yield value
        for inner in value.values():
            yield from objects(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from objects(inner)


def find_all(html: str, wanted) -> Iterator[dict]:
    for value in flight_values(html):
        for item in objects(value):
            if wanted(item):
                yield item


def _text(value) -> str:
    return " ".join(str(value or "").split())


def parse_offer(sku: str, title: str, mix_match: str, url: str) -> OfferData | None:
    """`fields.mixMatch` ("2 för 35") -> an offer. Wording that is not "N för P" is kept as the
    store's own text with no amount."""
    terms = _text(mix_match)
    if not terms:
        return None
    bundle = _BUNDLE.match(terms)
    price = to_decimal(bundle.group(2).replace(",", ".")) if bundle else None
    if bundle and price is not None:
        terms = f"{int(bundle.group(1))} för {format(price, 'f') if price == price.to_integral_value() else format(price.quantize(Decimal('0.01')), 'f')} kr"
    return OfferData(identity_key=f"{sku}:mixmatch", title=title, terms=terms, price=price,
                     currency="SEK", channel="online", source_url=url, eligibility="public",
                     skus=(sku,))


def stock_of(node: dict) -> tuple[str | None, int | None]:
    """(availability, quantity) from a product page's stock figure; unknown when the site gave only
    its national default."""
    status = node.get("stockStatus")
    quantity = to_decimal(status.get("inStockQuantity")) if isinstance(status, dict) else None
    if quantity is None or quantity < 0 or quantity == NATIONAL_DEFAULT_STOCK:
        return None, None
    return ("in_stock" if quantity > 0 else "out_of_stock"), int(quantity)


def parse_product(node: dict) -> tuple[PriceData, list[OfferData]] | None:
    """A search result or product page -> price and offers. None when the price is unknown."""
    number = _text(node.get("articleNumber"))
    prices = node.get("price") if isinstance(node.get("price"), dict) else {}
    price = to_decimal(prices.get("unitPriceIncludingVat"))
    currency = ((prices.get("currency") or {}).get("code") if isinstance(prices.get("currency"), dict)
                else None) or "SEK"
    if not number or not SKU.match(number) or price is None or price < 0 or currency != "SEK":
        return None
    sku = number.lower()                    # the address carries it in lower case
    fields = node.get("fields") if isinstance(node.get("fields"), dict) else {}
    # A campaign price below the shelf price is what a visitor pays now (never seen set; kept honest).
    regular = None
    discount = to_decimal(prices.get("discountPriceIncludingVat"))
    if discount is not None and 0 <= discount < price:
        regular, price = price, discount
    brand = _text(fields.get("brand")) or None
    title = _text(fields.get("customName")) or _text(node.get("name")) or number
    label = _text(fields.get("measurement")) or _text(fields.get("volume"))
    if not parse_size(label.replace(" ", "")):
        found = _SIZE_IN_NAME.search(_text(node.get("name")))
        label = found.group(1) if found else ""
    size = parse_size(label.replace(" ", "")) if label else None
    if label and label.lower() not in title.lower():
        title = f"{title} {label}"
    if brand and brand.lower() not in title.lower():
        title = f"{title} {brand}"
    url = BASE + node["url"] if str(node.get("url") or "").startswith("/") else BASE
    availability, quantity = stock_of(node)
    data = PriceData(
        sku=sku, name=title, current_price=price, regular_price=regular, currency="SEK",
        channel="online", source_url=url, gtin=None, availability=availability, brand=brand,
        package_value=size[0] if size else None, package_unit=size[1] if size else None,
        stock_quantity=quantity)
    offer = parse_offer(sku, title, fields.get("mixMatch"), url)
    return data, [offer] if offer else []


class DollarstoreConnector(HttpJsonConnector):
    id = "se.dollarstore"
    store_id_pattern = r"Store\d+"

    def source_for(self, store: str) -> str:
        return SEARCH_URL

    @staticmethod
    def _cookie(store: str) -> dict[str, str]:
        """The site's own way of saying which store a visitor shops in."""
        return {"Cookie": f"selectedStore={quote('|' + store, safe='')}"}

    # ---- stores ----------------------------------------------------------------------------
    def discover(self, place: str | None = None, **_ignored) -> list[StoreInfo]:
        """The stores on the site's store list; with `place`, the ones whose town or name contains it
        or whose postcode starts with its digits. One request."""
        html = self.client.get_text(STORES_URL)
        wanted = _text(place).casefold()
        digits = re.sub(r"\D", "", wanted)
        found: dict[str, StoreInfo] = {}
        for item in find_all(html, lambda d: d.get("__typename") == "Store" and str(d.get("id", "")).startswith("Store")):
            store_id = str(item["id"])
            if not re.fullmatch(self.store_id_pattern, store_id) or store_id in found:
                continue
            name = _text(item.get("name")) or _text(item.get("city"))
            zipcode = re.sub(r"\D", "", _text(item.get("zipCode")))
            if wanted and not (wanted in name.casefold() or wanted in _text(item.get("city")).casefold()
                               or (len(digits) >= 2 and zipcode.startswith(digits))):
                continue
            address = ", ".join(part for part in (_text(item.get("address")), _text(item.get("zipCode")),
                                                  _text(item.get("city"))) if part)
            found[store_id] = StoreInfo(external_id=store_id, name=f"Dollarstore {name}",
                                        address=address or None)
        if not found:
            raise ExtractionError("No stores found" + (f" for '{place}'" if wanted else ""))
        return list(found.values())

    # ---- products --------------------------------------------------------------------------
    def _search(self, store: str, text: str) -> tuple[list[dict], bool]:
        """(result nodes, more results exist)."""
        html = self.client.get_text(SEARCH_URL, {"q": text[:100]}, self._cookie(store))
        found = next(find_all(html, lambda d: isinstance(d.get("productSearch"), dict)), None)
        if found is None:
            raise ExtractionError("Unexpected search response")
        search = found["productSearch"]
        nodes = [n for n in search.get("nodes") or [] if isinstance(n, dict)]
        more = bool((search.get("pageInfo") or {}).get("hasNextPage"))
        return nodes, more

    def _page_stock(self, store: str, node: dict) -> dict | None:
        """The product page's own record of the product (it carries this store's stock)."""
        path = str(node.get("url") or "")
        if not path.startswith("/"):
            return None
        try:
            html = self.client.get_text(BASE + path, headers=self._cookie(store))
        except HttpStatusError as exc:
            if exc.status != 404:
                raise
            return None
        number = node.get("articleNumber")
        return next(find_all(html, lambda d: d.get("articleNumber") == number and "stockStatus" in d), None)

    def collect(self, store: str, request: ScanRequest) -> Collected:
        nodes: dict[str, dict] = {}
        missing: list[str] = []
        truncated = False
        looked_up: list[str] = []
        for sku in request.skus:
            key = sku.lower()
            if not SKU.match(key):
                missing.append(sku)
                continue
            found, _more = self._search(store, key)
            node = next((n for n in found if _text(n.get("articleNumber")).lower() == key), None)
            if node is None:
                missing.append(sku)
            else:
                nodes[key] = node
                looked_up.append(key)
        if request.query:
            found, truncated = self._search(store, request.query)
            for node in found:
                nodes.setdefault(_text(node.get("articleNumber")).lower(), node)
        # Stock is only on the product page: read it for what was asked for by id, then for the
        # first few text results, and say so when some were left unread.
        order = looked_up + [k for k in nodes if k not in looked_up]
        limit = len(looked_up) + (MAX_STOCK_LOOKUPS if request.query else 0)
        read = 0
        for key in order[:limit]:
            if self.client.budget_left() <= 0:
                break
            page = self._page_stock(store, nodes[key])
            read += 1
            if page is not None:
                nodes[key] = {**nodes[key], "stockStatus": page.get("stockStatus")}
        missing.extend(request.gtins)           # no barcode search: asked, answered "not found here"
        collected = Collected(missing=tuple(missing), truncated=truncated, fetched=len(nodes))
        if len(order) > read:
            collected.notes.append(f"stock was read for {read} of {len(order)} products")
        for node in nodes.values():
            parsed = parse_product(node)
            if not parsed:
                collected.skipped += 1
                continue
            collected.prices.append(parsed[0])
            collected.offers.extend(parsed[1])
        return collected

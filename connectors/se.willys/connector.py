"""Willys (se.willys): targeted product search and lookup through the JSON willys.se itself loads.

Notes learned from the live site (2026-11-26):
- `GET /search?q=...&size=N&page=P` (page is 0-based; `pagination.numberOfPages`) returns products
  without a barcode. A barcode as the query returns nothing, so there is no barcode search.
- `GET /axfood/rest/p/{code}` returns one product including its `ean`, so a lookup by id doubles
  as the way to learn a product's barcode.
- `priceValue` is the shelf price. Items whose code ends `_KG` are sold by weight and priced per
  kilogram (the label `ca: 140g` is one piece), so their basis is 1 kg.
- `potentialPromotions` lists campaigns. `campaignType: LOYALTY` is the member price ("Klubbpris");
  it needs a Willys Plus membership, so it is a member offer and never becomes the shelf price.
  A multi-buy ("2 för 99 kr") has `qualifyingCount` 2 and the per-item share in `price.value`.
  `validUntil` is epoch milliseconds.
- Prices are the online assortment as an anonymous visitor sees it, one national "store".
"""
import re
from datetime import UTC, datetime
from decimal import Decimal

from app.connectors.base import ExtractionError, HttpStatusError, OfferData, PriceData, ScanRequest
from app.connectors.kit import Collected, HttpJsonConnector, to_decimal, valid_gtin
from app.pricing import parse_size

BASE = "https://www.willys.se"
SEARCH_URL = BASE + "/search"
PRODUCT_URL = BASE + "/axfood/rest/p/"
PAGE_SIZE = 30
SKU = re.compile(r"^\d{6,12}_[A-Za-z]{2,3}$")
BY_WEIGHT = "_KG"


def _money(value: Decimal) -> str:
    """A whole number as is ("99"), anything else with two decimals ("12.50")."""
    return format(value.quantize(Decimal(1)) if value == value.to_integral_value() else value.quantize(Decimal("0.01")), "f")


def _datetime(millis) -> datetime | None:
    number = to_decimal(millis)
    if number is None or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(number) / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def parse_promotion(promo: dict, sku: str, title: str, url: str) -> OfferData | None:
    """One campaign -> an offer, or None when it does not describe a price we can state."""
    code = promo.get("code")
    if not code:
        return None
    member = promo.get("campaignType") == "LOYALTY"
    each = to_decimal((promo.get("price") or {}).get("value"))
    count = int(to_decimal(promo.get("qualifyingCount")) or 1)
    reward = to_decimal(str(promo.get("rewardLabel") or "").replace(",", "."))
    percent = to_decimal(promo.get("promotionPercentage"))
    if count > 1 and reward is not None:
        terms = f"{count} för {_money(reward)} kr"
    elif each is not None:
        terms = f"{_money(each)} kr"
    elif percent is not None and percent > 0:
        terms = f"{_money(percent)} % rabatt"
    else:
        return None
    if member:
        terms = "Klubbpris: " + terms
    return OfferData(identity_key=f"{sku}:{code}", title=title, terms=terms, price=each,
                     currency="SEK", channel="online", source_url=url,
                     eligibility="member" if member else "public", skus=(sku,),
                     valid_to=_datetime(promo.get("validUntil")))


def parse_product(raw: dict) -> tuple[PriceData, list[OfferData]] | None:
    """A search result or product detail -> price and offers. None when the price is unknown."""
    sku = str(raw.get("code") or "")
    price = to_decimal(raw.get("priceValue"))
    if not SKU.match(sku) or price is None or price < 0 or raw.get("online") is False:
        return None
    title = " ".join(part for part in (raw.get("name"), raw.get("manufacturer")) if part)
    url = f"{BASE}/produkt/{sku}"
    offers, regular = [], None
    for promo in raw.get("potentialPromotions") or []:
        offer = parse_promotion(promo, sku, title, url)
        if not offer:
            continue
        offers.append(offer)
        # A public single-item campaign price below the shelf price is what a visitor pays now.
        if (offer.eligibility == "public" and offer.price is not None and offer.price < price
                and int(to_decimal(promo.get("qualifyingCount")) or 1) <= 1):
            regular, price = price, offer.price
    size = (Decimal(1), "kg") if sku.upper().endswith(BY_WEIGHT) else parse_size(
        str(raw.get("displayVolume") or "").replace(" ", ""))
    availability = None if raw.get("outOfStock") is None else (
        "out_of_stock" if raw["outOfStock"] else "in_stock")
    return PriceData(
        sku=sku, name=title, current_price=price, regular_price=regular, currency="SEK",
        channel="online", source_url=url, gtin=valid_gtin(raw.get("ean")),
        availability=availability, brand=raw.get("manufacturer"),
        package_value=size[0] if size else None, package_unit=size[1] if size else None,
    ), offers


class WillysConnector(HttpJsonConnector):
    id = "se.willys"
    store_id_pattern = r"online"

    def __init__(self, manifest: dict | None = None, client=None):
        super().__init__(manifest, client)
        self.max_results = 2 * PAGE_SIZE

    def source_for(self, store: str) -> str:
        return SEARCH_URL

    def _search(self, text: str, page: int) -> dict:
        data = self.client.get_json(SEARCH_URL, {"q": text[:100], "size": PAGE_SIZE, "page": page})
        results = data.get("results") if isinstance(data, dict) else None
        if results is not None and not isinstance(results, list):
            raise ExtractionError("Unexpected search response")
        return {"results": results or [], "pages": int(
            ((data.get("pagination") or {}).get("numberOfPages")) or 1)}

    def _query(self, text: str, skip: int = 0,
               take: int | None = None) -> tuple[list[dict], bool, int | None]:
        """Results from `skip`, up to `take` (default: the cap). Willys answers in pages of PAGE_SIZE,
        so `skip` is read in whole pages and `take` rounds up to whole pages. Returns
        (products, truncated, next_skip)."""
        limit = min(take or self.max_results, self.max_results)
        page = skip // PAGE_SIZE
        found: list[dict] = []
        while self.client.budget_left() > 0 and len(found) < limit:
            result = self._search(text, page)
            found += result["results"]
            page += 1
            if page >= result["pages"] or len(result["results"]) < PAGE_SIZE:
                return found, False, None
        return found, True, page * PAGE_SIZE

    def collect(self, store: str, request: ScanRequest) -> Collected:
        raw: dict[str, dict] = {}
        missing: list[str] = []
        truncated, next_skip = False, None
        if request.query:
            products, truncated, next_skip = self._query(request.query, request.skip, request.take)
            raw.update((str(p.get("code")), p) for p in products)
        for sku in request.skus:
            if not SKU.match(sku):
                missing.append(sku)
                continue
            try:
                raw[sku] = self.client.get_json(PRODUCT_URL + sku)
            except HttpStatusError as exc:
                if exc.status != 404:
                    raise
                missing.append(sku)
        # Anonymous search cannot find a barcode, so a barcode request is answered "not found here"
        # (the service already avoids asking: the manifest says barcode_search is unsupported).
        missing.extend(request.gtins)
        collected = Collected(missing=tuple(missing), truncated=truncated, fetched=len(raw), next_skip=next_skip)
        for product in raw.values():
            parsed = parse_product(product) if isinstance(product, dict) else None
            if not parsed:
                collected.skipped += 1
                continue
            collected.prices.append(parsed[0])
            collected.offers.extend(parsed[1])
        return collected

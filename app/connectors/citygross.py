"""City Gross (se.citygross): targeted product search and lookup.

Uses the same first-party JSON API the citygross.se pages call (`/api/v1/Loop54/search` and
`/api/v1/sites`). Only the owner's chosen products are fetched: a text query, specific
product IDs, or barcodes. The store directory is a single small request.

Notes learned from the live API (2026-11-26):
- The `store` parameter is the store's `storeNumber`, not its `id`. An unknown value silently
  returns no products, so stores must be validated against the directory when registered.
- `netContent.unitOfMeasure` is 0 for both weights and volumes; package size is parsed from the
  Swedish `descriptiveSize` label instead.
- `currentPrice` does not include promotions. A public single-item promotion (`minQuantity` 1)
  sets the effective price; multi-buy and member promotions stay separate offers.
"""
from datetime import UTC, datetime
from decimal import Decimal

from app.connectors.base import (
    AccessBlocked,
    BudgetExceeded,
    ConnectorError,
    ExtractionError,
    OfferData,
    PriceData,
    RateLimited,
    ScanRequest,
    Snapshot,
    StoreInfo,
    TransientError,
)
from app.connectors.http import PoliteClient
from app.gtin import normalize_gtin
from app.pricing import parse_size

BASE = "https://www.citygross.se"
SEARCH_URL = BASE + "/api/v1/Loop54/search"
SITES_URL = BASE + "/api/v1/sites"
PAGE_SIZE = 40
STOCK_STATUS = {1: "in_stock"}  # other codes are unknown, so they stay unknown


def _decimal(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except ArithmeticError:
        return None
    return result if result.is_finite() else None


def _money(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _datetime(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value).astimezone(UTC) if value else None
    except (TypeError, ValueError):
        return None


def _gtin(value) -> str | None:
    try:
        normalize_gtin(str(value))
    except ValueError:
        return None
    return str(value)


def _gtin14(value) -> str | None:
    try:
        return normalize_gtin(str(value))
    except ValueError:
        return None


def parse_promotion(promo: dict, sku: str, title: str, url: str) -> OfferData | None:
    promo_id = promo.get("id")
    if not promo_id:
        return None
    quantity = int(promo.get("minQuantity") or 1)
    value = _decimal(promo.get("value"))
    members = bool(promo.get("membersOnly"))
    if promo.get("effectType") == "ItemsTotal" and value is not None:
        # Built from the structured fields; the API does not supply the retailer's wording.
        terms = f"{quantity} för {_money(value)} kr" if quantity > 1 else f"{_money(value)} kr"
        price = value
    else:
        terms, price = f"{promo.get('effectType')}: {promo.get('value')}", None
    if members:
        terms = "Medlemspris: " + terms
    return OfferData(identity_key=f"{sku}:{promo_id}", title=title, terms=terms, price=price,
                     currency="SEK", channel="online", source_url=url,
                     eligibility="member" if members else "public", skus=(sku,),
                     valid_from=_datetime(promo.get("from")), valid_to=_datetime(promo.get("to")))


def parse_product(raw: dict) -> tuple[PriceData, list[OfferData]] | None:
    """One search-result product -> price and offers. None when the price is unknown."""
    details = raw.get("productStoreDetails") or {}
    if not raw.get("id") or not raw.get("sellable") or not details.get("p_has_price"):
        return None
    prices = details.get("prices") or {}
    price = _decimal((prices.get("currentPrice") or {}).get("price"))
    if price is None or price < 0:
        return None
    regular = _decimal((prices.get("ordinaryPrice") or {}).get("price"))
    sku = str(raw["id"])
    title = " ".join(part for part in (raw.get("name"), raw.get("subtitle")) if part)
    url = BASE + raw["url"] if str(raw.get("url") or "").startswith("/") else BASE
    offers = []
    for promo in prices.get("promotions") or []:
        offer = parse_promotion(promo, sku, title, url)
        if not offer:
            continue
        offers.append(offer)
        single = _decimal((promo.get("priceDetails") or {}).get("price")) or offer.price
        if (offer.eligibility == "public" and int(promo.get("minQuantity") or 1) <= 1
                and promo.get("effectType") == "ItemsTotal" and single is not None
                and single < price):
            price = single
    size = parse_size(raw.get("descriptiveSize"))
    return PriceData(
        sku=sku, name=title, current_price=price, regular_price=regular, currency="SEK",
        channel="online", source_url=url, gtin=_gtin(raw.get("gtin")),
        availability=STOCK_STATUS.get(details.get("stockStatus")), brand=raw.get("brand"),
        package_value=size[0] if size else None, package_unit=size[1] if size else None,
    ), offers


class CityGrossConnector:
    id = "se.citygross"

    def __init__(self, manifest: dict | None = None, client: PoliteClient | None = None):
        manifest = manifest or {}
        self.max_requests = manifest.get("max_requests_per_scan", 6)
        self.max_results = 2 * PAGE_SIZE
        self.client = client or PoliteClient(
            manifest.get("allowed_hosts") or ["www.citygross.se"],
            manifest.get("minimum_request_interval_seconds", 3))

    def discover_stores(self) -> list[StoreInfo]:
        data = self.client.get_json(SITES_URL, {"siteTypeId": 3})
        stores = []
        for site in data.get("sites") if isinstance(data, dict) else []:
            number, name = site.get("storeNumber"), site.get("name")
            if number and name:
                address = ", ".join(str(part) for part in (site.get("streetAddress"), site.get("city"))
                                    if part)
                stores.append(StoreInfo(str(number), str(name), address or None))
        if not stores:
            raise ExtractionError("Store directory was empty")
        return stores

    def _search(self, store: str, text: str, skip: int, take: int) -> dict:
        data = self.client.get_json(SEARCH_URL, {"SearchQuery": text[:100], "skip": skip,
                                                 "take": take, "store": store})
        results = data.get("searchResults") if isinstance(data, dict) else None
        if not isinstance(results, dict) or not isinstance(results.get("products"), list):
            raise ExtractionError("Unexpected search response")
        return results

    def _query(self, store: str, text: str) -> tuple[list[dict], bool]:
        """All results up to the cap. Returns (products, truncated).

        A short page is the reliable end-of-results signal: the API's `totalCount` can overstate
        (it said 25 while returning 22 products), so it is only used to stop early.
        """
        found, skip = [], 0
        while self.client.budget_left() > 0 and len(found) < self.max_results:
            page = self._search(store, text, skip, PAGE_SIZE)
            products = page["products"]
            found += products
            skip += len(products)
            if len(products) < PAGE_SIZE or skip >= int(page.get("totalCount") or 0):
                return found, False
        return found, True

    def scan(self, external_store_id: str, channel: str,
             request: ScanRequest | None = None) -> Snapshot:
        source = f"{SEARCH_URL}?store={external_store_id}"
        if channel != "online":
            return Snapshot([], [], False, source, status="unsupported",
                            detail="Only online prices are supported")
        if request is None or not request.targeted:
            return Snapshot([], [], False, source, status="unsupported",
                            detail="Only targeted searches and lookups are supported")
        if not external_store_id.isdigit():
            return Snapshot([], [], False, source, status="invalid_data",
                            detail="Store number must be numeric")
        self.client.begin_scan(self.max_requests)
        before = self.client.requests_made
        try:
            snapshot = self._collect(external_store_id, request, source)
        except AccessBlocked as exc:
            return Snapshot([], [], False, source, status="access_blocked", detail=str(exc),
                            requests=self.client.requests_made - before)
        except RateLimited as exc:
            return Snapshot([], [], False, source, status="rate_limited",
                            detail="Retailer asked us to slow down", retry_after=exc.retry_after,
                            requests=self.client.requests_made - before)
        except BudgetExceeded as exc:
            return Snapshot([], [], False, source, status="partial", detail=str(exc),
                            requests=self.client.requests_made - before)
        except ConnectorError as exc:
            if isinstance(exc, TransientError):
                raise
            return Snapshot([], [], False, source, status="extraction_failed", detail=str(exc),
                            requests=self.client.requests_made - before)
        # TransientError (network, timeout, 5xx) propagates so the worker retries with backoff.
        return Snapshot(snapshot.prices, snapshot.offers, snapshot.complete, snapshot.source,
                        snapshot.status, snapshot.detail, snapshot.missing, snapshot.truncated,
                        True, self.client.requests_made - before)

    def _collect(self, store: str, request: ScanRequest, source: str) -> Snapshot:
        raw: dict[str, dict] = {}
        missing: list[str] = []
        truncated = False
        if request.query:
            products, truncated = self._query(store, request.query)
            raw.update((str(p.get("id")), p) for p in products)
        for sku in request.skus:
            hits = [p for p in self._search(store, sku, 0, 5)["products"] if str(p.get("id")) == sku]
            raw.update((sku, hit) for hit in hits[:1])
            if not hits:
                missing.append(sku)
        for gtin in request.gtins:
            wanted = _gtin14(gtin)
            hits = [p for p in self._search(store, gtin, 0, 5)["products"]
                    if wanted and _gtin14(p.get("gtin")) == wanted]
            raw.update((str(hit.get("id")), hit) for hit in hits[:1])
            if not hits:
                missing.append(gtin)
        prices, offers, unpriced = [], [], 0
        for product in raw.values():
            parsed = parse_product(product)
            if not parsed:
                unpriced += 1
                continue
            prices.append(parsed[0])
            offers.extend(parsed[1])
        notes = []
        if truncated:
            notes.append(f"only the first {len(raw)} results were fetched")
        if unpriced:
            notes.append(f"{unpriced} result(s) had no price and were skipped")
        detail = "; ".join(notes) or None
        if missing:
            # A missing product is unknown, not proof of discontinuation.
            status = "not_found" if not prices else "partial"
            return Snapshot(prices, offers, False, source, status, detail, tuple(missing), truncated)
        return Snapshot(prices, offers, True, source, "", detail, (), truncated)

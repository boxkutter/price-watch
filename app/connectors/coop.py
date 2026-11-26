"""Coop (se.coop): targeted product search and lookup through the API coop.se itself uses.

The data API (`external.api.coop.se`) needs the subscription key that coop.se's own pages send to
every visitor. Coop publishes no developer programme, so this is an undocumented interface and
the key is configuration (`COOP_API_KEY`), never committed. A 401/403 (for example a rotated key)
pauses the connector like any other refusal.

Notes learned from the live API (2026-11-26):
- Prices are per store, and the store is resolved from a postcode (`coopstore?postCode=`): the
  returned `code` is the `store` parameter. The default web assortment is a home-delivery store.
- The EAN is both the product id and the barcode, so a GTIN lookup is just a lookup by id, and
  one `by-id` request can fetch many products.
- Only `salesPriceData.b2cPrice` is the consumer price (`b2bPrice` is the business price).
- `availablePromotions` lists deal *types* (`MULTI_BUY_FIXED_PRICE`, ...) with no amounts, and
  the site shows no deal on those products, so no offers are produced. Deals are applied by the
  cart service, which is not part of the anonymous catalogue.
"""
import re
from decimal import Decimal

from app.config import settings
from app.connectors.base import (
    AccessBlocked,
    AuthRequired,
    BudgetExceeded,
    ConnectorError,
    ExtractionError,
    HttpStatusError,
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

BASE = "https://external.api.coop.se"
SEARCH_URL = BASE + "/personalization/search/products"
BY_ID_URL = BASE + "/personalization/search/entities/by-id"
STORE_URL = BASE + "/ecommerce/coop/coopstore"
PAGE_SIZE = 40
BY_ID_BATCH = 20
_UNITS = {"gram": "g", "g": "g", "kilogram": "kg", "kg": "kg", "milliliter": "ml", "ml": "ml",
          "centiliter": "cl", "cl": "cl", "deciliter": "dl", "dl": "dl", "liter": "l", "l": "l"}


def _decimal(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except ArithmeticError:
        return None
    return result if result.is_finite() else None


def _gtin(value) -> str | None:
    try:
        normalize_gtin(str(value))
    except ValueError:
        return None
    return str(value)


def _gtin_candidates(gtin: str) -> list[str]:
    """Ids Coop might use for a barcode: EAN-13 (the usual form), as given, and EAN-8."""
    normalized = normalize_gtin(gtin)
    return list(dict.fromkeys([normalized[1:], gtin, normalized.lstrip("0")]))


def parse_package(raw: dict) -> tuple[Decimal, str] | None:
    """Package size. The label is preferred; the numeric fields are used only for single packs."""
    label = raw.get("packageSizeInformation")
    parsed = parse_size(label)
    if parsed:
        return parsed
    if "x" in str(label or "").lower() or re.search(r"\d+\s*-?\s*pack", str(raw.get("name")), re.IGNORECASE):
        return None  # a multipack whose label we cannot read: size stays unknown
    unit = _UNITS.get(str(raw.get("packageSizeUnit") or "").lower())
    value = _decimal(raw.get("packageSize"))
    if unit and value and value > 0:
        return parse_size(f"{format(value.normalize(), 'f')}{unit}")
    return None


def parse_item(raw: dict) -> PriceData | None:
    """One product record -> price data. None when the consumer price is unknown."""
    ean = str(raw.get("ean") or raw.get("id") or "")
    price = _decimal((raw.get("salesPriceData") or {}).get("b2cPrice"))
    if not ean or price is None or price < 0:
        return None
    name, brand = str(raw.get("name") or ""), raw.get("manufacturerName")
    title = name if not brand or brand.casefold() in name.casefold() else f"{name} {brand}"
    label = raw.get("packageSizeInformation")
    if label:
        title = f"{title} {label}"
    size = parse_package(raw)
    return PriceData(
        sku=ean, name=title.strip(), current_price=price, regular_price=None, currency="SEK",
        channel="online", source_url=f"https://www.coop.se/handla/sok/?q={ean}",
        gtin=_gtin(ean),
        # `availableOnline` means orderable online; False is not evidence of being out of stock.
        availability="in_stock" if raw.get("availableOnline") is True else None,
        brand=brand, package_value=size[0] if size else None,
        package_unit=size[1] if size else None)


class CoopConnector:
    id = "se.coop"

    def __init__(self, manifest: dict | None = None, client: PoliteClient | None = None):
        manifest = manifest or {}
        self.max_requests = manifest.get("max_requests_per_scan", 6)
        self.max_results = 2 * PAGE_SIZE
        self.client = client or PoliteClient(
            manifest.get("allowed_hosts") or ["external.api.coop.se"],
            manifest.get("minimum_request_interval_seconds", 3))

    def _headers(self) -> dict:
        if not settings.coop_api_key:
            raise AuthRequired("COOP_API_KEY is not configured")
        return {"Ocp-Apim-Subscription-Key": settings.coop_api_key}

    def discover_stores(self, postcode: str | None = None) -> list[StoreInfo]:
        """Coop resolves a postcode to the store serving it; there is no global store list."""
        postcode = re.sub(r"\s+", "", postcode or "")
        if not re.fullmatch(r"\d{5}", postcode):
            raise ExtractionError("A five-digit postcode is required")
        try:
            data = self.client.get_json(STORE_URL, {"api-version": "v1", "fields": "BASIC",
                                                    "postCode": postcode}, self._headers())
        except HttpStatusError as exc:
            if exc.status == 400:
                raise ExtractionError("No Coop store serves that postcode") from exc
            raise
        if not isinstance(data, dict) or not data.get("code") or not data.get("name"):
            raise ExtractionError("Unexpected store response")
        return [StoreInfo(str(data["code"]), str(data["name"]), f"Serves postcode {postcode}")]

    def _params(self, store: str) -> dict:
        return {"api-version": "v2", "store": store, "groups": "CUSTOMER_PRIVATE", "direct": "false"}

    @staticmethod
    def _items(data) -> tuple[list[dict], int]:
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, dict) or not isinstance(results.get("items"), list):
            raise ExtractionError("Unexpected response")
        return results["items"], int(results.get("count") or 0)

    def _query(self, store: str, text: str) -> tuple[list[dict], bool]:
        """Search results up to the cap. A short page is the reliable end signal."""
        found, skip = [], 0
        while self.client.budget_left() > 0 and len(found) < self.max_results:
            body = {"query": text[:100],
                    "resultsOptions": {"skip": skip, "take": PAGE_SIZE, "sortBy": [], "facets": []},
                    "relatedResultsOptions": {"skip": 0, "take": 0},
                    "customData": {"consent": False}}  # no personalisation
            items, count = self._items(self.client.post_json(
                SEARCH_URL, body, self._params(store), self._headers()))
            found += items
            skip += len(items)
            if len(items) < PAGE_SIZE or skip >= count:
                return found, False
        return found, True

    def _by_id(self, store: str, ids: list[str]) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for start in range(0, len(ids), BY_ID_BATCH):
            items, _ = self._items(self.client.post_json(
                BY_ID_URL, ids[start:start + BY_ID_BATCH], self._params(store), self._headers()))
            found.update((str(item.get("ean") or item.get("id")), item) for item in items)
        return found

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
                            detail="Store code must be numeric")
        self.client.begin_scan(self.max_requests)
        before = self.client.requests_made

        def result(status: str, detail: str, **extra) -> Snapshot:
            return Snapshot([], [], False, source, status=status, detail=detail,
                            requests=self.client.requests_made - before, **extra)

        try:
            return self._collect(external_store_id, request, source, before)
        except AuthRequired as exc:
            return result("auth_required", str(exc))
        except AccessBlocked as exc:
            return result("access_blocked", str(exc))
        except RateLimited as exc:
            return result("rate_limited", "Retailer asked us to slow down",
                          retry_after=exc.retry_after)
        except BudgetExceeded as exc:
            return result("partial", str(exc))
        except TransientError:
            raise  # the worker retries with backoff
        except ConnectorError as exc:
            return result("extraction_failed", str(exc))

    def _collect(self, store: str, request: ScanRequest, source: str, before: int) -> Snapshot:
        raw: dict[str, dict] = {}
        missing: list[str] = []
        truncated = False
        if request.query:
            items, truncated = self._query(store, request.query)
            raw.update((str(item.get("ean") or item.get("id")), item) for item in items)
        candidates = {gtin: _gtin_candidates(gtin) for gtin in request.gtins}
        ids = list(dict.fromkeys([*request.skus, *(c for cs in candidates.values() for c in cs)]))
        if ids:
            looked_up = self._by_id(store, ids)
            for sku in request.skus:
                if sku in looked_up:
                    raw[sku] = looked_up[sku]
                else:
                    missing.append(sku)
            for gtin, options in candidates.items():
                hit = next((looked_up[o] for o in options if o in looked_up), None)
                if hit:
                    raw[str(hit.get("ean") or hit.get("id"))] = hit
                else:
                    missing.append(gtin)
        prices = [price for item in raw.values() if (price := parse_item(item))]
        unpriced = len(raw) - len(prices)
        notes = []
        if truncated:
            notes.append(f"only the first {len(raw)} results were fetched")
        if unpriced:
            notes.append(f"{unpriced} result(s) had no price and were skipped")
        detail = "; ".join(notes) or None
        requests = self.client.requests_made - before
        if missing:
            status = "not_found" if not prices else "partial"
            return Snapshot(prices, [], False, source, status, detail, tuple(missing), truncated,
                            True, requests)
        return Snapshot(prices, [], True, source, "", detail, (), truncated, True, requests)

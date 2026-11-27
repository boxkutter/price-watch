"""Authoring kit for connectors: the base class a new connector extends.

Subclass `HttpJsonConnector`, point the manifest's `implementation` at it, and implement:

    collect(store, request) -> Collected     fetch and parse what the request asks for
    discover(**inputs)      -> list[StoreInfo]   (optional) look up stores

Everything else is provided: argument checks, the per-scan request budget, mapping every failure
to the standard result statuses, request counting, and secret handling. See docs/connectors.md.
"""
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal

from app.connectors.base import (
    AccessBlocked,
    AuthRequired,
    BudgetExceeded,
    ConnectorError,
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


def to_decimal(value) -> Decimal | None:
    """A finite Decimal from a JSON number or string, else None (unknown is never zero)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except ArithmeticError:
        return None
    return result if result.is_finite() else None


def valid_gtin(value) -> str | None:
    """The value unchanged if it is a GTIN with a valid check digit, else None."""
    try:
        normalize_gtin(str(value))
    except ValueError:
        return None
    return str(value)


_JSON_LD = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                      re.DOTALL | re.IGNORECASE)


def json_ld(html: str) -> list[dict]:
    """Every schema.org JSON-LD object embedded in a page (blocks that are not valid JSON are
    skipped; `@graph` and lists are flattened). For connectors whose store only publishes pages."""
    found: list[dict] = []

    def add(item) -> None:
        if isinstance(item, list):
            for entry in item:
                add(entry)
        elif isinstance(item, dict):
            found.append(item)
            add(item.get("@graph"))

    for block in _JSON_LD.findall(html):
        try:
            add(json.loads(block))
        except ValueError:
            continue
    return found


@dataclass
class Collected:
    """What `collect()` returns."""
    prices: list[PriceData] = field(default_factory=list)
    offers: list[OfferData] = field(default_factory=list)
    missing: tuple[str, ...] = ()  # requested SKUs/GTINs the retailer did not return
    truncated: bool = False        # more results exist than were fetched
    fetched: int = 0               # raw items fetched (for the truncation note)
    skipped: int = 0               # items dropped because their price was unknown
    next_skip: int | None = None   # where the next page of results starts, when there is one
    notes: list[str] = field(default_factory=list)  # why something was left out, shown to the person


class HttpJsonConnector:
    id: str = ""
    channels: tuple[str, ...] = ("online",)
    store_id_pattern: str = r"\d+"

    def __init__(self, manifest: dict | None = None, client: PoliteClient | None = None):
        self.manifest = manifest or {}
        if self.manifest.get("id"):
            self.id = self.manifest["id"]
        self.config: dict[str, str] = {}
        self.max_requests = self.manifest.get("max_requests_per_scan", 6)
        self.client = client or PoliteClient(
            self.manifest.get("allowed_hosts") or [],
            self.manifest.get("minimum_request_interval_seconds", 3),
            follow_redirects=bool(self.manifest.get("follow_redirects")),
            headers=self.manifest.get("request_headers") or None)

    # ---- configuration -------------------------------------------------------------------
    def configure(self, values: dict[str, str]) -> None:
        """Called by the service before each scan with the settings the manifest declares."""
        self.config = dict(values)

    def require(self, name: str) -> str:
        value = self.config.get(name)
        if not value:
            raise AuthRequired(f"{name} is not configured")
        return value

    # ---- to implement --------------------------------------------------------------------
    def collect(self, store: str, request: ScanRequest) -> Collected:
        raise NotImplementedError

    def discover(self, **inputs) -> list[StoreInfo]:
        raise ConnectorError("This connector does not support store discovery")

    def source_for(self, store: str) -> str:
        return f"{self.id}://stores/{store}"

    # ---- provided ------------------------------------------------------------------------
    def discover_stores(self, **inputs) -> list[StoreInfo]:
        return self.discover(**inputs)

    def scan(self, external_store_id: str, channel: str,
             request: ScanRequest | None = None) -> Snapshot:
        source = self.source_for(external_store_id)
        if channel not in self.channels:
            return Snapshot([], [], False, source, status="unsupported",
                            detail=f"Only {', '.join(self.channels)} prices are supported")
        if request is None or not request.targeted:
            return Snapshot([], [], False, source, status="unsupported",
                            detail="Only targeted searches and lookups are supported")
        if not re.fullmatch(self.store_id_pattern, external_store_id):
            return Snapshot([], [], False, source, status="invalid_data",
                            detail="Store identifier has an unexpected format")
        self.client.begin_scan(self.max_requests)
        before = self.client.requests_made

        def failed(status: str, detail: str, **extra) -> Snapshot:
            return Snapshot([], [], False, source, status=status, detail=detail,
                            requests=self.client.requests_made - before, **extra)

        try:
            collected = self.collect(external_store_id, request)
        except AuthRequired as exc:
            return failed("auth_required", str(exc))
        except AccessBlocked as exc:
            return failed("access_blocked", str(exc))
        except RateLimited as exc:
            return failed("rate_limited", "Retailer asked us to slow down",
                          retry_after=exc.retry_after)
        except BudgetExceeded as exc:
            return failed("partial", str(exc))
        except TransientError:
            raise  # the worker retries with backoff
        except ConnectorError as exc:
            return failed("extraction_failed", str(exc))
        return self._snapshot(collected, source, self.client.requests_made - before)

    @staticmethod
    def _snapshot(collected: Collected, source: str, requests: int) -> Snapshot:
        notes = []
        if collected.truncated:
            notes.append(f"only the first {collected.fetched} results were fetched")
        if collected.skipped:
            notes.append(f"{collected.skipped} result(s) had no price and were skipped")
        notes += collected.notes
        detail = "; ".join(notes) or None
        if collected.missing:
            # A missing product is unknown, not proof of discontinuation.
            status = "not_found" if not collected.prices else "partial"
            return Snapshot(collected.prices, collected.offers, False, source, status, detail,
                            tuple(collected.missing), collected.truncated, True, requests,
                            next_skip=collected.next_skip)
        return Snapshot(collected.prices, collected.offers, True, source, "", detail, (),
                        collected.truncated, True, requests, next_skip=collected.next_skip)

"""Retailer connector protocol (RCP), version 1.

A connector turns a *targeted* request (a search text, specific SKUs or barcodes) into a
typed snapshot. Live connectors never enumerate a store's catalogue or offer list; see the
README's access policy.
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

# Standard result statuses (README section 5). `success` and `partial` are derived from
# `Snapshot.complete` when a connector does not set a status explicitly.
STATUSES = {"success", "partial", "not_found", "unavailable", "unsupported", "auth_required",
            "rate_limited", "access_blocked", "extraction_failed", "network_error", "timeout",
            "invalid_data"}


class ConnectorError(Exception):
    """Base class for failures a connector reports instead of returning data."""


class TransientError(ConnectorError):
    """Network failure, timeout or 5xx. The worker retries the job with backoff."""


class AccessBlocked(ConnectorError):
    """The retailer refused the client (401/403/redirect to a challenge). Never retried or
    worked around; the connector is paused until the owner reviews it."""


class AuthRequired(ConnectorError):
    """The connector needs configuration (such as an API key) before it can run."""


class BudgetExceeded(ConnectorError):
    """The per-scan request budget ran out; the scan returns what it has as partial."""


class RateLimited(ConnectorError):
    def __init__(self, retry_after: int | None = None):
        super().__init__("Rate limited")
        self.retry_after = retry_after


class ExtractionError(ConnectorError):
    """The response was not in the expected shape."""


class HttpStatusError(ExtractionError):
    """An unexpected HTTP status that is neither a refusal nor a server fault (e.g. 400, 404)."""

    def __init__(self, status: int):
        super().__init__(f"Unexpected HTTP {status}")
        self.status = status


@dataclass(frozen=True)
class ScanRequest:
    """What to retrieve. An empty request means 'everything', which live connectors refuse."""
    query: str | None = None
    skus: tuple[str, ...] = ()
    gtins: tuple[str, ...] = ()

    @property
    def targeted(self) -> bool:
        return bool(self.query or self.skus or self.gtins)


@dataclass(frozen=True)
class PriceData:
    sku: str
    name: str
    current_price: Decimal
    regular_price: Decimal | None
    currency: str
    channel: str
    source_url: str
    gtin: str | None = None
    availability: str | None = None
    brand: str | None = None
    package_value: Decimal | None = None
    package_unit: str | None = None
    # How many the store says it has, when it says (a per-store figure, not stored: it is shown in
    # search results). `availability` stays the stored, comparable fact.
    stock_quantity: int | None = None


@dataclass(frozen=True)
class OfferData:
    identity_key: str
    title: str
    terms: str
    price: Decimal | None
    currency: str
    channel: str
    source_url: str
    eligibility: str = "public"
    skus: tuple[str, ...] = ()  # products this offer applies to
    valid_from: datetime | None = None
    valid_to: datetime | None = None


@dataclass(frozen=True)
class StoreInfo:
    external_id: str
    name: str
    address: str | None = None


@dataclass(frozen=True)
class Snapshot:
    prices: list[PriceData]
    offers: list[OfferData]
    complete: bool
    source: str
    status: str = ""  # one of STATUSES; derived from `complete` when empty
    detail: str | None = None
    missing: tuple[str, ...] = field(default=())  # requested SKUs/GTINs the retailer did not return
    truncated: bool = False  # more results exist than were fetched
    targeted: bool = False  # items were already filtered to the request by the connector
    requests: int = 0  # HTTP requests made, for the per-retailer budget
    retry_after: int | None = None  # seconds, when the retailer asked us to back off

    @property
    def effective_status(self) -> str:
        return self.status or ("success" if self.complete else "partial")


class Connector(Protocol):
    id: str

    def scan(self, external_store_id: str, channel: str,
             request: ScanRequest | None = None) -> Snapshot: ...

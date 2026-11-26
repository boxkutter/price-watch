from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


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


@dataclass(frozen=True)
class Snapshot:
    prices: list[PriceData]
    offers: list[OfferData]
    complete: bool
    source: str


class Connector(Protocol):
    id: str
    def scan(self, external_store_id: str, channel: str) -> Snapshot: ...

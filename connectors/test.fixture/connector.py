"""Deterministic sample data for end-to-end testing. Never presented as live retail data."""
from decimal import Decimal

from app.connectors.base import OfferData, PriceData, Snapshot


class FixtureConnector:
    id = "test.fixture"

    def scan(self, external_store_id: str, channel: str, request=None) -> Snapshot:
        if external_store_id != "demo" or channel != "online":
            return Snapshot([], [], False, "fixture://unsupported-scope")
        return Snapshot(
            prices=[PriceData("oats-1kg", "Demo oats 1 kg", Decimal("24.90"),
                              Decimal("29.90"), "SEK", "online", "fixture://oats",
                              availability="in_stock", brand="Demo",
                              package_value=Decimal(1), package_unit="kg")],
            offers=[OfferData("oats-week", "Demo oats offer", "1 kg package",
                              Decimal("24.90"), "SEK", "online", "fixture://offer")],
            complete=True, source="fixture://demo",
        )

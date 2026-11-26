"""A synthetic store whose prices move over time. Nothing here touches a network.

Prices follow a wave (about two and a half minutes per cycle), each product with its own phase, so checking a
monitor a minute apart shows price drops, rises and alerts. Two stores (north and south)
sell the same products, the south one about 6% cheaper, and two products carry barcodes, so
comparing by barcode across the stores works too. The oats have a multi-buy offer that switches
on and off every minute.
"""
import math
import time
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from app.connectors.base import OfferData, PriceData, ScanRequest, Snapshot
from app.gtin import normalize_gtin


def _ean13(first12: str) -> str:
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(first12))
    return first12 + str((10 - total % 10) % 10)


# (sku, name, brand, base price, package value, package unit, barcode or None)
PRODUCTS = [
    ("oats-1kg", "Demo oats 1 kg", "Demo", Decimal("24.90"), Decimal(1), "kg", _ean13("999999000001")),
    ("coffee-450g", "Demo coffee 450 g", "Demo", Decimal("69.00"), Decimal(450), "g", _ean13("999999000002")),
    ("milk-1l", "Demo milk 1 L", "Demo", Decimal("17.50"), Decimal(1), "L", None),
]
STORE_FACTOR = {"north": Decimal("1.00"), "south": Decimal("0.94")}
PERIOD_SECONDS = 150
OFFER_WINDOW_SECONDS = 60


class DemoShopConnector:
    id = "test.demoshop"

    def __init__(self, clock=time.time):
        self.clock = clock  # tests replace it to move time

    def _price(self, index: int, base: Decimal, store: str, now: float) -> Decimal:
        wave = math.sin(2 * math.pi * now / PERIOD_SECONDS + index * 2.1)
        raw = base * STORE_FACTOR[store] * Decimal(str(round(1 + 0.12 * wave, 4)))
        return (raw / Decimal("0.05")).quantize(Decimal(1), rounding=ROUND_HALF_UP) * Decimal("0.05")

    def scan(self, external_store_id: str, channel: str, request: ScanRequest | None = None) -> Snapshot:
        source = f"demo://shop/{external_store_id}"
        if channel != "online" or external_store_id not in STORE_FACTOR:
            return Snapshot([], [], False, source, status="unsupported",
                            detail="Unknown demo store or channel")
        if request is None or not request.targeted:
            return Snapshot([], [], False, source, status="unsupported",
                            detail="Only targeted searches and lookups are supported")
        wanted_gtins = {}
        for gtin in request.gtins:
            try:
                wanted_gtins[normalize_gtin(gtin)] = gtin
            except ValueError:
                pass
        query = (request.query or "").casefold().strip()
        now = self.clock()
        prices, offers, found, found_gtins = [], [], set(), set()
        for index, (sku, name, brand, base, size, unit, gtin) in enumerate(PRODUCTS):
            hit = ((query and query in name.casefold()) or sku in request.skus
                   or (gtin and normalize_gtin(gtin) in wanted_gtins))
            if not hit:
                continue
            found.add(sku)
            if gtin and normalize_gtin(gtin) in wanted_gtins:
                found_gtins.add(normalize_gtin(gtin))
            price = self._price(index, base, external_store_id, now)
            url = f"https://demo.example.org/p/{sku}"
            prices.append(PriceData(sku, name, price, base, "SEK", "online", url, gtin=gtin,
                                    availability="in_stock", brand=brand,
                                    package_value=size, package_unit=unit))
            if sku == "oats-1kg" and int(now // OFFER_WINDOW_SECONDS) % 2 == 0:
                start = int(now // OFFER_WINDOW_SECONDS) * OFFER_WINDOW_SECONDS
                offers.append(OfferData(
                    f"{sku}:multibuy-{external_store_id}", name, "2 för 45 kr", Decimal(45), "SEK",
                    "online", url, skus=(sku,),
                    valid_from=datetime.fromtimestamp(start, UTC),
                    valid_to=datetime.fromtimestamp(start + OFFER_WINDOW_SECONDS, UTC)))
        missing = tuple(sku for sku in request.skus if sku not in found) + tuple(
            given for gtin14, given in wanted_gtins.items() if gtin14 not in found_gtins) + tuple(
            gtin for gtin in request.gtins if gtin not in wanted_gtins.values())
        status = ("not_found" if missing and not prices else "partial" if missing else "")
        return Snapshot(prices, offers, not missing, source, status, None, missing, False, True, 0)

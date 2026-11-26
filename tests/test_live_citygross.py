"""Opt-in smoke test against the real citygross.se. Skipped unless RUN_LIVE=1.

Makes about six requests, spaced by the connector's own 3 second limiter. Run it by hand, not in CI:
    RUN_LIVE=1 uv run pytest tests/test_live_citygross.py -s
"""
import os

import pytest

from app.connectors.base import ScanRequest
from app.connectors.registry import CONNECTORS

pytestmark = pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="set RUN_LIVE=1 to run")

AXA_SKU, AXA_GTIN = "100132321_ST", "7310130321085"


def test_live_directory_search_sku_and_barcode():
    connector = CONNECTORS["se.citygross"]
    stores = {s.external_id: s for s in connector.discover_stores()}
    assert len(stores) > 20 and stores["3204"].name == "City Gross Borås"

    found = connector.scan("3204", "online", ScanRequest(query="havregryn"))
    assert found.effective_status == "success" and found.prices, found.detail
    assert all(p.currency == "SEK" and p.current_price > 0 for p in found.prices)
    print(f"\nsearch: {len(found.prices)} products, {len(found.offers)} offers, "
          f"truncated={found.truncated}, requests={found.requests}")

    by_sku = connector.scan("3204", "online", ScanRequest(skus=(AXA_SKU,)))
    by_gtin = connector.scan("3204", "online", ScanRequest(gtins=(AXA_GTIN,)))
    for snapshot in (by_sku, by_gtin):
        assert snapshot.effective_status == "success", snapshot.detail
        (product,) = snapshot.prices
        assert product.sku == AXA_SKU and product.gtin == AXA_GTIN
    print("axa havregryn:", by_sku.prices[0].name, by_sku.prices[0].current_price, "SEK")

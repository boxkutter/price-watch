"""Opt-in smoke test against the real Coop API. Skipped unless RUN_LIVE=1 and COOP_API_KEY is set.

Makes about four requests, spaced by the connector's own 3 second limiter. Run it by hand:
    RUN_LIVE=1 COOP_API_KEY=... uv run pytest tests/test_live_coop.py -s
"""
import os

import pytest

from app.connectors.base import ScanRequest
from app.connectors.registry import CONNECTORS

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE") != "1" or not os.environ.get("COOP_API_KEY"),
    reason="set RUN_LIVE=1 and COOP_API_KEY to run")

EAN = "7300156599452"  # Coop Havregryn 1500 g


def test_live_postcode_search_and_lookup():
    connector = CONNECTORS["se.coop"]
    (store,) = connector.discover_stores("11120")  # neutral central-Stockholm postcode
    assert store.external_id.isdigit() and store.name

    found = connector.scan(store.external_id, "online", ScanRequest(query="havregryn"))
    assert found.effective_status == "success" and found.prices, found.detail
    assert all(p.currency == "SEK" and p.current_price > 0 for p in found.prices)
    print(f"\nstore: {store.external_id} {store.name}; search: {len(found.prices)} products, "
          f"truncated={found.truncated}, requests={found.requests}")

    looked_up = connector.scan(store.external_id, "online", ScanRequest(skus=(EAN,), gtins=(EAN,)))
    assert looked_up.effective_status == "success", looked_up.detail
    (product,) = looked_up.prices
    assert product.sku == EAN and product.gtin == EAN and looked_up.requests == 1
    print("lookup:", product.name, product.current_price, "SEK", product.package_value, product.package_unit)

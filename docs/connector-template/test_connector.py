"""Tests for the template connector. Run them from this folder:  pytest -q

`check_connector` is the contract every connector must pass; the other tests cover parsing.
"""
import json
from decimal import Decimal
from pathlib import Path

import httpx
from connector import ExampleShopConnector, parse_product

from app.connectors.base import ScanRequest
from app.connectors.contract import check_connector, client_for
from app.connectors.manifest import load_manifest

manifest = load_manifest(Path(__file__).parent / "manifest.yaml")
OATS = {"sku": "A1", "name": "Oat flakes 1 kg", "price": "24.90", "regular_price": "29.90",
        "currency": "SEK", "size": "1 kg", "ean": "7310130321085", "in_stock": True, "brand": "Demo"}


def fake_shop(request: httpx.Request) -> httpx.Response:
    """A stand-in for the real site. Answers only what the connector is allowed to ask."""
    path, params = request.url.path, dict(request.url.params)
    assert request.headers["authorization"].startswith("Bearer ")
    if path == "/v1/stores":
        return httpx.Response(200, json={"stores": [{"id": 12, "name": "Example Central"}]})
    if path == "/v1/products":
        hits = [OATS] if params.get("q") == "oat" or params.get("ean") == OATS["ean"] else []
        return httpx.Response(200, json={"products": hits})
    if path == "/v1/products/A1":
        return httpx.Response(200, json=OATS)
    return httpx.Response(404)


def make(transport, secrets):
    connector = ExampleShopConnector(manifest.model_dump(), client_for(manifest, transport))
    connector.configure(secrets)
    return connector


def test_the_connector_passes_the_contract():
    passed = check_connector(make, fake_shop, manifest=manifest, store="12",
                             request=ScanRequest(query="oat"),
                             secrets={"EXAMPLESHOP_TOKEN": "s3cret-token"})
    assert "happy_path" in passed and "secrets_stay_secret" in passed


def test_a_product_is_parsed_with_exact_decimals():
    price = parse_product(json.loads(json.dumps(OATS)))
    assert price.current_price == Decimal("24.90") and price.regular_price == Decimal("29.90")
    assert (price.package_value, price.package_unit) == (Decimal(1), "kg")
    assert price.gtin == "7310130321085" and price.availability == "in_stock"


def test_an_unknown_price_is_skipped_not_zero():
    assert parse_product({**OATS, "price": None}) is None
    assert parse_product({**OATS, "price": "not a number"}) is None


def test_lookups_by_sku_and_barcode_and_an_unknown_sku_is_missing_not_discontinued():
    connector = make(httpx.MockTransport(fake_shop), {"EXAMPLESHOP_TOKEN": "t"})
    found = connector.scan("12", "online", ScanRequest(skus=("A1",), gtins=(OATS["ean"],)))
    assert [p.sku for p in found.prices] == ["A1"] and found.effective_status == "success"
    gone = connector.scan("12", "online", ScanRequest(skus=("A1", "ZZ")))
    assert gone.effective_status == "partial" and gone.missing == ("ZZ",)

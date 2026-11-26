"""The connector contract kit: built-in connectors pass it, and bad connectors fail it."""
from dataclasses import replace
from decimal import Decimal

import pytest

from app.connectors.base import AccessBlocked, ScanRequest
from app.connectors.builtin.citygross.connector import CityGrossConnector
from app.connectors.builtin.coop.connector import CoopConnector
from app.connectors.contract import check_connector, client_for
from app.connectors.manifest import load_manifest
from app.connectors.registry import BUILTIN_DIR
from tests.test_citygross import FakeSite
from tests.test_coop import KEY, FakeCoop

ALL_CHECKS = ["identity", "targeted_only", "happy_path", "allowed_hosts_only", "refusals",
              "bad_payloads", "transient_errors"]


def test_city_gross_passes_the_contract():
    manifest = load_manifest(BUILTIN_DIR / "citygross" / "manifest.yaml")

    def make(transport, secrets):
        return CityGrossConnector(manifest.model_dump(), client_for(manifest, transport))

    passed = check_connector(make, FakeSite(), manifest=manifest, store="3204",
                             request=ScanRequest(query="havregryn"))
    assert passed == [*ALL_CHECKS, "store_discovery"]


def test_coop_passes_the_contract_including_secret_handling():
    manifest = load_manifest(BUILTIN_DIR / "coop" / "manifest.yaml")

    def make(transport, secrets):
        connector = CoopConnector(manifest.model_dump(), client_for(manifest, transport))
        connector.configure(secrets)
        return connector

    passed = check_connector(make, FakeCoop(), manifest=manifest, store="255700",
                             request=ScanRequest(query="havregryn"), secrets={"COOP_API_KEY": KEY},
                             discover_inputs={"postcode": "11120"})
    assert {"needs_configuration", "secrets_stay_secret", "store_discovery"} <= set(passed)


# ---- the kit must catch bad connectors ------------------------------------------------------

MANIFEST = load_manifest(BUILTIN_DIR / "citygross" / "manifest.yaml")
SITES = "https://www.citygross.se/api/v1/sites"


def run(connector_class, **extra):
    def make(transport, secrets):
        connector = connector_class(MANIFEST.model_dump(), client_for(MANIFEST, transport))
        connector.configure(secrets)
        return connector

    return check_connector(make, FakeSite(), manifest=MANIFEST, store="3204",
                           request=ScanRequest(query="havregryn"), **extra)


class Enumerates(CityGrossConnector):
    """Fetches something even when nothing specific was asked for."""

    def scan(self, external_store_id, channel, request=None):
        self.client.get_json(SITES, {"siteTypeId": 3})
        return super().scan(external_store_id, channel, request)


class Retries(CityGrossConnector):
    """Tries again after a refusal instead of reporting it."""

    def collect(self, store, request):
        try:
            return super().collect(store, request)
        except AccessBlocked:
            return super().collect(store, request)


class WrongHost(CityGrossConnector):
    def collect(self, store, request):
        self.client.allowed_hosts.append("elsewhere.example.org")  # a connector widening its own list
        self.client.get_json("https://elsewhere.example.org/api/v1/sites", {"siteTypeId": 3})
        return super().collect(store, request)


class SecretInUrl(CityGrossConnector):
    def collect(self, store, request):
        self.client.get_json(SITES, {"siteTypeId": 3, "token": self.require("TOK")})
        return super().collect(store, request)


class WrongId(CityGrossConnector):
    def __init__(self, manifest, client):
        super().__init__(manifest, client)
        self.id = "se.other"


class UnknownPriceAsNaN(CityGrossConnector):
    def collect(self, store, request):
        collected = super().collect(store, request)
        collected.prices = [replace(p, current_price=Decimal("NaN")) for p in collected.prices]
        return collected


@pytest.mark.parametrize("cls,fragment,extra", [
    (Enumerates, "untargeted scan must not contact", {}),
    (Retries, "must not be retried", {}),
    (WrongHost, "outside allowed_hosts", {}),
    (SecretInUrl, "sent in a URL", {"secrets": {"TOK": "s3cret-value"}}),
    (WrongId, "must equal the manifest id", {}),
    (UnknownPriceAsNaN, "finite", {}),
])
def test_the_kit_rejects_misbehaving_connectors(cls, fragment, extra):
    with pytest.raises(AssertionError, match=fragment):
        run(cls, **extra)

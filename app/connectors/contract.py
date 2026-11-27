"""Conformance suite for connectors.

An author supplies a fake site (an `httpx` handler that answers the connector's happy path) and a
factory that builds the connector against any transport. `check_connector` then runs the standard
checks every connector must pass, in plain asserts, so it works under pytest or on its own:

    def test_contract():
        check_connector(make, handler, manifest=manifest, store="123",
                        request=ScanRequest(query="milk"), secrets={"MY_API_KEY": "s3cret"})

See docs/connectors.md and connectors/_template/.
"""
from collections.abc import Callable
from decimal import Decimal

import httpx

from app.connectors.base import AccessBlocked, ScanRequest, TransientError
from app.connectors.http import PoliteClient, host_allowed
from app.connectors.manifest import Manifest

STATUSES_FOR_DATA = {"success", "partial", "not_found"}


class _Site:
    """Wraps the author's handler: records requests and lets a check override the response."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]):
        self.handler, self.override = handler, None
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return (self.override or self.handler)(request)


def client_for(manifest: Manifest, transport: httpx.BaseTransport) -> PoliteClient:
    """A client that honours the manifest's host allowlist but never sleeps; for factories."""
    return PoliteClient(manifest.allowed_hosts or ["shop.example.org"], 0, transport=transport,
                        headers=manifest.request_headers or None,
                        follow_redirects=manifest.follow_redirects)


def check_connector(make: Callable[[httpx.BaseTransport, dict], object],
                    handler: Callable[[httpx.Request], httpx.Response], *, manifest: Manifest,
                    store: str, request: ScanRequest, secrets: dict[str, str] | None = None,
                    discover_inputs: dict | None = None) -> list[str]:
    """Run every contract check. Returns the names of the checks that passed; raises
    AssertionError, naming the rule, at the first one that does not."""
    secrets = secrets or {}
    passed: list[str] = []

    def build() -> tuple[object, _Site]:
        site = _Site(handler)
        return make(httpx.MockTransport(site), secrets), site

    def ok(name: str) -> None:
        passed.append(name)

    # 1. Identity and shape
    connector, site = build()
    assert connector.id == manifest.id, f"connector.id {connector.id!r} must equal the manifest id"
    assert callable(getattr(connector, "scan", None)), "a connector must define scan()"
    ok("identity")

    # 2. A connector never enumerates: untargeted and unsupported-channel scans make no request
    for untargeted in (None, ScanRequest()):
        snapshot = connector.scan(store, "online", untargeted)
        assert snapshot.effective_status == "unsupported", "untargeted scans must be 'unsupported'"
    assert site.calls == [], "an untargeted scan must not contact the retailer"
    if manifest.capabilities.get("physical_store_prices") != "supported":
        assert connector.scan(store, "physical", request).effective_status == "unsupported"
        assert site.calls == [], "an unsupported channel must not contact the retailer"
    ok("targeted_only")

    # 3. Required settings missing -> a configuration status, no request
    required = [spec.name for spec in manifest.settings if spec.required]
    if required:
        bare, bare_site = make(httpx.MockTransport(_Site(handler)), {}), None
        assert bare.scan(store, "online", request).effective_status == "auth_required", \
            "a connector without its required settings must report 'auth_required'"
        ok("needs_configuration")
        del bare, bare_site

    # 4. The happy path
    snapshot = connector.scan(store, "online", request)
    assert snapshot.effective_status in STATUSES_FOR_DATA, \
        f"the fake site's happy path returned {snapshot.effective_status}: {snapshot.detail}"
    assert snapshot.targeted, "set Snapshot.targeted (the HttpJsonConnector kit does)"
    assert snapshot.requests == len(site.calls) and snapshot.requests >= 1, \
        "Snapshot.requests must count the HTTP requests made"
    skus = [price.sku for price in snapshot.prices]
    assert snapshot.prices, "the happy path should return at least one price"
    assert len(skus) == len(set(skus)), "a snapshot must not repeat a SKU"
    for price in snapshot.prices:
        assert price.sku and isinstance(price.sku, str)
        assert isinstance(price.current_price, Decimal) and price.current_price.is_finite() \
            and price.current_price >= 0, "prices are finite non-negative Decimals (unknown is skipped)"
        assert len(price.currency) == 3 and price.channel == "online"
    ok("happy_path")

    # 4b. Only allowed hosts are ever contacted, over HTTPS
    for call in site.calls:
        assert call.url.scheme == "https" and host_allowed(call.url.host, manifest.allowed_hosts), \
            f"request to {call.url.host} is outside allowed_hosts {manifest.allowed_hosts}"
    ok("allowed_hosts_only")

    # 5. Refusals are reported, once, and never retried or disguised
    for status, expected, headers in ((401, "access_blocked", {}), (403, "access_blocked", {}),
                                      (302, "access_blocked", {"Location": "https://x.example.org/"}),
                                      (429, "rate_limited", {"Retry-After": "42"})):
        connector, site = build()
        site.override = lambda r, s=status, h=headers: httpx.Response(s, headers=h)
        result = connector.scan(store, "online", request)
        assert result.effective_status == expected, f"HTTP {status} must map to {expected}"
        assert len(site.calls) == 1, f"HTTP {status} must not be retried"
        if status == 429:
            assert result.retry_after == 42, "Retry-After must be passed through"
    ok("refusals")

    # 6. Garbage is an extraction failure, not a crash
    connector, site = build()
    site.override = lambda r: httpx.Response(200, content=b"<html>not json</html>")
    assert connector.scan(store, "online", request).effective_status == "extraction_failed"
    ok("bad_payloads")

    # 7. Server faults raise so the worker retries with backoff
    for status in (500, 503):
        connector, site = build()
        site.override = lambda r, s=status: httpx.Response(s)
        try:
            connector.scan(store, "online", request)
        except TransientError:
            pass
        else:
            raise AssertionError(f"HTTP {status} must raise TransientError")

    def timeout(_request):
        raise httpx.ReadTimeout("slow")

    connector, site = build()
    site.override = timeout
    try:
        connector.scan(store, "online", request)
    except TransientError:
        pass
    else:
        raise AssertionError("a timeout must raise TransientError")
    ok("transient_errors")

    # 8. Secrets never appear in results
    if secrets:
        for override in (None, lambda r: httpx.Response(403), lambda r: httpx.Response(200, content=b"x")):
            connector, site = build()
            site.override = override
            result = connector.scan(store, "online", request)
            for value in secrets.values():
                assert value not in repr(result), "a secret leaked into the result"
                assert all(value not in str(call.url) for call in site.calls), \
                    "a secret was sent in a URL; use a header"
        ok("secrets_stay_secret")

    # 8b. Product links, when declared: every rule reads its own real example and nothing else
    if manifest.product_urls:
        from app.connectors.urls import match_product_url
        rules = [{"pattern": rule.pattern} for rule in manifest.product_urls]
        for rule in manifest.product_urls:
            assert rule.example, "give every product_urls rule a real example link"
            assert match_product_url(rules, rule.example), f"product_urls cannot read {rule.example}"
        for unrelated in ("https://example.org/", "https://www.google.com/search?q=milk"):
            assert match_product_url(rules, unrelated) is None, \
                "a product_urls pattern is too loose: it accepts an unrelated link"
        ok("product_urls")

    # 9. Store discovery, when declared
    if manifest.capabilities.get("store_discovery") == "supported":
        connector, site = build()
        stores = connector.discover_stores(**(discover_inputs or {}))
        assert stores and all(s.external_id and s.name for s in stores), \
            "discover_stores() must return StoreInfo with an external_id and a name"
        connector, site = build()
        site.override = lambda r: httpx.Response(403)
        try:
            connector.discover_stores(**(discover_inputs or {}))
        except AccessBlocked:
            pass
        else:
            raise AssertionError("a refused store lookup must raise AccessBlocked")
        ok("store_discovery")
    return passed

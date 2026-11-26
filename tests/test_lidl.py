"""Lidl connector: product pages, no price shown, redirects, and capability gating in the service."""
import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from app import retrieval
from app.connectors import registry
from app.connectors.base import ExtractionError, ScanRequest
from app.connectors.contract import check_connector, client_for
from app.connectors.http import PoliteClient
from app.connectors.kit import json_ld
from app.connectors.manifest import load_manifest
from app.connectors.registry import BUILTIN_DIR, CONNECTORS
from app.connectors.urls import match_product_url
from app.db import SessionLocal
from app.models import Listing
from app.worker import run_job
from tests.connector_modules import module_of
from tests.test_regressions import admin_client

_module = module_of("se.lidl")
LidlConnector, parse_page = _module.LidlConnector, _module.parse_page
FIXTURES = Path(__file__).parent / "fixtures"
DONUT, MANDLAR, SYLTMUNK = "11000213", "11000943", "11000778"
PAGES = {DONUT: ("halloween-donut", "lidl_donut.html"), MANDLAR: ("favorina-mandlar", "lidl_mandlar.html"),
         SYLTMUNK: ("syltmunk", "lidl_syltmunk.html")}
LINK = "https://www.lidl.se/p/favorina-mandlar/p11000943"


def page(sku):
    return (FIXTURES / PAGES[sku][1]).read_text(encoding="utf-8")


class FakeLidl:
    """Stands in for www.lidl.se: the bare id redirects to the canonical page, like the real site."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.override = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.override:
            return self.override(request)
        match = re.fullmatch(r"/p/(?:[^/]+/)?p(\d+)", request.url.path)
        if not match or match.group(1) not in PAGES:
            return httpx.Response(404)
        slug, _ = PAGES[match.group(1)]
        canonical = f"/p/{slug}/p{match.group(1)}"
        if request.url.path != canonical:
            return httpx.Response(301, headers={"Location": "https://www.lidl.se" + canonical})
        return httpx.Response(200, content=page(match.group(1)).encode())


def connector(site, follow=True, max_requests=6):
    client = PoliteClient(["www.lidl.se"], 0, transport=httpx.MockTransport(site), follow_redirects=follow)
    return LidlConnector({"max_requests_per_scan": max_requests}, client)


def scan(site, *skus, follow=True):
    return connector(site, follow).scan("online", "online", ScanRequest(skus=skus))


# ---- reading a page -------------------------------------------------------------------------

def test_a_page_with_a_price_gives_price_stock_and_no_invented_unit_size():
    price, name = parse_page(page(MANDLAR), MANDLAR)
    assert (name, price.sku, price.current_price) == ("Mandlar", MANDLAR, Decimal("34.9"))
    assert price.availability == "out_of_stock" and price.currency == "SEK"
    assert (price.package_value, price.package_unit, price.gtin) == (None, None, None)
    assert price.source_url == "https://www.lidl.se/p/p11000943"


@pytest.mark.parametrize("sku", [DONUT, SYLTMUNK])
def test_a_page_that_shows_no_price_is_unknown_not_zero(sku):
    price, name = parse_page(page(sku), sku)
    assert price is None and name


def test_the_barcode_comes_from_the_page_when_there_is_a_price():
    html = page(DONUT).replace('"availability": "OutOfStock"', '"availability": "InStock", "price": 12.5')
    price, _ = parse_page(html, DONUT)
    assert (price.gtin, price.current_price, price.availability) == ("4335619333284", Decimal("12.5"), "in_stock")


def test_other_availability_terms_stay_unknown():
    html = page(MANDLAR).replace("OutOfStock", "https://schema.org/PreOrder")
    assert parse_page(html, MANDLAR)[0].availability is None
    html = page(MANDLAR).replace("OutOfStock", "https://schema.org/InStock")
    assert parse_page(html, MANDLAR)[0].availability == "in_stock"


@pytest.mark.parametrize("html", ["<html>not found</html>", "<script type='application/ld+json'>{broken</script>",
                                  page(MANDLAR).replace("11000943", "99999999")])
def test_a_page_without_the_product_is_an_extraction_failure_not_a_price(html):
    with pytest.raises(ExtractionError):
        parse_page(html, MANDLAR)


def test_json_ld_is_tolerant_of_lists_graphs_and_garbage():
    html = ('<script type="application/ld+json">[{"@type":"A"},{"@type":"B"}]</script>'
            "<script type='application/ld+json'>not json</script>"
            '<script type="application/ld+json">{"@graph":[{"@type":"C"}]}</script>')
    kinds = [item.get("@type") for item in json_ld(html)]
    assert kinds == ["A", "B", None, "C"]      # the list is flattened, the broken block skipped, @graph opened


# ---- the connector --------------------------------------------------------------------------

def test_a_lookup_by_id_follows_lidls_own_redirect_and_counts_both_requests():
    site = FakeLidl()
    snapshot = scan(site, MANDLAR)
    assert [p.sku for p in snapshot.prices] == [MANDLAR] and snapshot.effective_status == "success"
    assert [(c.url.path) for c in site.calls] == ["/p/p11000943", "/p/favorina-mandlar/p11000943"]
    assert snapshot.requests == 2


def test_a_product_with_no_online_price_is_reported_with_the_reason():
    snapshot = scan(FakeLidl(), SYLTMUNK)
    assert snapshot.prices == [] and snapshot.effective_status == "not_found"
    assert snapshot.missing == (SYLTMUNK,) and "without an online price" in snapshot.detail
    assert "Syltmunk" in snapshot.detail


def test_some_priced_and_some_not_is_partial_and_the_priced_one_is_kept():
    snapshot = scan(FakeLidl(), MANDLAR, SYLTMUNK, "11999999")
    assert [p.sku for p in snapshot.prices] == [MANDLAR] and snapshot.effective_status == "partial"
    assert snapshot.missing == (SYLTMUNK, "11999999")


def test_search_and_barcode_requests_are_answered_without_contacting_lidl():
    site = FakeLidl()
    snapshot = connector(site).scan("online", "online", ScanRequest(query="mandlar", gtins=("4335619333284",)))
    assert snapshot.effective_status == "not_found" and site.calls == []
    assert "page link" in snapshot.detail


def test_a_garbage_id_is_never_requested():
    site = FakeLidl()
    assert scan(site, "../etc", "12").missing == ("../etc", "12") and site.calls == []


def test_redirects_are_refused_unless_the_manifest_opts_in():
    snapshot = scan(FakeLidl(), MANDLAR, follow=False)
    assert snapshot.effective_status == "access_blocked"


def test_a_redirect_to_another_host_is_always_a_refusal():
    site = FakeLidl()
    site.override = lambda r: httpx.Response(302, headers={"Location": "https://consent.example.org/login"})
    snapshot = scan(site, MANDLAR)
    assert snapshot.effective_status == "access_blocked" and len(site.calls) == 1


def test_a_redirect_to_http_or_a_loop_is_a_refusal_and_never_unbounded():
    site = FakeLidl()
    site.override = lambda r: httpx.Response(301, headers={"Location": "http://www.lidl.se/p/p11000943"})
    assert scan(site, MANDLAR).effective_status == "access_blocked" and len(site.calls) == 1
    site = FakeLidl()
    site.override = lambda r: httpx.Response(301, headers={"Location": "https://www.lidl.se/p/loop/p11000943"})
    snapshot = scan(site, MANDLAR)
    assert snapshot.effective_status == "access_blocked" and len(site.calls) == 3   # first + 2 hops, then stop


def test_redirect_hops_count_against_the_request_budget():
    site = FakeLidl()
    snapshot = connector(site, max_requests=1).scan("online", "online", ScanRequest(skus=(MANDLAR,)))
    assert snapshot.effective_status == "partial" and len(site.calls) == 1   # budget gone before the 2nd hop


def test_lidl_passes_the_contract_and_the_manifest_opts_in_to_redirects():
    manifest = load_manifest(BUILTIN_DIR / "se.lidl" / "manifest.yaml")
    assert manifest.follow_redirects is True

    def make(transport, secrets):
        return LidlConnector(manifest.model_dump(), client_for(manifest, transport))

    passed = check_connector(make, FakeLidl(), manifest=manifest, store="online",
                             request=ScanRequest(skus=(MANDLAR,)))
    assert {"identity", "targeted_only", "happy_path", "refusals", "bad_payloads", "product_urls"} <= set(passed)


def test_product_page_links_are_read_and_nothing_else_is():
    rules = registry.retailer("se.lidl")["product_urls"]
    assert match_product_url(rules, LINK) == MANDLAR
    assert match_product_url(rules, "https://www.lidl.se/c/lidl-plus-erbjudanden/s10017715") is None
    assert match_product_url(rules, "https://www.lidl.se/q/search?q=mandlar") is None
    assert match_product_url(rules, "https://www.lidl.se/p/x/pabc") is None


# ---- inside the service ---------------------------------------------------------------------

@pytest.fixture
def lidl(fresh_db, monkeypatch):
    retrieval.clear_cache()
    site = FakeLidl()
    monkeypatch.setitem(CONNECTORS, "se.lidl", connector(site))
    client = admin_client()
    store = next(s for s in client.get("/v1/stores").json()["items"] if s["retailer_id"] == "se.lidl")
    return SimpleNamespace(site=site, client=client, store_id=store["id"])


def finish(env, response):
    assert response.status_code in (200, 202), response.text
    assert run_job()
    return env.client.get(f"/v1/jobs/{response.json()['job_id']}").json()


def add(env, url):
    return finish(env, env.client.post("/v1/monitors/from-url", json={"url": url, "store_id": env.store_id}))


def test_a_priced_product_is_added_by_its_link(lidl):
    job = add(lidl, LINK)
    assert job["state"] == "succeeded" and job["result"]["product"]["sku"] == MANDLAR
    (monitor,) = lidl.client.get("/v1/monitors").json()["items"]
    assert monitor["latest"]["current_price"] == "34.90" and monitor["latest"]["availability"] == "out_of_stock"
    assert monitor["latest"]["unit_price"] is None            # unknown size: no invented unit price


def test_a_product_with_no_online_price_is_not_added_and_the_person_is_told_why(lidl):
    job = add(lidl, "https://www.lidl.se/p/syltmunk/p11000778")
    assert job["result"]["status"] == "not_found" and "without an online price" in job["result"]["detail"]
    assert lidl.client.get("/v1/monitors").json()["items"] == []
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Listing)) == 0


def test_searching_by_name_never_reaches_lidl_and_says_why(lidl):
    job = finish(lidl, lidl.client.post("/v1/search/jobs", json={"query": "mandlar", "store_ids": [lidl.store_id]}))
    (coverage,) = job["result"]["coverage"]
    assert coverage["status"] == "unsupported" and "page link" in coverage["detail"]
    assert lidl.site.calls == []


def test_a_search_monitor_is_refused_for_a_store_that_cannot_be_searched(lidl):
    body = {"name": "m", "type": "generic_product", "store_id": lidl.store_id, "interval_minutes": 60,
            "conditions": {"query": "mandlar"}}
    refused = lidl.client.post("/v1/monitors", json=body)
    assert refused.status_code == 422 and "page link" in refused.json()["message"]


def test_comparing_a_barcode_marks_lidl_unknown_until_a_product_is_linked(lidl):
    queued = lidl.client.post("/v1/compare/barcode", json={"gtin": "4335619333284", "store_ids": [lidl.store_id]})
    assert queued.status_code in (200, 202), queued.text
    row = (queued.json().get("known") or [{}])[0]
    assert row.get("status", "unknown_mapping") in ("unknown", "unknown_mapping")
    assert lidl.site.calls == []

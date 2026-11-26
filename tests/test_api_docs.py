"""The API documentation cannot drift from the API, and the example client really works."""
import importlib.util
import json
import re
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.connectors import registry
from app.main import USER_SCOPES, app
from app.openapi import ERROR_CODES, PUBLIC, TAGS
from app.webhooks import signed_headers
from app.worker import run_job
from tests.test_regressions import admin_client

ROOT = Path(__file__).parent.parent
GUIDE = (ROOT / "docs" / "api.md").read_text()


def load_example():
    spec = importlib.util.spec_from_file_location("shopper_client", ROOT / "examples" / "shopper_client.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["shopper_client"] = module
    spec.loader.exec_module(module)
    return module


# ---- the OpenAPI document --------------------------------------------------------------------

def operations():
    return [(method.upper(), path, op) for path, ops in app.openapi()["paths"].items()
            for method, op in ops.items()]


def test_every_operation_is_tagged_and_documents_how_to_authenticate_and_what_errors_look_like():
    schema = app.openapi()
    assert set(schema["components"]["securitySchemes"]) == {"UserToken", "ClientToken"}
    assert "Error" in schema["components"]["schemas"]
    assert [t["name"] for t in schema["tags"]] == [name for name, _ in TAGS]
    for method, path, op in operations():
        assert op["tags"] and op["tags"][0] in {name for name, _ in TAGS}, (method, path)
        if path.startswith("/health"):
            continue
        assert op["responses"]["429"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/Error"}
        assert op["responses"]["422"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/Error"}
        if path in PUBLIC:
            assert "security" not in op, path
        else:
            assert op["security"] == [{"UserToken": [], "ClientToken": []}, {"UserToken": []}], path
            assert {"401", "403"} <= set(op["responses"]), path
        if "{" in path:
            assert "404" in op["responses"], (method, path)
        if path.startswith("/v1/admin"):
            assert op["x-admin-only"] is True


def test_the_committed_openapi_file_matches_the_code():
    committed = json.loads((ROOT / "docs" / "openapi.json").read_text())
    assert committed == json.loads(json.dumps(app.openapi())), (
        "regenerate it: python -m app.openapi > docs/openapi.json")


def test_error_codes_in_the_spec_are_the_ones_the_service_uses():
    source = (ROOT / "app" / "main.py").read_text()
    used = set(re.findall(r'"(unauthorized|forbidden|not_found|method_not_allowed|conflict|invalid_request|'
                          r'rate_limited|unavailable|request_failed)"', source))
    assert used == set(ERROR_CODES)


# ---- the guide ------------------------------------------------------------------------------

def routes():
    found = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith(("/v1", "/health")):
            found |= {(m, path) for m in route.methods if m not in ("HEAD", "OPTIONS")}
    return found


def documented():
    return set(re.findall(r"^\| (GET|POST|PUT|PATCH|DELETE) \| `([^`]+)`", GUIDE, re.MULTILINE))


def test_every_endpoint_is_in_the_guides_reference_and_nothing_stale_is_left():
    actual, doc = routes(), documented()
    assert actual - doc == set(), f"missing from docs/api.md: {sorted(actual - doc)}"
    assert doc - actual == set(), f"docs/api.md lists endpoints that do not exist: {sorted(doc - actual)}"


def test_the_scope_table_and_the_limits_match_the_code():
    for scope in USER_SCOPES:
        assert f"| `{scope}` |" in GUIDE, scope
    defaults = {name: field.default for name, field in Settings.model_fields.items()}
    expected = [
        f"| API requests | {defaults['api_rate_limit_per_minute']} per minute per credential |",
        f"| Active monitors | {defaults['max_monitors_per_user']} |",
        f"| Live lookups in flight (search or compare) | {defaults['max_pending_searches_per_user']} |",
        f"| Live lookups per hour | {defaults['max_searches_per_hour']} |",
        f"| Stores in \"my stores\" | {defaults['max_my_stores']} |",
        f"| Stores in one barcode comparison | {defaults['max_compare_stores']} |",
        f"one per {defaults['min_manual_check_interval_seconds']} seconds",
    ]
    for text in expected:
        assert text in GUIDE, text
    for retailer in registry.retailers():
        if retailer["id"] in ("se.citygross", "se.coop", "se.willys", "se.lidl"):
            assert retailer["minimum_monitor_interval_minutes"] == 60  # "at least 60 minutes" in the guide
    assert "at least 60 minutes for City Gross, Coop, Willys and Lidl" in GUIDE


def test_every_event_type_the_service_emits_is_documented():
    emitted = set(re.findall(r'"((?:price|offer|product)\.[a-z_]+)"', (ROOT / "app" / "monitoring.py").read_text()))
    assert emitted and all(f"`{event}`" in GUIDE for event in emitted), emitted
    for code in re.findall(r"^\| \d{3}(?: / \d{3})? \| `([a-z_]+)`", GUIDE, re.MULTILINE):
        assert code in ERROR_CODES, code


def test_the_guide_tells_an_agent_what_it_must_know():
    for needle in ("Idempotency-Key", "Retry-After", "X-Client-Token", "status_url", "store_id",
                   "Never call `/v1/admin/*`", "Unknown is", "signing_secret", "merge", "from-url",
                   "9999990000019", "https://demo.example.org/p/oats-1kg"):
        assert needle in GUIDE, needle


def test_relative_links_in_the_guide_and_examples_resolve():
    for page in (ROOT / "docs" / "api.md", ROOT / "examples" / "README.md"):
        for target in re.findall(r"\]\((?!https?:|#|/)([^)#]+)", page.read_text()):
            assert (page.parent / target).exists(), f"{page.name} links to missing {target}"


def test_the_service_serves_its_own_documentation(fresh_db):
    client = TestClient(app)
    assert client.get("/api-guide").text == GUIDE
    assert client.get("/api-guide").headers["content-type"].startswith("text/markdown")
    llms = client.get("/llms.txt").text
    assert "/api-guide" in llms and "/openapi.json" in llms and "Idempotency-Key" in llms
    assert "UserToken" in client.get("/openapi.json").text and client.get("/docs").status_code == 200


# ---- the example client works against the real API --------------------------------------------

@pytest.fixture
def api(fresh_db, monkeypatch):
    shopper = load_example()
    admin = admin_client()
    session = admin.post("/v1/admin/example-user").json()
    now = {"t": 0.0}
    monkeypatch.setattr(registry.CONNECTORS["test.demoshop"], "clock", lambda: now["t"])
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    def advance_then_work():
        now["t"] += 45.0  # every job runs 45 simulated seconds later, so prices move between checks
        run_job()

    client = shopper.RetailMonitor(TestClient(app), session["user_token"], session["client_token"],
                                   on_wait=advance_then_work)
    return shopper, client


def test_the_worked_demo_runs_end_to_end_through_the_public_api(api):
    shopper, client = api
    lines: list[str] = []
    result = shopper.demo(client, say=lines.append)
    assert result["product"]["name"] == "Demo oats 1 kg" and result["compare"]["cheapest"]["store_name"]
    assert any("Added by link" in line for line in lines) and any("Cheaper at" in line for line in lines)
    assert result["events"] and all(e["type"].startswith(("price.", "offer.")) for e in result["events"])
    kept = client.watchlist(include_removed=True)
    assert kept and kept[0]["removed_at"] and client.watchlist() == []   # removed, history kept


def test_errors_surface_the_envelope_and_the_merge_question_is_a_question(api):
    shopper, client = api
    with pytest.raises(shopper.ApiError) as error:
        client.request("GET", "/v1/monitors/no-such-monitor")
    assert (error.value.status, error.value.code) == (404, "not_found") and error.value.request_id

    stores = {s["name"]: s["id"] for s in client.stores() if s["retailer_id"] == "test.demoshop"}
    north = stores["Demo Shop North"]
    client.choose_store(north)
    oats = client.watch(client.search("oats", [north])[0]["listing_id"], north, "Oats")
    milk_item = client.search("milk", [north])[0]
    milk = client.watch(milk_item["listing_id"], north, "Milk")
    assert oats["id"] and milk["id"]
    with pytest.raises(shopper.BarcodeBelongsToAnotherProduct) as question:
        client.link_barcode(milk["id"], "9999990000019")      # that is the oats' barcode
    assert question.value.existing_product["name"] == "Demo oats 1 kg"
    merged = client.link_barcode(milk["id"], "9999990000019", merge=True)
    assert merged["outcome"] == "merged" and merged["product"]["listings"] == 2


def test_the_event_cursor_pages_and_the_webhook_check_accepts_only_genuine_deliveries(api):
    shopper, client = api
    body = b'{"id":"e1","type":"price.decreased","occurred_at":"2026-11-26T09:00:00+00:00","data":{}}'
    headers = signed_headers("secret-value", body, "delivery-1", int(time.time()))
    assert shopper.verify_webhook("secret-value", headers, body)
    assert not shopper.verify_webhook("another-secret", headers, body)          # wrong key
    assert not shopper.verify_webhook("secret-value", headers, body + b" ")     # tampered body
    stale = signed_headers("secret-value", body, "delivery-1", int(time.time()) - 3600)
    assert not shopper.verify_webhook("secret-value", stale, body)              # replayed too late
    assert not shopper.verify_webhook("secret-value", {}, body)                 # missing headers
    assert list(client.events()) == []

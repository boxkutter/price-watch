"""Never spammy: our API limits, per-user quotas, and how we behave toward a retailer."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import connector_state, retrieval
from app.config import settings
from app.connectors.registry import CONNECTORS
from app.db import SessionLocal
from app.main import app
from app.models import ConnectorState, Store, aware, utcnow
from app.monitoring import next_due
from app.ratelimit import SlidingWindow
from app.worker import run_job
from tests.test_citygross import FakeSite, connector
from tests.test_regressions import admin_client

AXA = "7310130321085"          # 1,5KG AXA havregryn, no promotion
GYLLEN = "100132394_ST"        # 1KG Gyllenhamars, single-item promotion


@pytest.fixture
def cg(fresh_db, monkeypatch):
    retrieval.clear_cache()
    site = FakeSite()
    monkeypatch.setitem(CONNECTORS, "se.citygross", connector(site))
    with SessionLocal.begin() as db:
        store = Store(retailer_id="se.citygross", external_id="3204", name="City Gross Borås")
        db.add(store)
        db.flush()
        store_id = store.id
    return SimpleNamespace(site=site, client=admin_client(), store_id=store_id)


def job_of(cg, response):
    assert response.status_code in (200, 202), response.text
    job_id = response.json()["job_id"]
    assert run_job()
    return cg.client.get(f"/v1/jobs/{job_id}").json()


def search(cg, **body):
    return job_of(cg, cg.client.post("/v1/search/jobs", json={"store_ids": [cg.store_id], **body}))


def listing_id(cg, query):
    return search(cg, query=query)["result"]["items"][0]["listing_id"]


def monitor(cg, kind="exact_product", **conditions):
    body = {"name": "m", "type": kind, "store_id": cg.store_id, "interval_minutes": 60,
            "notify_existing_offers": True, "conditions": conditions}
    return cg.client.post("/v1/monitors", json=body)


def check(cg, monitor_id):
    return job_of(cg, cg.client.post(f"/v1/monitors/{monitor_id}/check"))


def state():
    with SessionLocal() as db:
        return db.get(ConnectorState, "se.citygross")


# ---- limits on our own API --------------------------------------------------------------

def test_sliding_window_limits_each_key_and_recovers():
    now = [0.0]
    window = SlidingWindow(2, 60, clock=lambda: now[0])
    assert window.check("a") is None and window.check("a") is None
    wait = window.check("a")
    assert wait is not None and 1 <= wait <= 61   # over the limit: told how long to wait
    assert window.check("b") is None            # other callers are unaffected
    now[0] = 61
    assert window.check("a") is None            # the window slides


def test_api_rate_limit_returns_429_with_retry_after_and_spares_health(fresh_db, monkeypatch):
    monkeypatch.setattr("app.main.api_limiter", SlidingWindow(3))
    client = TestClient(app)
    codes = [client.get("/v1/me").status_code for _ in range(5)]
    assert codes == [401, 401, 401, 429, 429]
    limited = client.get("/v1/me")
    assert limited.json()["code"] == "rate_limited" and int(limited.headers["Retry-After"]) >= 1
    assert limited.json()["request_id"] == limited.headers["X-Request-ID"]
    assert client.get("/health/live").status_code == 200


def test_per_user_quotas_on_monitors_searches_and_manual_checks(cg, monkeypatch):
    monkeypatch.setattr(settings, "max_monitors_per_user", 1)
    first = monitor(cg, "generic_product", query="havregryn")
    assert first.status_code == 201
    over = monitor(cg, "generic_product", query="mjölk")
    assert over.status_code == 429 and "limit" in over.json()["message"].lower()

    monkeypatch.setattr(settings, "max_pending_searches_per_user", 1)
    assert cg.client.post("/v1/search/jobs", json={"query": "havre", "store_ids": [cg.store_id]}
                          ).status_code == 202
    busy = cg.client.post("/v1/search/jobs", json={"query": "mjolk", "store_ids": [cg.store_id]})
    assert busy.status_code == 429 and busy.headers["Retry-After"] == "60"

    monkeypatch.setattr(settings, "min_manual_check_interval_seconds", 60)
    monitor_id = first.json()["id"]
    queued = cg.client.post(f"/v1/monitors/{monitor_id}/check")
    assert queued.status_code == 202
    again = cg.client.post(f"/v1/monitors/{monitor_id}/check")
    assert again.status_code == 202 and again.json()["job_id"] == queued.json()["job_id"]
    assert run_job() and run_job()  # drain the queued search and the check
    too_soon = cg.client.post(f"/v1/monitors/{monitor_id}/check")
    assert too_soon.status_code == 429 and int(too_soon.headers["Retry-After"]) >= 1


def test_live_retailers_enforce_a_minimum_check_interval_and_refuse_store_wide_monitors(cg):
    fast = cg.client.post("/v1/monitors", json={
        "name": "fast", "type": "generic_product", "store_id": cg.store_id,
        "interval_minutes": 15, "conditions": {"query": "havregryn"}})
    assert fast.status_code == 422 and "60 minutes" in fast.json()["message"]
    whole_store = cg.client.post("/v1/monitors", json={
        "name": "everything", "type": "store", "store_id": cg.store_id, "interval_minutes": 60})
    assert whole_store.status_code == 422  # live connectors never scan a whole store
    too_long = monitor(cg, "generic_product", query="x" * 101)
    assert too_long.status_code == 422


# ---- end to end through the worker ------------------------------------------------------

def test_barcode_search_finds_the_product_and_records_a_verified_mapping(cg):
    result = search(cg, gtin=AXA)
    assert result["state"] == "succeeded"
    assert result["result"]["barcode_mapping"] == "verified"
    assert result["result"]["coverage"][0]["status"] == "complete"
    (item,) = result["result"]["items"]
    assert item["name"] == "Havregryn 1,5KG AXA" and item["current_price"] == "19.95"
    assert cg.client.get(f"/v1/products/barcode/{AXA}").json()["id"] == item["product_id"]
    unknown = search(cg, gtin="4006381333931")
    assert unknown["result"]["items"] == [] and unknown["result"]["barcode_mapping"] == "unknown"
    assert unknown["result"]["coverage"][0]["status"] == "not_found"


def test_exact_monitor_follows_price_and_promotion_for_just_that_product(cg):
    lid = listing_id(cg, "gyllenhamars")
    created = monitor(cg, listing_id=lid, maximum_price="18.00")
    assert created.status_code == 201
    mid = created.json()["id"]
    first = check(cg, mid)
    assert first["state"] == "succeeded" and first["result"]["prices"] == 1
    sku_requests = [c for c in cg.site.calls if c.url.params["SearchQuery"] == GYLLEN]
    assert len(sku_requests) == 1 and all(c.url.params["store"] == "3204" for c in cg.site.calls)
    def types():
        return [e["type"] for e in cg.client.get("/v1/events").json()["items"]]

    assert types() == ["offer.detected"]  # the promotion attached to the monitored product

    retrieval.clear_cache()
    gyllen = next(p for p in cg.site.items if p["id"] == GYLLEN)
    gyllen["productStoreDetails"]["prices"]["promotions"] = []  # promotion ends
    second = check(cg, mid)
    assert second["state"] == "succeeded"
    assert "price.increased" in types()
    latest = cg.client.get(f"/v1/listings/{lid}/prices").json()["items"][0]
    assert latest["current_price"] == "20.75" and latest["regular_price"] == "20.75"


def test_duplicate_monitors_share_one_retailer_request(cg):
    lid = listing_id(cg, "gyllenhamars")
    first = monitor(cg, listing_id=lid).json()["id"]
    second = monitor(cg, listing_id=lid).json()["id"]
    check(cg, first)
    calls = len(cg.site.calls)
    assert check(cg, second)["state"] == "succeeded"
    assert len(cg.site.calls) == calls  # served from the short cache


def test_a_block_pauses_the_connector_and_stops_all_requests_until_resumed(cg):
    mid = monitor(cg, "generic_product", query="havregryn").json()["id"]
    cg.site.status = 403
    blocked = check(cg, mid)
    assert blocked["state"] == "access_blocked"
    assert state().state == "paused" and "not disguised" in state().reason
    calls = len(cg.site.calls)

    cg.site.status = 200  # the site recovers, but we do not probe it
    assert check(cg, mid)["state"] == "paused"
    assert search(cg, query="havregryn")["result"]["coverage"][0]["status"] == "paused"
    assert len(cg.site.calls) == calls
    health = cg.client.get("/v1/connectors/se.citygross/health").json()
    assert health["paused"] and health["status"] == "paused" and health["pause_reason"]

    assert cg.client.post("/v1/admin/connectors/se.citygross/resume").status_code == 204
    assert check(cg, mid)["state"] == "succeeded" and len(cg.site.calls) == calls + 1


def test_retry_after_starts_a_cooldown_during_which_nothing_is_requested(cg):
    mid = monitor(cg, "generic_product", query="havregryn").json()["id"]
    cg.site.status, cg.site.headers = 429, {"Retry-After": "300"}
    assert check(cg, mid)["state"] == "rate_limited"
    cooldown = state().cooldown_until
    assert cooldown is not None and aware(cooldown) > utcnow() + timedelta(seconds=250)
    cg.site.status = 200
    calls = len(cg.site.calls)
    assert check(cg, mid)["state"] == "rate_limited" and len(cg.site.calls) == calls
    assert state().state != "paused"  # slowing down is not a block


def test_daily_request_budget_stops_requests(cg):
    mid = monitor(cg, "generic_product", query="havregryn").json()["id"]
    with SessionLocal.begin() as db:
        row = connector_state.get_state(db, "se.citygross")
        row.window_start, row.requests_in_window = utcnow(), 400
    assert check(cg, mid)["state"] == "rate_limited" and cg.site.calls == []
    with SessionLocal.begin() as db:  # a new 24h window starts fresh
        connector_state.get_state(db, "se.citygross").window_start = utcnow() - timedelta(hours=25)
    assert check(cg, mid)["state"] == "succeeded"
    assert state().requests_in_window == 1


def test_repeated_extraction_failures_pause_for_review(cg):
    mid = monitor(cg, "generic_product", query="havregryn").json()["id"]
    cg.site.body = b'{"layout": "changed"}'
    states = [check(cg, mid)["state"] for _ in range(connector_state.FAILURE_PAUSE_THRESHOLD)]
    assert states == ["extraction_failed"] * connector_state.FAILURE_PAUSE_THRESHOLD
    assert state().state == "paused" and "extraction failures" in state().reason
    assert check(cg, mid)["state"] == "paused"


def test_store_discovery_is_a_deduplicated_worker_job(cg):
    first = cg.client.post("/v1/admin/retailers/se.citygross/stores/discover")
    assert first.status_code == 202
    again = cg.client.post("/v1/admin/retailers/se.citygross/stores/discover")
    assert again.json()["job_id"] == first.json()["job_id"]
    assert cg.site.calls == []  # the API never contacts the retailer itself
    assert run_job()
    result = cg.client.get(f"/v1/jobs/{first.json()['job_id']}").json()["result"]
    assert result["status"] == "success" and result["stores"] == 3 and result["created"] == 2
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Store).where(
            Store.retailer_id == "se.citygross")) == 3
    rerun = cg.client.post("/v1/admin/retailers/se.citygross/stores/discover")
    assert run_job()
    assert cg.client.get(f"/v1/jobs/{rerun.json()['job_id']}").json()["result"]["created"] == 0
    assert cg.client.post("/v1/admin/retailers/test.fixture/stores/discover").status_code == 422


def test_scheduler_spreads_due_times_with_jitter():
    base = utcnow() + timedelta(minutes=60)
    offsets = {(next_due(60) - base).total_seconds() for _ in range(20)}
    assert len(offsets) > 1 and all(-2 <= offset <= 362 for offset in offsets)  # up to 10% later

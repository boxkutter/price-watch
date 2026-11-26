"""The example client: a regular user with a scoped API client, and the synthetic demo shop."""
import sys
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.connectors import registry
from app.connectors.base import ScanRequest
from app.connectors.urls import match_product_url
from app.db import SessionLocal
from app.example_user import EXAMPLE_EMAIL
from app.main import app
from app.models import (
    AuditLog,
    Event,
    Job,
    Listing,
    Monitor,
    PriceObservation,
    Store,
    User,
    UserStore,
)
from app.worker import run_job
from tests.test_regressions import admin_client

OATS_URL = "https://demo.example.org/p/oats-1kg"


def demo_module():
    return sys.modules[type(registry.CONNECTORS["test.demoshop"]).__module__]


@pytest.fixture
def shop(fresh_db, monkeypatch):
    """Admin client, an example-user client, and a movable clock for the demo shop."""
    admin = admin_client()
    session = admin.post("/v1/admin/example-user").json()
    user = TestClient(app)
    user.headers.update({"Authorization": "Bearer " + session["user_token"],
                         "X-Client-Token": session["client_token"]})
    now = {"t": 0.0}
    monkeypatch.setattr(registry.CONNECTORS["test.demoshop"], "clock", lambda: now["t"])
    stores = {s["name"]: s["id"] for s in user.get("/v1/stores").json()["items"]}
    return SimpleNamespace(admin=admin, user=user, session=session, now=now, stores=stores,
                           north=stores["Demo Shop North"], south=stores["Demo Shop South"])


def finish(client, response):
    assert response.status_code in (200, 202), response.text
    job_id = response.json()["job_id"]
    assert run_job()
    return client.get(f"/v1/jobs/{job_id}").json()


# ---- the demo shop ---------------------------------------------------------------------------

def scan(shop_, store, **request):
    return registry.CONNECTORS["test.demoshop"].scan(store, "online", ScanRequest(**request))


def test_demo_prices_move_over_time_and_the_south_shop_is_cheaper(shop):
    series = []
    for t in (0, 60, 120, 180, 240):
        shop.now["t"] = float(t)
        series.append(scan(shop, "north", skus=("oats-1kg",)).prices[0].current_price)
    assert len(set(series)) >= 4  # it really moves from one minute to the next
    shop.now["t"] = 60.0
    north = scan(shop, "north", skus=("oats-1kg",)).prices[0].current_price
    south = scan(shop, "south", skus=("oats-1kg",)).prices[0].current_price
    assert Decimal("0.90") < south / north < Decimal("0.98")


def test_the_demo_shop_follows_the_same_rules_as_a_live_connector(shop):
    demo = registry.CONNECTORS["test.demoshop"]
    assert demo.scan("north", "online", ScanRequest()).effective_status == "unsupported"
    assert demo.scan("north", "online", None).effective_status == "unsupported"
    assert demo.scan("north", "physical", ScanRequest(query="x")).effective_status == "unsupported"
    assert demo.scan("nowhere", "online", ScanRequest(query="x")).effective_status == "unsupported"
    oats = demo_module().PRODUCTS[0][6]
    by_code = scan(shop, "south", gtins=(oats,))
    assert [p.sku for p in by_code.prices] == ["oats-1kg"] and by_code.requests == 0
    assert scan(shop, "south", gtins=("4006381333931",)).effective_status == "not_found"
    partial = scan(shop, "north", skus=("oats-1kg", "nope"))
    assert partial.effective_status == "partial" and partial.missing == ("nope",)
    assert [p.sku for p in scan(shop, "north", query="demo").prices] == ["oats-1kg", "coffee-450g", "milk-1l"]


def test_the_oats_offer_switches_on_and_off_and_product_links_are_understood(shop):
    shop.now["t"] = 10.0
    assert scan(shop, "north", skus=("oats-1kg",)).offers[0].terms == "2 för 45 kr"
    shop.now["t"] = 70.0
    assert scan(shop, "north", skus=("oats-1kg",)).offers == []
    rules = registry.retailer("test.demoshop")["product_urls"]
    assert match_product_url(rules, OATS_URL) == "oats-1kg"
    assert match_product_url(rules, "https://demo.example.org/other") is None


def test_the_demo_stores_come_from_the_connector_manifests(shop):
    names = {s["name"] for s in shop.user.get("/v1/stores").json()["items"]}
    assert {"Synthetic demo store", "Demo Shop North", "Demo Shop South"} <= names


# ---- the example user ------------------------------------------------------------------------

def test_the_example_user_is_a_regular_user_with_a_scoped_client(shop):
    me = shop.user.get("/v1/me").json()
    assert me["email"] == EXAMPLE_EMAIL and me["is_admin"] is False and me["client_id"]
    assert "clients:manage" not in me["scopes"] and "admin" not in me["scopes"]
    assert {"catalogue:read", "monitors:read", "monitors:write", "events:read"} <= set(me["scopes"])
    assert shop.session["scopes"] == [s for s in me["scopes"] if s != "clients:manage"] or set(
        shop.session["scopes"]) == set(me["scopes"])
    with SessionLocal() as db:
        assert db.scalar(select(User).where(User.email == EXAMPLE_EMAIL)).is_admin is False


def test_the_example_user_cannot_reach_administration_in_any_way(shop):
    for method, path in (("get", "/v1/admin/connectors"), ("get", "/v1/admin/jobs"),
                         ("post", "/v1/admin/example-user"), ("post", "/v1/admin/invitations"),
                         ("delete", "/v1/admin/example-user/data"),
                         ("post", "/v1/admin/connectors/se.coop/disable")):
        assert getattr(shop.user, method)(path).status_code == 403, path
    assert shop.user.post("/v1/clients", json={"name": "x", "scopes": ["monitors:read"]}).status_code == 403
    plain = TestClient(app)
    plain.headers["Authorization"] = shop.user.headers["Authorization"]  # user token alone, no client
    assert plain.get("/v1/admin/connectors").status_code == 403           # still not an admin
    only_client = TestClient(app)
    only_client.headers["X-Client-Token"] = shop.session["client_token"]  # a client token alone is useless
    assert only_client.get("/v1/monitors").status_code == 401


def test_only_an_administrator_can_open_or_reset_the_example_session(fresh_db):
    anonymous = TestClient(app)
    assert anonymous.post("/v1/admin/example-user").status_code == 401
    assert anonymous.delete("/v1/admin/example-user/data").status_code == 401


def test_opening_a_new_session_revokes_the_previous_client_credential_and_keeps_the_user(shop):
    old = dict(shop.user.headers)
    again = shop.admin.post("/v1/admin/example-user").json()
    assert again["user"]["id"] == shop.session["user"]["id"]
    assert again["client_token"] != shop.session["client_token"]
    stale = TestClient(app)
    stale.headers.update(old)
    revoked = stale.get("/v1/monitors")
    assert revoked.status_code == 401 and "client token" in revoked.json()["message"].lower()
    fresh = TestClient(app)
    fresh.headers.update({"Authorization": "Bearer " + again["user_token"],
                          "X-Client-Token": again["client_token"]})
    assert fresh.get("/v1/monitors").status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(User).where(User.email == EXAMPLE_EMAIL)) == 1
        actions = [a.action for a in db.scalars(select(AuditLog))]
    assert {"example_user.created", "example_user.session_opened"} <= set(actions)


# ---- using it like a client ------------------------------------------------------------------

def add_oats(shop_):
    first = shop_.user.post("/v1/monitors/from-url", json={"url": OATS_URL, "store_id": shop_.north})
    job = finish(shop_.user, first)
    assert job["state"] == "succeeded", job
    return job["result"]


def test_a_client_can_add_watch_and_see_the_price_move(shop):
    shop.now["t"] = 0.0
    added = add_oats(shop)
    assert added["product"]["name"] == "Demo oats 1 kg" and added["product"]["current_price"] == "24.90"
    mid = added["monitor_id"]
    monitors = shop.user.get("/v1/monitors").json()["items"]
    assert [m["id"] for m in monitors] == [mid]
    assert monitors[0]["store_name"] == "Demo Shop North" and monitors[0]["product_name"] == "Demo oats 1 kg"
    assert monitors[0]["barcode"]["source"] == "retailer" and monitors[0]["latest"]["previous_price"] is None

    shop.now["t"] = 60.0
    assert finish(shop.user, shop.user.post(f"/v1/monitors/{mid}/check"))["state"] == "succeeded"
    shop.now["t"] = 120.0
    assert finish(shop.user, shop.user.post(f"/v1/monitors/{mid}/check"))["state"] == "succeeded"
    latest = shop.user.get(f"/v1/monitors/{mid}").json()["latest"]
    assert latest["currency"] == "SEK" and latest["previous_price"] is not None
    assert latest["current_price"] != latest["previous_price"] and latest["unit_price"]["unit"] == "kg"
    types = {e["type"] for e in shop.user.get("/v1/events").json()["items"]}
    assert types & {"price.decreased", "price.increased"}  # the monitor noticed, on its own


def test_comparing_the_demo_shops_by_barcode_works_offline_for_a_client(shop):
    for store in (shop.north, shop.south):
        assert shop.user.put(f"/v1/me/stores/{store}").status_code == 204
    shop.now["t"] = 60.0
    barcode = demo_module().PRODUCTS[0][6]
    queued = shop.user.post("/v1/compare/barcode", json={"gtin": barcode, "here_store_id": shop.north})
    result = finish(shop.user, queued)["result"]
    assert result["complete"] and result["requests"] == 0          # nothing leaves the machine
    assert result["cheapest"]["store_name"] == "Demo Shop South"
    assert result["saving_vs_here"]["amount"].startswith("-")


def test_the_example_users_data_is_private_and_can_be_reset(shop):
    shop.now["t"] = 0.0
    mid = add_oats(shop)["monitor_id"]
    shop.user.put(f"/v1/me/stores/{shop.north}")
    shop.user.post("/v1/compare/barcode", json={"gtin": demo_module().PRODUCTS[0][6]})
    run_job()
    assert shop.admin.get("/v1/monitors").json()["items"] == []          # the admin does not see it
    assert shop.admin.get(f"/v1/monitors/{mid}").status_code == 404

    reset = shop.admin.delete("/v1/admin/example-user/data").json()
    assert reset["monitors"] == 1 and reset["stores"] == 1
    assert shop.user.get("/v1/monitors?include_removed=true").json()["items"] == []
    assert shop.user.get("/v1/me/stores").json()["items"] == [] and shop.user.get("/v1/events").json()["items"] == []
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Monitor)) == 0
        assert db.scalar(select(func.count()).select_from(UserStore)) == 0
        assert db.scalar(select(func.count()).select_from(Event)) == 0
        assert db.scalar(select(func.count()).select_from(Job).where(Job.owner_id == shop.session["user"]["id"])) == 0
        assert db.scalar(select(func.count()).select_from(Listing)) >= 1             # the catalogue stays
        assert db.scalar(select(func.count()).select_from(PriceObservation)) >= 1
        assert db.scalar(select(func.count()).select_from(Store).where(Store.retailer_id == "test.demoshop")) == 2
        assert db.scalars(select(AuditLog).where(AuditLog.action == "example_user.reset")).one()
    assert shop.admin.delete("/v1/admin/example-user/data").json()["monitors"] == 0   # safe to repeat


def test_the_example_client_page_is_served_and_calls_only_the_public_api():
    page = TestClient(app).get("/demo")
    assert page.status_code == 200 and "example client" in page.text.lower()
    import re
    paths = set(re.findall(r"call\('(?:GET|POST|PUT|DELETE)','(/v1/[^'?]*)", page.text))
    assert paths and not [p for p in paths if p.startswith("/v1/admin")], paths
    assert "X-Client-Token" in page.text and "Authorization" in page.text


def test_the_example_client_can_scan_a_barcode_with_the_camera(fresh_db):
    client = TestClient(app)
    page = client.get("/demo").text
    assert 'name="viewport"' in page and "viewport-fit=cover" in page          # made for a phone
    assert 'data-scan="search-form"' in page and 'data-scan="compare-form"' in page   # the camera buttons
    assert 'id="scanner"' in page and 'capture="environment"' in page             # live view and photo fallback
    script = client.get("/vendor/zxing.min.js")
    assert script.status_code == 200 and script.headers["content-type"].startswith("text/javascript")
    assert len(script.content) > 200_000 and b"fetch(" not in script.content and b"eval(" not in script.content
    assert "max-age" in script.headers["cache-control"]

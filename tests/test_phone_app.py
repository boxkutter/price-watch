"""What the phone app needs from the service: edit an alert price, sign itself in, find stores near you."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.main import APP_SCOPES, app
from app.models import ClientGrant, Credential
from tests.test_barcodes import apple_monitor
from tests.test_coop import (
    KEY,
    coop,  # noqa: F401  (a fixture: the simulated Coop and an admin client)
)
from tests.test_politeness import job_of
from tests.test_regressions import admin_client
from tests.test_users_admin import PASSWORD, register, sign_in


@pytest.fixture(autouse=True)
def coop_key(monkeypatch):
    monkeypatch.setenv("COOP_API_KEY", KEY)


def person(admin, email="phone@example.invalid"):
    client, user_id, _ = register(admin, email)
    sign_in(client, email)
    return client, user_id


# ---- editing an alert price -----------------------------------------------------------------------

def test_the_alert_price_can_be_set_changed_and_removed(cg):
    mid, _ = apple_monitor(cg)
    path = f"/v1/monitors/{mid}"
    before = cg.client.get(path).json()
    assert "maximum_price" not in before["conditions"]

    changed = cg.client.patch(path, json={"maximum_price": "17.5"})
    assert changed.status_code == 200 and changed.json()["conditions"]["maximum_price"] == "17.5"
    assert changed.json()["conditions"]["listing_id"] == before["conditions"]["listing_id"]  # the rest is kept
    assert cg.client.get(path).json()["conditions"]["maximum_price"] == "17.5"

    assert cg.client.patch(path, json={"maximum_price": 12}).json()["conditions"]["maximum_price"] == "12"
    kept = cg.client.patch(path, json={"name": "Apples"}).json()          # a field left out is kept
    assert kept["name"] == "Apples" and kept["conditions"]["maximum_price"] == "12"
    removed = cg.client.patch(path, json={"maximum_price": None}).json()  # null removes it
    assert "maximum_price" not in removed["conditions"]


@pytest.mark.parametrize("bad", ["cheap", "-1", "NaN", "Infinity"])
def test_a_bad_alert_price_is_refused_and_nothing_changes(cg, bad):
    mid, _ = apple_monitor(cg)
    cg.client.patch(f"/v1/monitors/{mid}", json={"maximum_price": "20"})
    assert cg.client.patch(f"/v1/monitors/{mid}", json={"maximum_price": bad}).status_code == 422
    assert cg.client.get(f"/v1/monitors/{mid}").json()["conditions"]["maximum_price"] == "20"


def test_the_check_interval_cannot_go_below_what_the_retailer_allows(cg):
    mid, _ = apple_monitor(cg)
    refused = cg.client.patch(f"/v1/monitors/{mid}", json={"interval_minutes": 15})
    assert refused.status_code == 422 and "60 minutes" in refused.json()["message"]
    assert cg.client.patch(f"/v1/monitors/{mid}", json={"interval_minutes": 120}).json()["interval_minutes"] == 120


def test_only_the_owner_can_edit_and_a_removed_monitor_cannot_be_edited(cg):
    mid, _ = apple_monitor(cg)
    other, _ = person(cg.client)
    assert other.patch(f"/v1/monitors/{mid}", json={"maximum_price": "1"}).status_code == 404
    assert cg.client.delete(f"/v1/monitors/{mid}").status_code == 204
    assert cg.client.patch(f"/v1/monitors/{mid}", json={"maximum_price": "1"}).status_code == 409


def test_a_read_only_client_cannot_edit(cg):
    mid, _ = apple_monitor(cg)
    client = cg.client.post("/v1/clients", json={"name": "reader", "scopes": ["monitors:read"]}).json()
    cg.client.post("/v1/me/client-grants", json={"client_id": client["client_id"], "scopes": ["monitors:read"]})
    response = cg.client.patch(f"/v1/monitors/{mid}", json={"maximum_price": "1"},
                               headers={"X-Client-Token": client["client_token"]})
    assert response.status_code == 403


# ---- an app signing itself in ---------------------------------------------------------------------

def test_a_person_signs_an_app_in_with_only_their_own_login(fresh_db):
    admin = admin_client()
    phone, _ = person(admin)
    made = phone.post("/v1/me/app-credentials", json={"name": "Price Watch Android (Pixel 9a)"})
    assert made.status_code == 201
    app_creds = made.json()
    assert app_creds["scopes"] == APP_SCOPES and app_creds["client_token"]

    # the app now works with both tokens, and sees exactly the app scopes
    both = {"X-Client-Token": app_creds["client_token"]}
    me = phone.get("/v1/me", headers=both).json()
    assert me["client_id"] == app_creds["client_id"] and sorted(me["scopes"]) == sorted(APP_SCOPES)
    assert phone.get("/v1/monitors", headers=both).status_code == 200
    # and cannot do what an app must never do
    assert phone.post("/v1/webhooks", json={"url": "https://example.org/h"}, headers=both).status_code == 403
    assert phone.post("/v1/me/app-credentials", json={"name": "x"}, headers=both).status_code == 403


def test_an_app_can_never_have_more_than_the_person_has(fresh_db):
    admin = admin_client()
    phone, _ = person(admin)
    token = phone.headers["Authorization"]
    assert "admin" not in phone.post("/v1/me/app-credentials", json={"name": "x"}).json()["scopes"]
    # without a user token there is nothing to sign in as
    assert TestClient(app).post("/v1/me/app-credentials", json={"name": "x"}).status_code == 401
    assert token  # (the person's own token is what authorised the call)


def test_signing_the_app_out_revokes_it_and_only_its_owner_can(fresh_db):
    admin = admin_client()
    phone, _ = person(admin)
    other, _ = person(admin, "other@example.invalid")
    creds = phone.post("/v1/me/app-credentials", json={"name": "Phone"}).json()
    both = {"X-Client-Token": creds["client_token"]}

    assert other.delete(f"/v1/me/app-credentials/{creds['client_id']}").status_code == 404
    assert phone.get("/v1/monitors", headers=both).status_code == 200
    assert phone.delete(f"/v1/me/app-credentials/{creds['client_id']}").status_code == 204
    assert phone.get("/v1/monitors", headers=both).status_code == 401  # the credential is revoked
    assert phone.delete(f"/v1/me/app-credentials/{creds['client_id']}").status_code == 204  # idempotent


def test_the_oldest_app_sign_in_is_replaced_beyond_the_limit(fresh_db, monkeypatch):
    monkeypatch.setattr(settings, "max_app_credentials_per_user", 2)
    admin = admin_client()
    phone, _ = person(admin)
    made = [phone.post("/v1/me/app-credentials", json={"name": f"Phone {i}"}).json() for i in range(3)]
    status = [phone.get("/v1/monitors", headers={"X-Client-Token": m["client_token"]}).status_code for m in made]
    assert status == [401, 200, 200]
    with SessionLocal() as db:
        live = db.scalars(select(Credential).where(Credential.kind == "client", Credential.revoked_at.is_(None))).all()
        assert len(live) == 2
        assert all(g.revoked_at for g in db.scalars(select(ClientGrant).where(ClientGrant.client_id == made[0]["client_id"])))


def test_logging_in_still_needs_the_password(fresh_db):
    admin = admin_client()
    person(admin, "someone@example.invalid")
    bad = TestClient(app).post("/v1/auth/login", json={"email": "someone@example.invalid", "password": PASSWORD + "x"})
    assert bad.status_code == 401


# ---- finding the stores near you ------------------------------------------------------------------

def test_a_person_can_find_stores_by_postcode_and_gets_their_ids(coop):  # noqa: F811
    phone, _ = person(coop.client)
    missing = phone.post("/v1/retailers/se.coop/stores/discover")
    assert missing.status_code == 422 and "postcode" in missing.json()["message"]
    asked = phone.post("/v1/retailers/se.coop/stores/discover", json={"postcode": "111 20"})
    assert asked.status_code == 202 and asked.json()["status_url"].startswith("/v1/jobs/")
    assert coop.site.calls == []  # the API never contacts the retailer itself

    from app.worker import run_job
    assert run_job()
    result = phone.get(asked.json()["status_url"]).json()["result"]
    assert result["status"] == "success" and len(result["store_ids"]) == result["stores"] == 1
    store_id = result["store_ids"][0]
    assert any(s["id"] == store_id for s in phone.get("/v1/stores").json()["items"])
    assert phone.put(f"/v1/me/stores/{store_id}").status_code == 204


def test_each_person_has_their_own_store_lookup_and_a_limit_per_hour(coop, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "max_store_lookups_per_hour", 2)
    a, _ = person(coop.client, "a@example.invalid")
    b, _ = person(coop.client, "b@example.invalid")
    first = a.post("/v1/retailers/se.coop/stores/discover", json={"postcode": "11120"}).json()
    same = b.post("/v1/retailers/se.coop/stores/discover", json={"postcode": "11120"}).json()
    assert first["job_id"] != same["job_id"]          # b is not handed a's job
    assert b.get(f"/v1/jobs/{first['job_id']}").status_code == 404
    assert a.post("/v1/retailers/se.coop/stores/discover", json={"postcode": "11121"}).status_code == 202
    limited = a.post("/v1/retailers/se.coop/stores/discover", json={"postcode": "11122"})
    assert limited.status_code == 429 and limited.headers["retry-after"]


def test_a_read_only_client_cannot_ask_for_stores_to_be_looked_up(coop):  # noqa: F811
    client = coop.client.post("/v1/clients", json={"name": "reader", "scopes": ["monitors:read"]}).json()
    coop.client.post("/v1/me/client-grants", json={"client_id": client["client_id"], "scopes": ["monitors:read"]})
    response = coop.client.post("/v1/retailers/se.coop/stores/discover", json={"postcode": "11120"},
                                headers={"X-Client-Token": client["client_token"]})
    assert response.status_code == 403


def test_the_administrators_lookup_is_unchanged_and_not_limited(coop, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "max_store_lookups_per_hour", 0)
    first = coop.client.post("/v1/admin/retailers/se.coop/stores/discover", json={"postcode": "11120"})
    assert first.status_code == 202
    assert job_of(coop, first)["result"]["status"] == "success"

"""Adding a product from its page link, and removing monitors (history kept by default)."""
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from app.config import settings
from app.connectors import registry
from app.connectors.manifest import ProductUrl
from app.connectors.urls import MAX_URL_LENGTH, match_product_url, normalize_product_url
from app.db import SessionLocal
from app.main import app
from app.models import Delivery, EvaluationState, Event, IdempotencyRecord, Job, Monitor, Store
from app.worker import run_job, schedule_due
from tests.test_politeness import check, job_of, listing_id, monitor

APPLE = "https://www.citygross.se/matvaror/frukt-och-gront/frukt/%C3%A4pple-ingrid-marie-p100142800_KG"
APPLE_SKU = "100142800_KG"
OATS_SKU = "100132321_ST"


def rules(retailer_id: str) -> list[dict]:
    return registry.retailer(retailer_id)["product_urls"]


# ---- reading links -------------------------------------------------------------------------

@pytest.mark.parametrize("url,sku", [
    (APPLE, APPLE_SKU),                                                              # the real link
    (APPLE.replace("%C3%A4", "ä"), APPLE_SKU),                                       # already decoded
    (APPLE + "/", APPLE_SKU),                                                        # trailing slash
    (APPLE + "?utm_source=mail&x=1#reviews", APPLE_SKU),                             # query and fragment
    ("  " + APPLE.replace("www.citygross", "WWW.CITYGROSS") + " ", APPLE_SKU),        # host case, padding
    ("https://www.citygross.se/matvaror/skafferiet/axa-havregryn-p100132321_ST", OATS_SKU),
    ("https://citygross.se/matvaror/x/y-p100132321_ST", OATS_SKU),                    # no www
])
def test_city_gross_links_yield_the_product_id(url, sku):
    assert match_product_url(rules("se.citygross"), url) == sku


def test_coop_links_yield_the_ean():
    url = "https://www.coop.se/handla/varor/skafferi/flingor-musli/gryn/havregryn-7300156599452/"
    assert match_product_url(rules("se.coop"), url) == "7300156599452"
    assert match_product_url(rules("se.coop"), url + "?store=1") == "7300156599452"


@pytest.mark.parametrize("url,fragment", [
    ("http://www.citygross.se/matvaror/x-p100132321_ST", "https://"),                # not https
    ("ftp://www.citygross.se/x-p1_ST", "https://"),
    ("javascript:alert(1)", "https://"),
    ("https://user:pw@www.citygross.se/x-p100132321_ST", "username or password"),
    ("https://www.citygross.se:8443/x-p100132321_ST", "product link"),               # other port
    ("https://www.citygross.se/x-p100132321_ST\nHost: evil", "product link"),         # header injection
    ("https://www.citygross.se/x p100132321_ST", "product link"),                    # whitespace
    ("", "product link"),
    ("https://www.citygross.se/" + "a" * MAX_URL_LENGTH, "product link"),
])
def test_unsafe_or_malformed_links_are_refused(url, fragment):
    with pytest.raises(ValueError, match=fragment):
        normalize_product_url(url)


@pytest.mark.parametrize("url", [
    "https://www.citygross.se/",                                    # home page
    "https://www.citygross.se/matvaror/frukt-och-gront/frukt",      # a category, not a product
    "https://www.citygross.se/matvaror/x-pabc_ST",                  # no digits
    "https://evil.example/matvaror/x-p100132321_ST",                # another site
    "https://www.citygross.se.evil.example/x-p100132321_ST",        # look-alike host
    "https://www.coop.se/handla/varor/x-123",                       # too short for an EAN
])
def test_links_that_are_not_product_pages_match_nothing(url):
    assert match_product_url(rules("se.citygross"), url) is None
    assert match_product_url(rules("se.coop"), url) is None


def test_the_id_is_never_taken_from_the_query_string_or_other_hosts():
    sneaky = "https://www.citygross.se/matvaror/category?next=https://x/y-p100132321_ST"
    assert match_product_url(rules("se.citygross"), sneaky) is None


@pytest.mark.parametrize("kwargs,fragment", [
    ({"pattern": "^https://x\\.example\\.org/(?P<id>[0-9]+)$"}, "named group"),
    ({"pattern": "^https://x\\.example\\.org/(?P<sku>[0-9]+)$", "example": "https://x.example.org/abc"},
     "own example"),
    ({"pattern": "^(unclosed"}, "not a valid regular expression"),
    ({"pattern": "x"}, "at least 5"),
])
def test_a_bad_product_url_rule_is_refused_when_the_manifest_loads(kwargs, fragment):
    with pytest.raises(ValidationError, match=fragment):
        ProductUrl(**kwargs)


# ---- adding a product from a link -----------------------------------------------------------

def add(cg, url=APPLE, **extra):
    return cg.client.post("/v1/monitors/from-url", json={"url": url, "store_id": cg.store_id, **extra})


def test_a_link_adds_the_product_and_a_monitor_in_one_step(cg):
    queued = add(cg)
    assert queued.status_code == 202
    assert cg.site.calls == []  # the API itself never contacts the retailer, and never fetches the page
    job = job_of(cg, queued)
    result = job["result"]
    assert job["state"] == "succeeded" and result["already_monitored"] is False
    assert result["product"]["name"] == "Äpple Ingrid Marie CA140G KLASS 1"
    assert result["product"]["current_price"] == "34.95"
    assert result["product"]["unit_price"] == {"amount": "34.95", "unit": "kg"}  # per kg, not per apple
    (call,) = cg.site.calls
    assert call.url.params["SearchQuery"] == APPLE_SKU and call.url.params["store"] == "3204"
    assert call.url.host == "www.citygross.se" and "p100142800" not in str(call.url)

    monitors = cg.client.get("/v1/monitors").json()["items"]
    assert [m["id"] for m in monitors] == [result["monitor_id"]]
    assert monitors[0]["name"] == "Äpple Ingrid Marie CA140G KLASS 1" and monitors[0]["type"] == "exact_product"
    history = cg.client.get(f"/v1/listings/{result['product']['listing_id']}/prices").json()["items"]
    assert [h["current_price"] for h in history] == ["34.95"]
    assert check(cg, result["monitor_id"])["state"] == "succeeded"  # the monitor really works


def test_adding_the_same_product_again_returns_the_existing_monitor_without_any_request(cg):
    first = job_of(cg, add(cg))["result"]["monitor_id"]
    calls = len(cg.site.calls)
    again = add(cg)
    assert again.status_code == 200
    assert again.json()["already_monitored"] is True and again.json()["monitor"]["id"] == first
    assert len(cg.site.calls) == calls
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Monitor)) == 1


def test_name_threshold_and_interval_are_applied(cg):
    result = job_of(cg, add(cg, name="Apples", maximum_price="30", interval_minutes=120))["result"]
    created = cg.client.get(f"/v1/monitors/{result['monitor_id']}").json()
    assert created["name"] == "Apples" and created["interval_minutes"] == 120
    assert created["conditions"]["maximum_price"] == "30"


def test_a_product_the_store_does_not_list_creates_nothing(cg):
    gone = "https://www.citygross.se/matvaror/x/y-p999999999_ST"
    job = job_of(cg, add(cg, gone))
    assert job["state"] == "not_found" and "does not list that product" in job["result"]["detail"]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Monitor)) == 0


def test_a_paused_or_disabled_connector_is_respected(cg):
    cg.client.post("/v1/admin/connectors/se.citygross/pause")
    job = job_of(cg, add(cg))
    assert job["state"] == "paused" and cg.site.calls == []
    cg.client.post("/v1/admin/connectors/se.citygross/resume")
    cg.client.post("/v1/admin/connectors/se.citygross/disable")
    refused = add(cg)
    assert refused.status_code == 422 and "disabled" in refused.json()["message"]


@pytest.mark.parametrize("body,status,fragment", [
    ({"url": "https://example.org/some/page"}, 422, "not a product page on a supported store"),
    ({"url": "http://www.citygross.se/x-p100132321_ST"}, 422, "https://"),
    ({"url": APPLE, "maximum_price": "cheap"}, 422, "number"),
    ({"url": APPLE, "maximum_price": "-1"}, 422, "zero or more"),
    ({"url": APPLE, "interval_minutes": 15}, 422, "60 minutes"),
    ({"url": APPLE, "store_id": "no-such-store"}, 404, "Store not found"),
])
def test_bad_requests_are_refused_clearly(cg, body, status, fragment):
    body = {"store_id": cg.store_id, **body}
    response = cg.client.post("/v1/monitors/from-url", json=body)
    assert response.status_code == status and fragment in response.json()["message"]
    assert cg.site.calls == []


def test_the_store_must_be_unambiguous_and_belong_to_the_links_retailer(cg):
    only = cg.client.post("/v1/monitors/from-url", json={"url": APPLE})  # one City Gross store
    assert only.status_code == 202
    with SessionLocal.begin() as db:
        db.add(Store(retailer_id="se.citygross", external_id="3206", name="City Gross Eskilstuna"))
    many = cg.client.post("/v1/monitors/from-url", json={"url": APPLE})
    assert many.status_code == 422 and "send store_id" in many.json()["message"] and "2 registered" in many.json()["message"]
    fixture_store = next(s for s in cg.client.get("/v1/stores").json()["items"] if s["retailer_id"] == "test.fixture")
    wrong = cg.client.post("/v1/monitors/from-url", json={"url": APPLE, "store_id": fixture_store["id"]})
    assert wrong.status_code == 422 and "different retailer" in wrong.json()["message"]
    with SessionLocal.begin() as db:
        db.query(Store).filter(Store.retailer_id == "se.citygross").delete()
    none = cg.client.post("/v1/monitors/from-url", json={"url": APPLE})
    assert none.status_code == 422 and "register one first" in none.json()["message"]


def test_replays_with_an_idempotency_key_return_the_same_job_and_a_changed_body_conflicts(cg):
    headers = {"Idempotency-Key": "add-apple"}
    first = cg.client.post("/v1/monitors/from-url", headers=headers, json={"url": APPLE, "store_id": cg.store_id})
    second = cg.client.post("/v1/monitors/from-url", headers=headers, json={"url": APPLE, "store_id": cg.store_id})
    assert first.json()["job_id"] == second.json()["job_id"]
    other = cg.client.post("/v1/monitors/from-url", headers=headers,
                           json={"url": APPLE, "store_id": cg.store_id, "name": "different"})
    assert other.status_code == 409
    assert run_job() and not run_job()  # one job, run once
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == "add_product")) == 1


def test_the_monitor_limit_applies_at_the_api_and_in_the_worker(cg, monkeypatch):
    monkeypatch.setattr(settings, "max_monitors_per_user", 1)
    other = "https://www.citygross.se/matvaror/skafferiet/axa-havregryn-p100132321_ST"
    job_of(cg, add(cg))
    refused = add(cg, other)
    assert refused.status_code == 429 and "remove one" in refused.json()["message"]
    queued = add(cg, other)  # the limit is checked again by the worker; simulate the race
    assert queued.status_code == 429
    cg.client.delete(f"/v1/monitors/{cg.client.get('/v1/monitors').json()['items'][0]['id']}")
    assert add(cg, other).status_code == 202  # removing one frees the slot


def test_a_read_only_client_cannot_add_products(cg):
    client = cg.client.post("/v1/clients", json={"name": "reader", "scopes": ["monitors:read"]}).json()
    cg.client.post("/v1/me/client-grants", json={"client_id": client["client_id"], "scopes": ["monitors:read"]})
    response = cg.client.post("/v1/monitors/from-url", json={"url": APPLE, "store_id": cg.store_id},
                              headers={"X-Client-Token": client["client_token"]})
    assert response.status_code == 403


# ---- removing a monitor ---------------------------------------------------------------------

def make_monitor_with_events(cg):
    lid = listing_id(cg, "gyllenhamars")
    mid = monitor(cg, listing_id=lid).json()["id"]
    assert check(cg, mid)["state"] == "succeeded"
    assert [e["type"] for e in cg.client.get(f"/v1/monitors/{mid}/events").json()["items"]] == ["offer.detected"]
    return mid, lid


def test_removing_stops_and_hides_the_monitor_but_keeps_its_history(cg):
    mid, lid = make_monitor_with_events(cg)
    assert cg.client.delete(f"/v1/monitors/{mid}").status_code == 204
    assert cg.client.get("/v1/monitors").json()["items"] == []                      # hidden
    removed = cg.client.get("/v1/monitors?include_removed=true").json()["items"]
    assert [m["id"] for m in removed] == [mid] and removed[0]["removed_at"] and removed[0]["enabled"] is False
    assert cg.client.get(f"/v1/monitors/{mid}").json()["removed_at"]                  # still readable
    assert len(cg.client.get(f"/v1/monitors/{mid}/events").json()["items"]) == 1      # history kept
    assert [e["type"] for e in cg.client.get("/v1/events").json()["items"]] == ["offer.detected"]
    assert cg.client.post(f"/v1/monitors/{mid}/check").status_code == 404           # no more checks
    assert cg.client.delete(f"/v1/monitors/{mid}").status_code == 204                # idempotent
    with SessionLocal.begin() as db:  # make everything overdue: a removed monitor still must not run
        for row in db.scalars(select(Monitor)):
            row.next_due_at = row.next_due_at.replace(year=2020)
    assert schedule_due() == 0                                                       # never scheduled
    assert cg.client.get(f"/v1/listings/{lid}/prices").json()["items"]               # price history stays


def test_removing_frees_a_monitor_slot_and_re_adding_creates_a_fresh_monitor(cg, monkeypatch):
    monkeypatch.setattr(settings, "max_monitors_per_user", 1)
    first = job_of(cg, add(cg))["result"]["monitor_id"]
    cg.client.delete(f"/v1/monitors/{first}")
    again = job_of(cg, add(cg))["result"]
    assert again["already_monitored"] is False and again["monitor_id"] != first
    assert len(cg.client.get("/v1/monitors?include_removed=true").json()["items"]) == 2


def test_permanent_deletion_removes_the_monitor_its_events_and_its_state_but_not_price_history(cg):
    mid, lid = make_monitor_with_events(cg)
    key = {"Idempotency-Key": "make-it"}
    created = cg.client.post("/v1/monitors", headers=key, json={
        "name": "x", "type": "exact_product", "store_id": cg.store_id, "conditions": {"listing_id": lid}})
    assert created.status_code == 201
    assert cg.client.delete(f"/v1/monitors/{mid}?permanent=true").status_code == 204
    assert cg.client.get(f"/v1/monitors/{mid}").status_code == 404
    assert cg.client.get("/v1/events").json()["items"] == []
    with SessionLocal() as db:
        for model in (Event, Delivery, EvaluationState):
            assert db.scalar(select(func.count()).select_from(model)) == 0, model
        assert db.scalar(select(func.count()).select_from(Job).where(Job.monitor_id == mid)) == 0
        assert db.scalar(select(func.count()).select_from(IdempotencyRecord).where(
            IdempotencyRecord.resource_id == mid)) == 0
    assert cg.client.get(f"/v1/listings/{lid}/prices").json()["items"]               # catalogue kept
    cg.client.delete(f"/v1/monitors/{created.json()['id']}?permanent=true")
    replay = cg.client.post("/v1/monitors", headers=key, json={
        "name": "x", "type": "exact_product", "store_id": cg.store_id, "conditions": {"listing_id": lid}})
    assert replay.status_code == 201  # a stale idempotency record must not break a re-create


def test_a_queued_job_of_a_permanently_deleted_monitor_does_not_break_the_worker(cg):
    mid, _ = make_monitor_with_events(cg)
    assert cg.client.post(f"/v1/monitors/{mid}/check").status_code == 202
    cg.client.delete(f"/v1/monitors/{mid}?permanent=true")
    assert run_job() is False  # the job went with the monitor; nothing to run, nothing raised


def test_you_cannot_remove_someone_elses_monitor(cg):
    mid, _ = make_monitor_with_events(cg)
    token = cg.client.post("/v1/admin/invitations", json={"email": "other@example.invalid"}).json()
    other = TestClient(app)
    other.post("/v1/auth/accept-invitation", json={"token": token["invitation_token"],
                                                   "password": "another-long-password"})
    other.headers["Authorization"] = "Bearer " + other.post("/v1/auth/login", json={
        "email": "other@example.invalid", "password": "another-long-password"}).json()["access_token"]
    for path in (f"/v1/monitors/{mid}", f"/v1/monitors/{mid}?permanent=true"):
        assert other.delete(path).status_code == 404
    assert cg.client.get(f"/v1/monitors/{mid}").json()["removed_at"] is None

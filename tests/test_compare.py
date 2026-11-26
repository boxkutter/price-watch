"""Standing in a store with a barcode: compare the same item across your other stores."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import settings
from app.connectors import registry
from app.db import SessionLocal
from app.models import Job, Store
from app.worker import run_job
from tests.test_barcodes import AXA, ean13
from tests.test_coop import KEY, FakeCoop
from tests.test_coop import connector as coop_connector
from tests.test_politeness import job_of

AXA_AT_COOP = {"id": AXA, "ean": AXA, "name": "Havregryn", "manufacturerName": "AXA",
               "packageSize": 1500.0, "packageSizeInformation": "1500 g", "packageSizeUnit": "Gram",
               "salesPriceData": {"b2cPrice": 17.5, "b2bPrice": 16.5}, "availableOnline": True}


@pytest.fixture
def shops(cg, monkeypatch):
    """Two retailers that sell the same barcode: City Gross at 19.95, Coop at 17.50."""
    monkeypatch.setenv("COOP_API_KEY", KEY)
    coop = FakeCoop()
    coop.items.append(AXA_AT_COOP)
    monkeypatch.setitem(registry.CONNECTORS, "se.coop", coop_connector(coop))
    with SessionLocal.begin() as db:
        store = Store(retailer_id="se.coop", external_id="255700", name="Coop Häggvik")
        db.add(store)
        db.flush()
        coop_store = store.id
    return SimpleNamespace(client=cg.client, cg=cg, cg_site=cg.site, coop=coop,
                           cg_store=cg.store_id, coop_store=coop_store, store_id=cg.store_id)


def compare(sh, **body):
    return sh.client.post("/v1/compare/barcode", json={"gtin": AXA, **body})


def mine(sh, *store_ids):
    for store_id in store_ids:
        assert sh.client.put(f"/v1/me/stores/{store_id}").status_code == 204


def rows(result):
    return {r["retailer_id"]: r for r in result["rows"]}


# ---- my stores ---------------------------------------------------------------------------------

def test_you_choose_the_stores_you_compare_across(shops):
    assert shops.client.get("/v1/me/stores").json()["items"] == []
    mine(shops, shops.cg_store, shops.coop_store, shops.coop_store)        # idempotent
    items = shops.client.get("/v1/me/stores").json()["items"]
    assert sorted(i["retailer_id"] for i in items) == ["se.citygross", "se.coop"]
    assert shops.client.put("/v1/me/stores/no-such").status_code == 404
    assert shops.client.delete(f"/v1/me/stores/{shops.coop_store}").status_code == 204
    assert [i["retailer_id"] for i in shops.client.get("/v1/me/stores").json()["items"]] == ["se.citygross"]


def test_the_number_of_stores_per_person_is_capped(shops, monkeypatch):
    monkeypatch.setattr(settings, "max_my_stores", 1)
    mine(shops, shops.cg_store)
    assert shops.client.put(f"/v1/me/stores/{shops.coop_store}").status_code == 429


# ---- comparing -------------------------------------------------------------------------------

def test_scanning_in_one_store_finds_the_same_item_in_the_others_and_shows_the_saving(shops):
    mine(shops, shops.cg_store, shops.coop_store)
    queued = compare(shops, here_store_id=shops.cg_store)
    assert queued.status_code == 202
    first = queued.json()
    assert [r["status"] for r in first["known"]] == ["unknown", "unknown"]  # nothing known yet
    assert first["known"][0]["store_id"] == shops.cg_store                 # the store you are in is first
    assert shops.cg_site.calls == [] and shops.coop.calls == []             # the API itself contacts nobody

    job = job_of(shops, queued)
    result = job["result"]
    assert job["state"] == "succeeded" and result["complete"] and result["requests"] == 2
    by = rows(result)
    assert by["se.citygross"]["current_price"] == "19.95" and by["se.coop"]["current_price"] == "17.50"
    assert by["se.coop"]["is_cheapest"] and not by["se.citygross"]["is_cheapest"]
    assert result["cheapest"]["store_name"] == "Coop Häggvik"
    assert result["saving_vs_here"] == {"amount": "-2.45", "percent": "-12.3"}   # cheaper than where you stand
    assert by["se.citygross"]["vs_cheapest"] == {"amount": "2.45", "percent": "14.0"}
    assert by["se.coop"]["unit_price"] == {"amount": "11.67", "unit": "kg"}
    assert len(shops.cg_site.calls) == 1 and len(shops.coop.calls) == 1        # exactly one request per store
    assert result["product"]["barcode"]["source"] == "retailer"
    assert result["product"]["retailers"] == ["se.citygross", "se.coop"] and result["product"]["listings"] == 2


def test_the_next_scan_answers_instantly_from_the_catalogue_and_refreshes_from_the_cache(shops):
    mine(shops, shops.cg_store, shops.coop_store)
    job_of(shops, compare(shops, here_store_id=shops.cg_store))
    calls = len(shops.cg_site.calls) + len(shops.coop.calls)

    again = compare(shops, here_store_id=shops.cg_store).json()
    known = rows({"rows": again["known"]})
    assert known["se.coop"]["source"] == "catalogue" and known["se.coop"]["current_price"] == "17.50"
    assert known["se.coop"]["age_seconds"] < 60 and again["cheapest"]["store_name"] == "Coop Häggvik"
    assert again["status"] in ("queued", "running")
    refreshed = job_of(shops, SimpleNamespace(status_code=202, json=lambda: again))
    assert refreshed["state"] == "succeeded"
    assert len(shops.cg_site.calls) + len(shops.coop.calls) == calls  # the 10 minute cache: no new requests


def test_known_prices_can_be_asked_for_without_any_live_lookup(shops):
    mine(shops, shops.cg_store, shops.coop_store)
    job_of(shops, compare(shops))
    jobs_before = _job_count()
    instant = compare(shops, refresh=False)
    assert instant.status_code == 200 and "job_id" not in instant.json()
    assert instant.json()["cheapest"]["store_name"] == "Coop Häggvik" and _job_count() == jobs_before


def _job_count() -> int:
    with SessionLocal() as db:
        return len(db.scalars(select(Job)).all())


def test_a_store_that_does_not_sell_it_is_a_clear_answer_not_a_failure(shops):
    mine(shops, shops.cg_store, shops.coop_store)
    shops.coop.items.remove(AXA_AT_COOP)
    result = job_of(shops, compare(shops))["result"]
    by = rows(result)
    assert by["se.coop"]["status"] == "not_found" and "does not list" in by["se.coop"]["detail"]
    assert result["complete"] and result["status"] == "success"
    assert result["cheapest"]["store_name"] == "City Gross Borås"


def test_a_paused_store_is_reported_and_never_contacted(shops):
    mine(shops, shops.cg_store, shops.coop_store)
    shops.client.post("/v1/admin/connectors/se.coop/pause")
    result = job_of(shops, compare(shops))["result"]
    assert rows(result)["se.coop"]["status"] == "paused" and shops.coop.calls == []
    assert result["status"] == "partial" and not result["complete"]
    assert result["cheapest"]["store_name"] == "City Gross Borås"  # the others still answer


def test_a_store_whose_connector_cannot_search_by_barcode_uses_linked_products(shops, monkeypatch):
    mine(shops, shops.cg_store, shops.coop_store)
    job_of(shops, compare(shops))                      # learn both listings (and so the mapping)
    capabilities = registry.retailer("se.coop")["capabilities"]
    monkeypatch.setitem(capabilities, "barcode_search", "unsupported")
    shops.coop.calls.clear()
    retrieval_clear()
    via_mapping = job_of(shops, compare(shops))["result"]
    assert rows(via_mapping)["se.coop"]["status"] == "found"
    body = shops.coop.calls[-1].content.decode()
    assert AXA in body and "entities/by-id" in str(shops.coop.calls[-1].url)  # asked by linked product id

    unknown = ean13("400638133393")                    # a product no store has been linked for
    nothing = job_of(shops, compare(shops, gtin=unknown))["result"]
    assert rows(nothing)["se.coop"]["status"] == "unknown_mapping"
    assert "link" in rows(nothing)["se.coop"]["detail"] and nothing["status"] == "partial"


def retrieval_clear():
    from app import retrieval
    retrieval.clear_cache()


def test_a_disabled_connector_is_skipped(shops):
    mine(shops, shops.cg_store, shops.coop_store)
    shops.client.post("/v1/admin/connectors/se.coop/disable")
    result = job_of(shops, compare(shops))["result"]
    assert rows(result)["se.coop"]["status"] == "disabled" and shops.coop.calls == []


@pytest.mark.parametrize("body,status,fragment", [
    ({"gtin": "4006381333932"}, 422, "check digit"),
    ({"gtin": ean13("209021910000")}, 422, "store-internal"),
    ({"gtin": AXA}, 422, "Choose the stores"),                        # no stores chosen yet
    ({"gtin": AXA, "store_ids": ["no-such-store"]}, 404, "Store not found"),
])
def test_bad_comparisons_are_refused_clearly(shops, body, status, fragment):
    response = shops.client.post("/v1/compare/barcode", json=body)
    assert response.status_code == status and fragment in response.json()["message"]
    assert shops.cg_site.calls == [] and shops.coop.calls == []


def test_the_number_of_stores_checked_live_is_capped(shops, monkeypatch):
    monkeypatch.setattr(settings, "max_compare_stores", 1)
    mine(shops, shops.cg_store, shops.coop_store)
    refused = compare(shops)
    assert refused.status_code == 422 and "At most 1 stores" in refused.json()["message"]
    assert compare(shops, store_ids=[shops.cg_store]).status_code == 202


def test_comparisons_share_the_per_user_lookup_budget_with_searches(shops, monkeypatch):
    mine(shops, shops.cg_store, shops.coop_store)
    monkeypatch.setattr(settings, "max_pending_searches_per_user", 1)
    assert compare(shops).status_code == 202
    busy = shops.client.post("/v1/search/jobs", json={"query": "havre", "store_ids": [shops.cg_store]})
    assert busy.status_code == 429 and busy.headers["Retry-After"] == "60"
    assert compare(shops).status_code == 429
    assert run_job()


def test_repeating_a_scan_with_an_idempotency_key_returns_the_same_job(shops):
    mine(shops, shops.cg_store, shops.coop_store)
    headers = {"Idempotency-Key": "scan-1"}
    body = {"gtin": AXA, "here_store_id": shops.cg_store}
    first = shops.client.post("/v1/compare/barcode", headers=headers, json=body)
    second = shops.client.post("/v1/compare/barcode", headers=headers, json=body)
    assert first.json()["job_id"] == second.json()["job_id"]
    assert shops.client.post("/v1/compare/barcode", headers=headers, json={**body, "refresh": False}
                             ).status_code == 409


def test_a_barcode_search_also_uses_linked_products_for_stores_without_barcode_search(shops, monkeypatch):
    mine(shops, shops.cg_store, shops.coop_store)
    job_of(shops, compare(shops))
    monkeypatch.setitem(registry.retailer("se.coop")["capabilities"], "barcode_search", "unsupported")
    retrieval_clear()
    shops.coop.calls.clear()
    found = job_of(shops, shops.client.post("/v1/search/jobs", json={
        "gtin": AXA, "store_ids": [shops.coop_store]}))["result"]
    assert found["items"] and found["items"][0]["name"].startswith("Havregryn")
    unmapped = job_of(shops, shops.client.post("/v1/search/jobs", json={
        "gtin": ean13("400638133393"), "store_ids": [shops.coop_store]}))["result"]
    assert unmapped["coverage"][0]["status"] == "unknown_mapping" and unmapped["items"] == []

# ruff: noqa: F811  (fixtures imported from another test module are used by name)
"""The whole point is the cheapest deal for *you*: member prices, percentages and multi-buys all count,
and membership is for a chain (ICA, City Gross), not for one shop."""
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import Offer, Store, utcnow
from tests.test_compare import compare, mine, rows, shops  # noqa: F401
from tests.test_politeness import job_of
from tests.test_users_admin import register, sign_in

D = Decimal


def put_offer(retailer_id: str, store_id: str, sku: str, terms: str, eligibility: str = "public", promo: str = "x"):
    with SessionLocal.begin() as db:
        db.add(Offer(retailer_id=retailer_id, store_id=store_id, channel="online", identity_key=f"{sku}:{promo}",
                     title="Havregryn", terms=terms, eligibility=eligibility, price=None, currency="SEK",
                     content_hash=promo, status="active", first_seen=utcnow(), last_seen=utcnow()))


# ---- saying you are a member ------------------------------------------------------------------------------

def test_membership_is_for_a_chain_and_covers_all_its_stores(shops):
    with SessionLocal.begin() as db:
        db.add(Store(retailer_id="se.citygross", external_id="9999", name="City Gross Malmö"))
        second_cg = db.scalar(select(Store.id).where(Store.external_id == "9999"))
    mine(shops, shops.cg_store, second_cg, shops.coop_store)
    assert shops.client.get("/v1/me/memberships").json() == {"items": []}
    assert {s["retailer_id"]: s["member"] for s in shops.client.get("/v1/me/stores").json()["items"]} == {
        "se.citygross": False, "se.coop": False}

    assert shops.client.put("/v1/me/memberships/se.citygross").status_code == 204
    assert shops.client.put("/v1/me/memberships/se.citygross").status_code == 204        # saying it again changes nothing
    assert shops.client.get("/v1/me/memberships").json() == {"items": [{"retailer_id": "se.citygross"}]}
    stores = shops.client.get("/v1/me/stores").json()["items"]
    assert [s["member"] for s in stores if s["retailer_id"] == "se.citygross"] == [True, True]   # both shops of the chain
    assert [s["member"] for s in stores if s["retailer_id"] == "se.coop"] == [False]

    assert shops.client.delete("/v1/me/memberships/se.citygross").status_code == 204
    assert shops.client.delete("/v1/me/memberships/se.citygross").status_code == 204
    assert shops.client.get("/v1/me/memberships").json() == {"items": []}
    assert shops.client.put("/v1/me/memberships/se.nowhere").status_code == 404


def test_membership_is_personal_and_needs_the_right_scope(shops):
    shops.client.put("/v1/me/memberships/se.citygross")
    other = TestClient(app)
    register(shops.client, "other@example.invalid")
    sign_in(other, "other@example.invalid")
    assert other.get("/v1/me/memberships").json() == {"items": []}
    reader = shops.client.post("/v1/clients", json={"name": "r", "scopes": ["monitors:read"]}).json()
    shops.client.post("/v1/me/client-grants", json={"client_id": reader["client_id"], "scopes": ["monitors:read"]})
    headers = {"X-Client-Token": reader["client_token"]}
    assert shops.client.get("/v1/me/memberships", headers=headers).status_code == 200
    assert shops.client.put("/v1/me/memberships/se.coop", headers=headers).status_code == 403


# ---- comparing by what you would really pay ---------------------------------------------------------------

def known(shops, **body):
    return compare(shops, refresh=False, **body).json()


def seeded(shops):
    """City Gross at 19.95 and Coop at 17.50 on the shelf, known to the catalogue; the SKUs of each."""
    mine(shops, shops.cg_store, shops.coop_store)
    result = job_of(shops, compare(shops, here_store_id=shops.cg_store))["result"]
    return {r["retailer_id"]: r["sku"] for r in result["rows"]}


def test_a_member_price_makes_a_dearer_shelf_price_the_cheapest_deal_for_a_member_only(shops):
    sku = seeded(shops)
    put_offer("se.citygross", shops.cg_store, sku["se.citygross"], "Medlemspris: 15.00 kr", "member", "club")

    plain = known(shops, here_store_id=shops.cg_store)
    by = rows({"rows": plain["known"]})
    assert by["se.citygross"]["effective_price"] == "19.95" and by["se.citygross"]["best"] is None
    assert plain["cheapest"]["store_name"] == "Coop Häggvik"

    assert shops.client.put("/v1/me/memberships/se.citygross").status_code == 204
    member = known(shops, here_store_id=shops.cg_store)
    by = rows({"rows": member["known"]})
    cg = by["se.citygross"]
    assert (cg["current_price"], cg["effective_price"]) == ("19.95", "15.00")       # the shelf price is still shown
    assert cg["best"]["member"] is True and cg["best"]["quantity"] == 1 and cg["best"]["terms"] == "Medlemspris: 15.00 kr"
    assert cg["is_cheapest"] and member["cheapest"]["store_name"] == "City Gross Borås"
    assert member["cheapest"]["effective_price"] == "15.00"
    assert by["se.coop"]["vs_cheapest"] == {"amount": "2.50", "percent": "16.7"}     # measured on what you pay
    assert member["saving_vs_here"] is None                                           # you are already in the cheapest store


def test_a_multi_buy_counts_in_a_comparison_with_the_number_to_buy(shops):
    sku = seeded(shops)
    put_offer("se.coop", shops.coop_store, sku["se.coop"], "2 för 28 kr")             # 14,00 each
    result = known(shops, here_store_id=shops.cg_store)
    coop = rows({"rows": result["known"]})["se.coop"]
    assert (coop["current_price"], coop["effective_price"]) == ("17.50", "14.00")
    assert coop["best"]["quantity"] == 2 and coop["best"]["member"] is False and coop["is_cheapest"]
    assert result["saving_vs_here"] == {"amount": "-5.95", "percent": "-29.8"}


def test_a_percentage_off_counts_too_and_the_better_of_all_offers_wins(shops):
    sku = seeded(shops)
    put_offer("se.coop", shops.coop_store, sku["se.coop"], "10 % rabatt", promo="a")  # 15,75
    put_offer("se.coop", shops.coop_store, sku["se.coop"], "2 för 30 kr", promo="b")  # 15,00
    coop = rows({"rows": known(shops)["known"]})["se.coop"]
    assert coop["effective_price"] == "15.00" and coop["best"]["terms"] == "2 för 30 kr"


def test_an_offer_nobody_has_seen_lately_or_that_has_ended_is_not_used(shops):
    sku = seeded(shops)
    put_offer("se.coop", shops.coop_store, sku["se.coop"], "2 för 20 kr", promo="old")
    with SessionLocal.begin() as db:
        offer = db.scalar(select(Offer).where(Offer.identity_key == f"{sku['se.coop']}:old"))
        offer.last_seen = utcnow().replace(year=2020)
    assert rows({"rows": known(shops)["known"]})["se.coop"]["effective_price"] == "17.50"


def test_a_live_comparison_uses_the_stores_current_offers_and_your_membership(shops):
    mine(shops, shops.cg_store, shops.coop_store)
    axa = next(p for p in shops.cg_site.items if p["id"] == "100132321_ST")
    axa["productStoreDetails"]["prices"]["promotions"] = [{
        "id": "1,2", "type": "1", "effectType": "ItemsTotal", "membersOnly": True,
        "from": "2026-11-23T00:00:00+01:00", "to": "2099-11-29T23:59:00+01:00",
        "minQuantity": 1, "value": 15, "name": "club", "campaignTypeId": "0", "couponId": None}]

    plain = job_of(shops, compare(shops))["result"]
    assert rows(plain)["se.citygross"]["effective_price"] == "19.95" and plain["cheapest"]["store_name"] == "Coop Häggvik"

    shops.client.put("/v1/me/memberships/se.citygross")
    from app import retrieval
    retrieval.clear_cache()
    member = job_of(shops, compare(shops))["result"]
    cg = rows(member)["se.citygross"]
    assert (cg["current_price"], cg["effective_price"]) == ("19.95", "15.00") and cg["best"]["member"] is True
    assert member["cheapest"]["store_name"] == "City Gross Borås"


def test_search_results_carry_the_best_price_for_the_person(shops):
    mine(shops, shops.cg_store)
    axa = next(p for p in shops.cg_site.items if p["id"] == "100132321_ST")
    axa["productStoreDetails"]["prices"]["promotions"] = [{
        "id": "1,3", "type": "1", "effectType": "ItemsTotal", "membersOnly": True,
        "from": "2026-11-23T00:00:00+01:00", "to": "2099-11-29T23:59:00+01:00",
        "minQuantity": 1, "value": 15, "name": "club", "campaignTypeId": "0", "couponId": None}]

    def best():
        from app import retrieval
        retrieval.clear_cache()
        job = job_of(shops, shops.client.post("/v1/search/jobs", json={"gtin": "7310130321085", "store_ids": [shops.cg_store]}))
        (item,) = job["result"]["items"]
        return item["best"]

    assert best() is None
    shops.client.put("/v1/me/memberships/se.citygross")
    found = best()
    assert (found["unit_price"], found["quantity"], found["member"]) == ("15.00", 1, True)

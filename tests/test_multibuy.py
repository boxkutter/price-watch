# ruff: noqa: F811  (fixtures imported from another test module are used by name)
"""A multi-buy counts as its price per item: "2 för 35 kr" is 17,50 kr each."""
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.connectors.base import OfferData, Snapshot
from app.db import SessionLocal
from app.models import Event
from app.multibuy import parse_deal
from tests.test_regressions import make_monitor, price, scan, scripted  # noqa: F401  (scripted is a fixture)

D = Decimal


@pytest.mark.parametrize("terms,single,quantity,total", [
    ("2 för 35 kr", None, 2, "35"),
    ("2 för 35", None, 2, "35"),
    ("3 för 50 kr", None, 3, "50"),
    ("2 FÖR 12,50 KR", None, 2, "12.50"),
    ("2 för 12.50 kr", None, 2, "12.50"),
    ("3 st för 50:-", None, 3, "50"),
    ("2 for 35 kr", None, 2, "35"),
    ("Klubbpris: 2 för 99 kr", None, 2, "99"),
    ("2 för 1", "20", 2, "20"),                    # pay for one, get two
    ("3 för 2", "20", 3, "40"),
    ("Köp 2 betala för 1", "20", 2, "20"),
    ("köp 3 betala för 2", "30", 3, "60"),
    ("19 kr/st vid köp av 2", None, 2, "38"),
    ("Vid köp av 3: 15 kr/st", None, 3, "45"),
])
def test_the_wording_of_a_multi_buy_is_read(terms, single, quantity, total):
    deal = parse_deal(terms, D(single) if single else None)
    assert deal is not None and (deal.quantity, deal.total) == (quantity, D(total))


@pytest.mark.parametrize("terms,single", [
    ("17.95 kr", None),                            # one item: that is the price, not a multi-buy
    ("20 % rabatt", None),
    ("Medlemspris", None),
    ("", None), (None, None),
    ("1 för 15 kr", None),                         # one item is not a multi-buy
    ("2 för 1", None),                             # pay-for wording needs the price of one item
    ("100 för 1 kr", None),                        # not a plausible number of items
    ("PriceChangeAll: 19.95", None),               # a store's internal wording, not a deal
])
def test_wording_that_is_not_a_multi_buy_is_left_alone(terms, single):
    assert parse_deal(terms, D(single) if single else None) is None


def test_the_price_per_item_is_what_is_compared_with_an_alert_price():
    deal = parse_deal("2 för 35 kr")
    assert deal.unit == D("17.50")
    assert deal.reaches(D("19")) and deal.reaches(D("17.50")) and not deal.reaches(D("17.49"))


def test_the_comparison_is_exact_not_rounded():
    deal = parse_deal("3 för 50 kr")           # 16.666… each
    assert deal.unit == D("16.67")
    assert deal.reaches(D("16.67")) and not deal.reaches(D("16.66"))


# ---- a monitor's alert price sees multi-buys --------------------------------------------------------

def deal(terms, eligibility="public", promo="week", amount=None):
    return OfferData(f"oats:{promo}", "Demo oats offer", terms, amount, "SEK", "online", "fixture://offer",
                     eligibility=eligibility, skus=("oats",))


def watch(scripted, target="19", offers=(), single="20.00", scans=1):
    """A search monitor for oats at [single] kr with an alert price; [scans] baseline scans first."""
    monitor = make_monitor("generic_product", conditions={"query": "oats", "maximum_price": target})
    for _ in range(scans):
        scripted.snapshot = Snapshot([price(amount=single)], [], True, "x")
        assert scan(monitor).state == "succeeded"
    scripted.snapshot = Snapshot([price(amount=single)], list(offers), True, "x")
    assert scan(monitor).state == "succeeded"
    return monitor


def events(monitor_id):
    with SessionLocal() as db:
        return [(e.type, e.payload) for e in db.scalars(
            select(Event).where(Event.monitor_id == monitor_id).order_by(Event.created_at))]


def test_two_for_35_reaches_an_alert_price_of_19_when_one_costs_20(scripted):
    monitor = watch(scripted, offers=[deal("2 för 35 kr")])
    types = [t for t, _ in events(monitor)]
    assert "offer.detected" in types and types.count("price.threshold_reached") == 1
    payload = dict(events(monitor))["price.threshold_reached"]
    assert payload["price"] == "17.50" and payload["quantity"] == 2 and payload["multibuy"] is True
    assert payload["terms"] == "2 för 35 kr" and payload["threshold"] == "19"
    assert not any(t.startswith("price.") and t != "price.threshold_reached" for t in types)  # the single price did not move


def test_it_is_announced_once_and_again_only_when_the_offer_changes(scripted):
    monitor = watch(scripted, offers=[deal("2 för 35 kr")])
    def again(*offers):
        scripted.snapshot = Snapshot([price()], list(offers), True, "x")
        assert scan(monitor).state == "succeeded"

    again(deal("2 för 35 kr"))
    assert [t for t, _ in events(monitor)].count("price.threshold_reached") == 1
    again(deal("3 för 45 kr"))                                  # a different offer: 15 kr each
    thresholds = [p for t, p in events(monitor) if t == "price.threshold_reached"]
    assert [p["price"] for p in thresholds] == ["17.50", "15.00"]


@pytest.mark.parametrize("offers,reason", [
    ([deal("2 för 39 kr")], "19,50 each is above the alert price"),
    ([deal("Klubbpris: 2 för 30 kr", eligibility="member")], "a member price is not counted for a non-member"),
    ([deal("21.00 kr")], "not cheaper than one item"),
    ([deal("3 % rabatt")], "19,40 is above the alert price"),
    ([deal("Kampanj")], "not a stated price"),
])
def test_an_offer_that_does_not_reach_the_alert_price_only_announces_the_offer(scripted, offers, reason):
    monitor = watch(scripted, offers=offers)
    assert "price.threshold_reached" not in [t for t, _ in events(monitor)], reason


@pytest.mark.parametrize("terms,price,member", [
    ("20 % rabatt", "16.00", False),               # a percentage off one item
    ("17.50 kr", "17.50", False),                  # a public price for one item that was not folded into the price
])
def test_a_percentage_or_a_cheaper_single_price_reaches_the_alert_price_too(scripted, terms, price, member):
    monitor = watch(scripted, offers=[deal(terms)])
    payload = dict(events(monitor))["price.threshold_reached"]
    assert payload["price"] == price and payload["quantity"] == 1 and payload["multibuy"] is False


def test_member_offers_count_for_a_member_of_the_chain_and_only_for_them(scripted):
    from tests.test_regressions import admin_client
    member_offer = deal("Medlemspris: 15.00 kr", eligibility="member", promo="club")
    assert "price.threshold_reached" not in [t for t, _ in events(watch(scripted, offers=[member_offer]))]

    assert admin_client().put("/v1/me/memberships/test.fixture").status_code == 204
    monitor = watch(scripted, offers=[member_offer])
    payload = dict(events(monitor))["price.threshold_reached"]
    assert payload["price"] == "15.00" and payload["member"] is True and payload["multibuy"] is False


def test_pay_for_wording_uses_the_price_of_one_item(scripted):
    monitor = watch(scripted, target="12", offers=[deal("Köp 2 betala för 1")])     # 20 for two = 10 each
    payload = dict(events(monitor))["price.threshold_reached"]
    assert payload["price"] == "10.00" and payload["quantity"] == 2


def test_the_first_check_is_a_baseline_and_announces_nothing_unless_asked(scripted):
    monitor = make_monitor("generic_product", conditions={"query": "oats", "maximum_price": "19"})
    scripted.snapshot = Snapshot([price(amount="20.00")], [deal("2 för 35 kr")], True, "x")
    assert scan(monitor).state == "succeeded"
    assert events(monitor) == []


def test_a_monitor_without_an_alert_price_is_unchanged(scripted):
    monitor = make_monitor("generic_product", conditions={"query": "oats"})
    for offers in ([], [deal("2 för 35 kr")]):
        scripted.snapshot = Snapshot([price(amount="20.00")], offers, True, "x")
        scan(monitor)
    assert [t for t, _ in events(monitor)] == ["offer.detected"]


# ---- what clients are told --------------------------------------------------------------------------

def exact_monitor_of_oats(scripted, offers, single="20.00"):
    """A product monitor on the oats listing, after a scan that saw [offers]."""
    scripted.snapshot = Snapshot([price(amount=single)], list(offers), True, "x")
    seeding = make_monitor("generic_product", conditions={"query": "oats"})
    assert scan(seeding).state == "succeeded"
    with SessionLocal() as db:
        from app.models import Listing
        listing = db.scalar(select(Listing).where(Listing.sku == "oats"))
    return make_monitor("exact_product", conditions={"listing_id": listing.id})


def watchlist_item(monitor_id):
    from tests.test_regressions import admin_client
    return next(m for m in admin_client().get("/v1/monitors").json()["items"] if m["id"] == monitor_id)


def test_a_watched_product_reports_its_best_multi_buy_per_item(scripted):
    monitor = exact_monitor_of_oats(scripted, [deal("2 för 35 kr"), deal("3 för 54 kr", promo="b")])
    assert watchlist_item(monitor)["offer"] == {"terms": "2 för 35 kr", "quantity": 2, "unit_price": "17.50",
                                                "member": False, "valid_to": None}


@pytest.mark.parametrize("offers", [
    [deal("2 för 45 kr")],                                        # 22,50 each: dearer than one item
    [deal("Klubbpris: 2 för 30 kr", eligibility="member")],      # a member price, for someone who is not a member
    [deal("21.00 kr")],                                           # not cheaper than one item
    [],
])
def test_an_offer_that_is_not_cheaper_or_not_usable_is_not_reported(scripted, offers):
    assert watchlist_item(exact_monitor_of_oats(scripted, offers))["offer"] is None


def test_a_member_sees_their_member_price_as_the_best(scripted):
    from tests.test_regressions import admin_client
    offers = [deal("2 för 35 kr"), deal("Medlemspris: 15.00 kr", eligibility="member", promo="club")]
    monitor = exact_monitor_of_oats(scripted, offers)
    assert watchlist_item(monitor)["offer"]["unit_price"] == "17.50"          # not a member yet
    admin_client().put("/v1/me/memberships/test.fixture")
    best = watchlist_item(monitor)["offer"]
    assert (best["unit_price"], best["quantity"], best["member"]) == ("15.00", 1, True)


def test_search_results_say_what_an_offer_costs_per_item():
    from app.deals import deal_fields
    assert deal_fields("2 för 35 kr", D("20")) == {"quantity": 2, "unit_price": "17.50"}
    assert deal_fields("Köp 2 betala för 1", D("20")) == {"quantity": 2, "unit_price": "10.00"}
    assert deal_fields("Medlemspris: 17.95 kr", D("20")) == {"quantity": 1, "unit_price": "17.95"}
    assert deal_fields("20 % rabatt", D("20")) == {"quantity": 1, "unit_price": "16.00"}
    assert deal_fields("Kampanj", D("20")) == {"quantity": None, "unit_price": None}

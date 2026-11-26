"""Linking a scanned barcode to a product: validation, provenance, merging, and undo."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.barcodes import BarcodeError, clean_barcode, is_restricted, search_form
from app.db import SessionLocal
from app.main import app
from app.models import AuditLog, Listing, PriceObservation, Product
from tests.test_politeness import job_of, listing_id, monitor
from tests.test_product_links import add

AXA = "7310130321085"      # retailer-published at City Gross (1,5KG AXA havregryn)
SCANNED = "4006381333931"  # a barcode a person scans at home


def ean13(first12: str) -> str:
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(first12))
    return first12 + str((10 - total % 10) % 10)


# ---- reading a barcode -----------------------------------------------------------------------

@pytest.mark.parametrize("text,gtin14", [
    (SCANNED, "0" + SCANNED),
    (" 4006381 333931 ", "0" + SCANNED),          # spaces as printed
    ("4006381-333931", "0" + SCANNED),            # hyphens
    ("0036000291452", "00036000291452"),          # EAN-13 form of a UPC-A
    ("036000291452", "00036000291452"),            # UPC-A
    ("96385074", "00000096385074"),               # EAN-8
    ("10012345678902", "10012345678902"),         # GTIN-14 (a carton), not an item code
])
def test_every_common_barcode_form_is_accepted_and_normalised(text, gtin14):
    assert clean_barcode(text) == gtin14


@pytest.mark.parametrize("text,fragment", [
    ("4006381333932", "check digit"),             # wrong last digit
    ("40063813339", "8, 12, 13 or 14"),           # wrong length
    ("abcdefghijklm", "8, 12, 13 or 14"),
    ("", "8, 12, 13 or 14"),
    (ean13("209021910000"), "store-internal"),    # EAN-13 prefix 2: loose/weighed item code
    (ean13("021234567890"), "store-internal"),    # prefix 02: restricted circulation
    (ean13("200000012345"), "store-internal"),
])
def test_unusable_barcodes_are_refused_with_a_plain_reason(text, fragment):
    with pytest.raises(BarcodeError, match=fragment):
        clean_barcode(text)


def test_restricted_codes_are_identified_but_real_codes_are_not():
    assert is_restricted("0" + ean13("209021910000")) and is_restricted("0" + ean13("021234567890"))
    assert not is_restricted("0" + SCANNED) and not is_restricted("10012345678902")


def test_the_digits_sent_to_a_store_are_as_scanned_or_ean13():
    assert search_form("0" + SCANNED, SCANNED) == SCANNED
    assert search_form("0" + SCANNED, " 4006 381 333931 ") == "4006381333931"
    assert search_form("0" + SCANNED, "nonsense") == SCANNED
    assert search_form("00036000291452", "036000291452") == "036000291452"


# ---- linking to a monitor --------------------------------------------------------------------

def apple_monitor(cg):
    result = job_of(cg, add(cg))["result"]
    return result["monitor_id"], result["product"]["listing_id"]


def oats_monitor(cg):
    lid = listing_id(cg, "axa")
    mid = monitor(cg, listing_id=lid).json()["id"]
    return mid, lid


def link(cg, mid, gtin, **extra):
    return cg.client.post(f"/v1/monitors/{mid}/barcode", json={"gtin": gtin, **extra})


def test_a_scanned_barcode_makes_a_product_findable_and_records_where_it_came_from(cg):
    mid, _ = apple_monitor(cg)
    before = cg.client.get(f"/v1/monitors/{mid}").json()
    assert before["barcode"] is None and before["product_id"]  # the loose apple has no real barcode

    linked = link(cg, mid, SCANNED)
    assert linked.status_code == 200 and linked.json()["outcome"] == "set"
    assert linked.json()["product"]["barcode"]["source"] == "user"
    assert cg.client.get(f"/v1/products/barcode/{SCANNED}").json()["id"] == before["product_id"]
    detail = cg.client.get(f"/v1/products/{before['product_id']}").json()
    assert detail["gtin_source"] == "user" and detail["gtin_set_at"] and detail["verified"]
    assert cg.client.get(f"/v1/monitors/{mid}").json()["barcode"] == {"gtin14": "0" + SCANNED, "source": "user"}
    with SessionLocal() as db:
        entry = db.scalars(select(AuditLog).where(AuditLog.action == "product.barcode_set")).one()
        assert entry.details["gtin14"] == "0" + SCANNED and entry.details["monitor_id"] == mid

    assert link(cg, mid, SCANNED).json()["outcome"] == "unchanged"
    other = ean13("400638133393"[:-1] + "4")
    assert link(cg, mid, other).json()["outcome"] == "replaced"   # a person can correct a person
    assert cg.client.get(f"/v1/products/barcode/{SCANNED}").status_code == 404


def test_a_wrong_scan_can_be_undone_but_only_for_a_barcode_a_person_set(cg):
    mid, _ = apple_monitor(cg)
    link(cg, mid, SCANNED)
    assert cg.client.delete(f"/v1/monitors/{mid}/barcode").status_code == 204
    assert cg.client.get(f"/v1/products/barcode/{SCANNED}").status_code == 404
    assert cg.client.get(f"/v1/monitors/{mid}").json()["barcode"] is None
    assert cg.client.delete(f"/v1/monitors/{mid}/barcode").status_code == 204  # nothing left: a no-op


def test_a_barcode_the_store_publishes_cannot_be_changed_or_removed_by_a_person(cg):
    mid, _ = oats_monitor(cg)
    shown = cg.client.get(f"/v1/monitors/{mid}").json()
    assert shown["barcode"] == {"gtin14": "0" + AXA, "source": "retailer"}
    changed = link(cg, mid, SCANNED)
    assert changed.status_code == 409 and "published by the store" in changed.json()["message"]
    assert cg.client.delete(f"/v1/monitors/{mid}/barcode").status_code == 409
    assert cg.client.get(f"/v1/products/barcode/{AXA}").status_code == 200


@pytest.mark.parametrize("gtin,fragment", [
    (ean13("209021910000"), "store-internal"),
    ("4006381333932", "check digit"),
    ("123", "Invalid request"),                   # too short for any barcode
])
def test_bad_codes_are_refused_before_anything_changes(cg, gtin, fragment):
    mid, _ = apple_monitor(cg)
    response = link(cg, mid, gtin)
    assert response.status_code == 422 and fragment in response.json()["message"]
    assert cg.client.get(f"/v1/monitors/{mid}").json()["barcode"] is None


def test_a_barcode_that_belongs_to_another_product_asks_before_merging(cg):
    apple_mid, apple_lid = apple_monitor(cg)
    oats_mid, _ = oats_monitor(cg)
    oats_product = cg.client.get(f"/v1/monitors/{oats_mid}").json()["product_id"]
    apple_product = cg.client.get(f"/v1/monitors/{apple_mid}").json()["product_id"]

    refused = link(cg, apple_mid, AXA)                      # it is the oats' barcode
    body = refused.json()
    assert refused.status_code == 409 and "merge=true" in body["message"]
    existing = body["details"][0]["existing_product"]
    assert existing["id"] == oats_product and existing["retailers"] == ["se.citygross"]
    assert cg.client.get(f"/v1/monitors/{apple_mid}").json()["product_id"] == apple_product  # unchanged

    merged = link(cg, apple_mid, AXA, merge=True)
    assert merged.status_code == 200 and merged.json()["outcome"] == "merged"
    assert merged.json()["merged_listings"] == 1 and merged.json()["product"]["listings"] == 2
    assert cg.client.get(f"/v1/monitors/{apple_mid}").json()["product_id"] == oats_product
    with SessionLocal() as db:
        assert db.get(Product, apple_product) is None                          # the empty product is gone
        assert db.scalar(select(func.count()).select_from(Listing).where(
            Listing.product_id == oats_product)) == 2
        assert db.scalar(select(func.count()).select_from(PriceObservation).where(
            PriceObservation.listing_id == apple_lid)) >= 1                    # price history is intact
        assert db.scalars(select(AuditLog).where(AuditLog.action == "product.barcode_merged")).one()


def test_merging_is_refused_when_the_product_already_has_a_different_barcode(cg):
    apple_mid, _ = apple_monitor(cg)
    oats_monitor(cg)
    link(cg, apple_mid, SCANNED)
    refused = link(cg, apple_mid, AXA, merge=True)
    assert refused.status_code == 409 and "different barcode" in refused.json()["message"]


def test_only_a_specific_product_monitor_can_take_a_barcode_and_only_its_owner(cg):
    generic = monitor(cg, "generic_product", query="havregryn").json()["id"]
    refused = link(cg, generic, SCANNED)
    assert refused.status_code == 422 and "one specific product" in refused.json()["message"]
    mid, _ = apple_monitor(cg)
    token = cg.client.post("/v1/admin/invitations", json={"email": "other@example.invalid"}).json()
    other = TestClient(app)
    other.post("/v1/auth/accept-invitation", json={"token": token["invitation_token"],
                                                   "password": "another-long-password"})
    other.headers["Authorization"] = "Bearer " + other.post("/v1/auth/login", json={
        "email": "other@example.invalid", "password": "another-long-password"}).json()["access_token"]
    assert other.post(f"/v1/monitors/{mid}/barcode", json={"gtin": SCANNED}).status_code == 404
    assert other.delete(f"/v1/monitors/{mid}/barcode").status_code == 404


def test_a_read_only_client_cannot_link_barcodes(cg):
    mid, _ = apple_monitor(cg)
    client = cg.client.post("/v1/clients", json={"name": "reader", "scopes": ["monitors:read"]}).json()
    cg.client.post("/v1/me/client-grants", json={"client_id": client["client_id"], "scopes": ["monitors:read"]})
    response = cg.client.post(f"/v1/monitors/{mid}/barcode", json={"gtin": SCANNED},
                              headers={"X-Client-Token": client["client_token"]})
    assert response.status_code == 403


def test_the_administrator_override_follows_the_same_rules_and_records_its_source(cg):
    mid, _ = apple_monitor(cg)
    product = cg.client.get(f"/v1/monitors/{mid}").json()["product_id"]
    path = f"/v1/admin/products/{product}/verified-gtin"
    assert cg.client.post(path, json={"gtin": ean13("209021910000")}).status_code == 422
    assert cg.client.post(path, json={"gtin": AXA}).status_code in (200, 409)  # 409 once oats exist
    assert cg.client.post(path, json={"gtin": SCANNED}).status_code == 200
    assert cg.client.get(f"/v1/products/{product}").json()["gtin_source"] == "admin"

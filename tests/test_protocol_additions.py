"""Manifest additions made for Willys and Lidl: declared stores, scheduled window, redirects."""
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app import declared_stores
from app.connectors import registry
from app.connectors.manifest import Manifest
from app.db import SessionLocal
from app.models import Store

BASE = {"id": "xx.shop", "name": "Shop", "country": "SE", "protocol_version": 1, "connector_version": "1"}


@pytest.mark.parametrize("window", ["25:00-26:00", "4:00-8:45", "04:00", "04:00-08:60", "04:00 - 08:45", ""])
def test_a_malformed_scheduled_window_is_rejected(window):
    with pytest.raises(ValidationError, match="scheduled_window_utc"):
        Manifest(**BASE, scheduled_window_utc=window)


@pytest.mark.parametrize("window", ["04:00-08:45", "22:00-02:00", "00:00-23:59"])
def test_a_wellformed_scheduled_window_is_accepted(window):
    assert Manifest(**BASE, scheduled_window_utc=window).scheduled_window_utc == window


def test_declared_stores_must_be_unique_and_plainly_named():
    stores = [{"external_id": "online", "name": "A"}, {"external_id": "online", "name": "B"}]
    with pytest.raises(ValidationError, match="unique"):
        Manifest(**BASE, stores=stores)
    for bad in ({"external_id": "has space", "name": "x"}, {"external_id": "", "name": "x"},
                {"external_id": "ok", "name": ""}, {"external_id": "ok", "name": "x", "extra": 1}):
        with pytest.raises(ValidationError):
            Manifest(**BASE, stores=[bad])


def test_new_manifest_fields_default_to_off():
    manifest = Manifest(**BASE)
    assert (manifest.stores, manifest.scheduled_window_utc, manifest.follow_redirects) == ([], None, False)


def test_every_bundled_connector_that_declares_stores_has_them_after_start_up(fresh_db):
    declared = {(r["id"], s["external_id"]): s["name"] for r in registry.retailers() for s in r["stores"]}
    assert {("se.willys", "online"), ("se.lidl", "online"), ("test.fixture", "demo"),
            ("test.demoshop", "north"), ("test.demoshop", "south")} <= set(declared)
    with SessionLocal() as db:
        have = {(s.retailer_id, s.external_id): s.name for s in db.scalars(select(Store))}
    assert {key: have.get(key) for key in declared} == declared


def test_seeding_creates_what_is_missing_renames_and_never_deletes_or_duplicates(fresh_db):
    with SessionLocal.begin() as db:
        store = db.scalar(select(Store).where(Store.retailer_id == "se.willys"))
        store.name = "Old name"
        stray = Store(retailer_id="se.willys", external_id="2149", name="Discovered elsewhere")
        db.add(stray)
        db.delete(db.scalar(select(Store).where(Store.retailer_id == "se.lidl")))
    with SessionLocal.begin() as db:
        assert declared_stores.seed(db) == 1                      # only Lidl was missing
    with SessionLocal.begin() as db:
        assert declared_stores.seed(db) == 0                      # idempotent
        names = {(s.retailer_id, s.external_id): s.name for s in db.scalars(select(Store))}
        assert names[("se.willys", "online")] == "Willys (online)"           # renamed back
        assert names[("se.willys", "2149")] == "Discovered elsewhere"        # never removed
        assert db.scalar(select(func.count()).select_from(Store).where(Store.retailer_id == "se.lidl")) == 1

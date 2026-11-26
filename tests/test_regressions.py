"""Regression tests for defects found in code review."""
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.connectors.base import OfferData, PriceData, Snapshot
from app.connectors.registry import CONNECTORS
from app.db import SessionLocal, engine
from app.main import app
from app.models import (
    Delivery,
    Event,
    Job,
    Monitor,
    Store,
    User,
    Webhook,
    new_id,
    utcnow,
)
from app.security import encrypt_secret, hash_token
from app.webhooks import due_delivery
from app.worker import claim_job, run_job


def price(sku="oats", amount="24.90", currency="SEK"):
    return PriceData(sku, "Demo oats 1 kg", Decimal(amount), Decimal("29.90"), currency,
                     "online", "fixture://oats", brand="Demo",
                     package_value=Decimal(1), package_unit="kg")


def offer(terms="1 kg package", amount="24.90"):
    return OfferData("oats-week", "Demo oats offer", terms, Decimal(amount), "SEK",
                     "online", "fixture://offer")


class Scripted:
    """Connector that returns whatever snapshot the test sets next."""

    def __init__(self):
        self.snapshot = Snapshot([], [], True, "fixture://scripted")

    def scan(self, external_store_id, channel):
        return self.snapshot


@pytest.fixture
def scripted(fresh_db, monkeypatch):
    connector = Scripted()
    monkeypatch.setitem(CONNECTORS, "test.fixture", connector)
    return connector


def make_monitor(kind="store", **fields):
    with SessionLocal.begin() as db:
        owner = db.scalar(select(User))
        store = db.scalar(select(Store))
        monitor = Monitor(owner_id=owner.id, name="m", type=kind, store_id=store.id, **fields)
        db.add(monitor)
        db.flush()
        return monitor.id


def scan(monitor_id) -> Job:
    with SessionLocal.begin() as db:
        job = Job(monitor_id=monitor_id, kind="scan")
        db.add(job)
        db.flush()
        job_id = job.id
    assert run_job()
    with SessionLocal() as db:
        return db.get(Job, job_id)


def admin_client():
    client = TestClient(app)
    token = client.post("/v1/auth/login", json={
        "email": "admin@example.invalid", "password": "0123456789abcdef"}).json()["access_token"]
    client.headers["Authorization"] = "Bearer " + token
    return client


def test_unknown_route_uses_error_envelope(fresh_db):
    response = TestClient(app).get("/v1/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "not_found"
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_admin_page_does_not_depend_on_working_directory(fresh_db, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert TestClient(app).get("/admin").status_code == 200


def test_login_rejects_oversized_input(fresh_db):
    response = TestClient(app).post("/v1/auth/login", json={
        "email": "a@example.invalid", "password": "x" * 100_000})
    assert response.status_code == 422


def test_retrying_a_partial_job_does_not_collide_with_its_first_run(scripted):
    monitor_id = make_monitor()
    scripted.snapshot = Snapshot([price("good"), price("bad", currency="EUR")], [], True, "x")
    job = scan(monitor_id)
    assert job.state == "partial"
    client = admin_client()
    assert client.post(f"/v1/admin/jobs/{job.id}/retry").status_code == 202
    assert run_job()
    with SessionLocal() as db:
        retried = db.get(Job, job.id)
        assert retried.state == "partial", retried.state  # not "queued" after an IntegrityError
        assert retried.attempts == 1


def test_duplicate_sku_in_one_snapshot_is_partial_not_a_crash(scripted):
    monitor_id = make_monitor()
    scripted.snapshot = Snapshot([price("dup"), price("dup", "19.90")], [offer(), offer()],
                                 True, "x")
    job = scan(monitor_id)
    assert job.state == "partial"
    assert job.result["invalid_items"] == 2


def test_offer_that_flips_back_and_forth_notifies_every_change(scripted):
    monitor_id = make_monitor()
    for terms in ["A", "B", "A", "B"]:  # first scan is the baseline
        scripted.snapshot = Snapshot([], [offer(terms=terms)], True, "x")
        assert scan(monitor_id).state == "succeeded"
    with SessionLocal() as db:
        types = [e.type for e in db.scalars(select(Event).order_by(Event.created_at))]
    assert types == ["offer.changed"] * 3


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="needs row locks")
def test_delivery_claim_locks_only_the_row_it_sends(fresh_db):
    with SessionLocal.begin() as db:
        owner = db.scalar(select(User))
        store = db.scalar(select(Store))
        monitor = Monitor(owner_id=owner.id, name="m", type="store", store_id=store.id)
        hook = Webhook(owner_id=owner.id, url="https://example.com/h",
                       secret_hash=hash_token("s"), secret_ciphertext=encrypt_secret("s"))
        db.add_all([monitor, hook])
        db.flush()
        for _ in range(3):
            event = Event(owner_id=owner.id, monitor_id=monitor.id, subject_key=new_id(),
                          type="offer.detected", transition_key="t", payload={})
            db.add(event)
            db.flush()
            db.add(Delivery(event_id=event.id, webhook_id=hook.id,
                            next_attempt_at=utcnow() - timedelta(minutes=1)))
    first, second = SessionLocal(), SessionLocal()
    try:
        claimed = due_delivery(first)
        other = due_delivery(second)
        assert claimed is not None
        assert other is not None and other.id != claimed.id
    finally:
        first.rollback(); second.rollback()
        first.close(); second.close()


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="needs row locks")
def test_job_claim_locks_only_the_row_it_runs(scripted):
    monitor_id = make_monitor()
    with SessionLocal.begin() as db:
        for _ in range(3):
            db.add(Job(monitor_id=monitor_id, kind="scan"))
    first, second = SessionLocal(), SessionLocal()
    try:
        assert claim_job(first).id != claim_job(second).id
    finally:
        first.rollback(); second.rollback()
        first.close(); second.close()

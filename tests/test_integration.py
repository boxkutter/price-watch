import asyncio
from datetime import timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select

from app.bootstrap import main as bootstrap
from app.connectors.base import OfferData, PriceData, Snapshot
from app.connectors.registry import CONNECTORS
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import (
    Delivery,
    Event,
    Job,
    Listing,
    Monitor,
    Offer,
    PriceObservation,
    Webhook,
    utcnow,
)
from app.pricing import base_quantity, unit_price
from app.security import encrypt_secret, hash_token
from app.webhooks import signed_headers, validate_destination
from app.worker import run_delivery, run_job


def test_invitation_delegation_scan_and_partial_snapshot(monkeypatch):
    # This exercises the shared database path used by the API and worker.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    bootstrap()

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health/ready")).status_code == 503
            admin_login = await client.post("/v1/auth/login", json={
                "email": "admin@example.invalid", "password": "0123456789abcdef"})
            assert admin_login.status_code == 200
            admin_h = {"Authorization": "Bearer " + admin_login.json()["access_token"]}
            invited = await client.post("/v1/admin/invitations", headers=admin_h,
                                        json={"email": "user@example.invalid"})
            assert invited.status_code == 201
            accepted = await client.post("/v1/auth/accept-invitation", json={
                "token": invited.json()["invitation_token"],
                "password": "long-user-password-123"})
            assert accepted.status_code == 201
            user_login = await client.post("/v1/auth/login", json={
                "email": "user@example.invalid", "password": "long-user-password-123"})
            user_h = {"Authorization": "Bearer " + user_login.json()["access_token"]}
            client_result = await client.post("/v1/clients", headers=admin_h, json={
                "name": "Crisis Planner", "scopes": ["monitors:read", "events:read"]})
            assert client_result.status_code == 201
            client_token = client_result.json()["client_token"]
            delegated_h = {**user_h, "X-Client-Token": client_token}
            assert (await client.get("/v1/events", headers=delegated_h)).status_code == 403
            granted = await client.post("/v1/me/client-grants", headers=user_h, json={
                "client_id": client_result.json()["client_id"],
                "scopes": ["monitors:read", "events:read"]})
            assert granted.status_code == 201
            assert (await client.get("/v1/events", headers=delegated_h)).status_code == 200
            store_id = next(item["id"] for item in (await client.get("/v1/stores", headers=user_h)).json()["items"]
                            if item["retailer_id"] == "test.fixture")
            connector_status = (await client.get("/v1/connectors/test.fixture/health",
                                                 headers=user_h)).json()
            assert connector_status["enabled"] is True
            assert connector_status["status"] == "no_checks"
            assert (await client.post("/v1/monitors", headers=delegated_h, json={
                "name": "Denied", "type": "store", "store_id": store_id})).status_code == 403
            monitor_body = {"name": "All offers", "type": "store", "store_id": store_id,
                            "notify_existing_offers": True}
            keyed_h = {**user_h, "Idempotency-Key": "first-store-monitor"}
            monitor_result = await client.post("/v1/monitors", headers=keyed_h,
                                               json=monitor_body)
            assert monitor_result.status_code == 201
            monitor_id = monitor_result.json()["id"]
            repeated = await client.post("/v1/monitors", headers=keyed_h, json=monitor_body)
            assert repeated.json()["id"] == monitor_id
            conflict = await client.post("/v1/monitors", headers=keyed_h,
                                         json={**monitor_body, "name": "Different"})
            assert conflict.status_code == 409
            hidden = await client.get(f"/v1/monitors/{monitor_id}", headers=admin_h)
            assert hidden.status_code == 404
            assert hidden.json()["code"] == "not_found"
            assert hidden.json()["request_id"] == hidden.headers["X-Request-ID"]
            with SessionLocal.begin() as db:
                db.add(Webhook(owner_id=accepted.json()["user_id"], url="https://example.com/hook",
                               secret_hash=hash_token("test"),
                               secret_ciphertext=encrypt_secret("test")))
            check_h = {**user_h, "Idempotency-Key": "first-check"}
            job_result = await client.post(f"/v1/monitors/{monitor_id}/check", headers=check_h)
            assert job_result.status_code == 202
            same_job = await client.post(f"/v1/monitors/{monitor_id}/check", headers=check_h)
            assert same_job.json()["job_id"] == job_result.json()["job_id"]
            assert run_job()
            job = (await client.get("/v1/jobs/" + job_result.json()["job_id"], headers=user_h)).json()
            assert job["state"] == "succeeded"
            assert job["result"]["offers"] == 1
            events = (await client.get("/v1/events", headers=delegated_h)).json()["items"]
            assert [x["type"] for x in events] == ["offer.detected"]
            assert (await client.get("/v1/events", headers=admin_h)).json()["items"] == []

            with SessionLocal() as db:
                assert len(db.scalars(select(PriceObservation)).all()) == 1
                assert len(db.scalars(select(Offer)).all()) == 1
            product_id = (await client.get("/v1/products/search?q=Demo oats", headers=user_h)).json()["items"][0]["id"]
            mapping = await client.post(f"/v1/admin/products/{product_id}/verified-gtin",
                                        headers=admin_h, json={"gtin": "4006381333931"})
            assert mapping.status_code == 200
            assert (await client.get("/v1/products/barcode/4006381333931", headers=user_h)).json()["id"] == product_id
            product = (await client.get(f"/v1/products/{product_id}", headers=user_h)).json()
            assert product["listings"][0]["store_id"] == store_id
            with SessionLocal.begin() as db:
                db.add(PriceObservation(listing_id=product["listings"][0]["id"],
                                        run_id="private-member-test", eligibility="member",
                                        current_price=Decimal("1.00"), currency="SEK"))
            latest = (await client.get(f"/v1/products/{product_id}/prices", headers=user_h)).json()
            assert latest["items"][0]["current_price"] == "24.90"
            history = (await client.get(f"/v1/products/{product_id}/history", headers=user_h)).json()
            assert len(history["items"]) == 1
            listing_history = (await client.get(f"/v1/listings/{product['listings'][0]['id']}/prices",
                                                headers=user_h)).json()
            assert len(listing_history["items"]) == 1
            offers = (await client.get(f"/v1/stores/{store_id}/offers", headers=user_h)).json()
            assert offers["items"][0]["title"] == "Demo oats offer"

            search_body = {"query": "oats", "store_ids": [store_id], "channel": "online"}
            search_headers = {**user_h, "Idempotency-Key": "search-oats"}
            search_job = await client.post("/v1/search/jobs", headers=search_headers,
                                           json=search_body)
            assert search_job.status_code == 202
            repeated_search = await client.post("/v1/search/jobs", headers=search_headers,
                                                json=search_body)
            assert repeated_search.json()["job_id"] == search_job.json()["job_id"]
            assert run_job()
            search_result = (await client.get(search_job.json()["status_url"], headers=user_h)).json()
            assert search_result["result"]["items"][0]["product_id"] == product_id
            assert search_result["result"]["coverage"][0]["status"] == "complete"
            assert (await client.get(search_job.json()["status_url"], headers=admin_h)).status_code == 404
            barcode_job = await client.post("/v1/search/jobs", headers=user_h, json={
                "gtin": "4006381333931", "store_ids": [store_id]})
            assert barcode_job.status_code == 202
            assert run_job()
            barcode_result = (await client.get(barcode_job.json()["status_url"], headers=user_h)).json()
            assert barcode_result["result"]["barcode_mapping"] == "verified"
            assert barcode_result["result"]["items"][0]["product_id"] == product_id

            second = await client.post("/v1/monitors", headers=user_h, json={
                "name": "Same offers", "type": "store", "store_id": store_id,
                "notify_existing_offers": True})
            await client.post(f"/v1/monitors/{second.json()['id']}/check", headers=user_h)
            assert run_job()
            with SessionLocal() as db:
                assert len(db.scalars(select(Event)).all()) == 2
                assert len(db.scalars(select(Delivery)).all()) == 1
            page1 = (await client.get("/v1/events?limit=1", headers=user_h)).json()
            assert len(page1["items"]) == 1 and page1["next_cursor"]
            page2 = (await client.get("/v1/events?limit=1&after=" + page1["next_cursor"],
                                      headers=user_h)).json()
            assert len(page2["items"]) == 1
            assert page1["items"][0]["id"] != page2["items"][0]["id"]

            with SessionLocal() as db:
                listing_id = db.scalar(select(Listing.id))
            exact = await client.post("/v1/monitors", headers=user_h, json={
                "name": "Oats price", "type": "exact_product", "store_id": store_id,
                "conditions": {"listing_id": listing_id, "maximum_price": "20.00"}})
            assert exact.status_code == 201
            await client.post(f"/v1/monitors/{exact.json()['id']}/check", headers=user_h)
            assert run_job()  # first observation establishes a baseline

            original = CONNECTORS["test.fixture"]
            class CheaperConnector:
                def scan(self, external_store_id, channel, request=None):
                    base = original.scan(external_store_id, channel)
                    return Snapshot([PriceData("oats-1kg", "Demo oats 1 kg", Decimal("19.90"),
                                               Decimal("29.90"), "SEK", "online", "fixture://oats",
                                               availability="in_stock")],
                                    base.offers, True, "fixture://cheaper")

            CONNECTORS["test.fixture"] = CheaperConnector()
            try:
                await client.post(f"/v1/monitors/{exact.json()['id']}/check", headers=user_h)
                assert run_job()
            finally:
                CONNECTORS["test.fixture"] = original
            event_types = [x["type"] for x in (await client.get("/v1/events", headers=user_h)).json()["items"]]
            assert "price.decreased" in event_types
            assert "price.threshold_reached" in event_types

            class OutOfStockConnector:
                def scan(self, external_store_id, channel, request=None):
                    base = original.scan(external_store_id, channel)
                    return Snapshot([PriceData("oats-1kg", "Demo oats 1 kg", Decimal("19.90"),
                                               Decimal("29.90"), "SEK", "online", "fixture://oats",
                                               availability="out_of_stock")],
                                    base.offers, True, "fixture://out-of-stock")

            CONNECTORS["test.fixture"] = OutOfStockConnector()
            try:
                await client.post(f"/v1/monitors/{exact.json()['id']}/check", headers=user_h)
                assert run_job()
            finally:
                CONNECTORS["test.fixture"] = original
            event_types = [x["type"] for x in (await client.get("/v1/events", headers=user_h)).json()["items"]]
            assert "product.unavailable" in event_types

            class RevisedOfferConnector:
                def scan(self, external_store_id, channel, request=None):
                    base = original.scan(external_store_id, channel)
                    revised = OfferData("oats-week", "Demo oats offer", "2 kg minimum",
                                        Decimal("21.90"), "SEK", "online", "fixture://offer")
                    return Snapshot(base.prices, [revised], True, "fixture://revised")

            CONNECTORS["test.fixture"] = RevisedOfferConnector()
            try:
                await client.post(f"/v1/monitors/{monitor_id}/check", headers=user_h)
                assert run_job()
            finally:
                CONNECTORS["test.fixture"] = original
            offer_history = (await client.get(f"/v1/stores/{store_id}/offers/history",
                                              headers=user_h)).json()["items"]
            assert {x["terms"] for x in offer_history} == {"1 kg package", "2 kg minimum"}
            event_types = [x["type"] for x in (await client.get("/v1/events", headers=user_h)).json()["items"]]
            assert "offer.changed" in event_types

            class PartialConnector:
                def scan(self, external_store_id, channel, request=None):
                    return Snapshot([], [], False, "fixture://partial")

            old = CONNECTORS["test.fixture"]
            CONNECTORS["test.fixture"] = PartialConnector()
            try:
                partial_job = await client.post(f"/v1/monitors/{monitor_id}/check", headers=user_h)
                assert run_job()
            finally:
                CONNECTORS["test.fixture"] = old
            with SessionLocal() as db:
                assert len(db.scalars(select(Event)).all()) == 6
                assert db.scalar(select(Offer)).status == "active"
                assert db.scalar(select(Monitor).where(Monitor.id == monitor_id)).baseline_complete

            with SessionLocal.begin() as db:
                recovered_job = Job(owner_id=accepted.json()["user_id"], kind="search",
                                    payload={"query": "no matching product", "store_ids": [store_id],
                                             "channel": "online"}, state="running",
                                    lease_until=utcnow() - timedelta(minutes=1))
                db.add(recovered_job)
                db.flush()
                recovered_id = recovered_job.id
            assert run_job()
            with SessionLocal() as db:
                recovered = db.get(Job, recovered_id)
                assert recovered.state == "succeeded"
                assert recovered.result["items"] == []
            retried_job = await client.post(
                f"/v1/admin/jobs/{partial_job.json()['job_id']}/retry", headers=admin_h)
            assert retried_job.status_code == 202
            assert retried_job.json()["state"] == "queued"

            sent = []
            class FakeResponse:
                status_code = 202

            class FakeClient:
                def __init__(self, **kwargs):
                    assert kwargs["follow_redirects"] is False
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return None
                def post(self, url, content, headers):
                    sent.append((url, content, headers))
                    return FakeResponse()

            monkeypatch.setattr("app.webhooks.validate_destination", lambda url: None)
            monkeypatch.setattr("app.webhooks.httpx.Client", FakeClient)
            assert run_delivery()
            assert sent[0][0] == "https://example.com/hook"
            assert sent[0][2]["X-Retail-Signature"].startswith("v1=")
            with SessionLocal() as db:
                assert db.scalar(select(Delivery).where(Delivery.id == sent[0][2]["X-Retail-Delivery"])).state == "delivered"
            delivery_id = sent[0][2]["X-Retail-Delivery"]
            with SessionLocal.begin() as db:
                failed = db.get(Delivery, delivery_id)
                failed.state = "failed"
                failed.attempts = 8
                hook_id = failed.webhook_id
            retried = await client.post(f"/v1/webhooks/{hook_id}/deliveries/{delivery_id}/retry",
                                        headers=user_h)
            assert retried.status_code == 202
            with SessionLocal() as db:
                assert db.get(Delivery, delivery_id).attempts == 0

            revoked = await client.delete("/v1/me/client-grants/" + client_result.json()["client_id"],
                                          headers=user_h)
            assert revoked.status_code == 204
            assert (await client.get("/v1/events", headers=delegated_h)).status_code == 403
            assert (await client.delete("/v1/clients/" + client_result.json()["client_id"],
                                        headers=admin_h)).status_code == 204
            audit_rows = (await client.get("/v1/admin/audit", headers=admin_h)).json()["items"]
            assert {row["action"] for row in audit_rows} >= {
                "invitation.created", "client.created", "client_grant.revoked",
                "monitor.created", "product.gtin_verified"}
            assert (await client.get("/v1/admin/audit", headers=user_h)).status_code == 403
            assert (await client.post("/v1/auth/logout", headers=user_h)).status_code == 204
            assert (await client.get("/v1/me", headers=user_h)).status_code == 401
            for _ in range(5):
                wrong = await client.post("/v1/auth/login", json={
                    "email": "nobody@example.invalid", "password": "incorrect"})
                assert wrong.status_code == 401
            limited = await client.post("/v1/auth/login", json={
                "email": "nobody@example.invalid", "password": "incorrect"})
            assert limited.status_code == 429
            assert limited.headers["Retry-After"] == "900"

    asyncio.run(scenario())


def test_webhook_signature_is_stable():
    body = b'{"id":"one"}'
    headers = signed_headers("secret", body, "delivery", 123)
    assert headers["X-Retail-Timestamp"] == "123"
    assert headers["X-Retail-Delivery"] == "delivery"
    assert headers["X-Retail-Signature"].startswith("v1=")
    assert headers == signed_headers("secret", body, "delivery", 123)
    assert headers != signed_headers("secret", b"different", "delivery", 123)


def test_webhook_rejects_loopback_and_plain_http():
    import pytest

    with pytest.raises(ValueError, match="Private"):
        validate_destination("https://127.0.0.1/hook")
    with pytest.raises(ValueError, match="HTTPS"):
        validate_destination("http://example.com/hook")


def test_unit_price_normalizes_compatible_units_and_preserves_unknowns():
    assert base_quantity(Decimal(750), "g") == (Decimal("0.750"), "kg")
    assert unit_price(Decimal("15.00"), Decimal(750), "g") == (Decimal("20.00"), "kg")
    assert unit_price(Decimal("15.00"), Decimal(1), "unknown") is None
    assert unit_price(Decimal("15.00"), None, "kg") is None

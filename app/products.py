"""Adding a product to monitoring from its page link, and removing monitors.

The link is parsed for the store's own product id (see app/connectors/urls.py); the page is never
fetched. The worker then looks that one product up through the connector, with the same rate
limits, pause and cache as any scan, and creates the product, its first price observation and the
monitor together. If the store does not list the product, nothing is created.
"""
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.audit import record as audit
from app.config import settings
from app.connectors.base import ScanRequest
from app.connectors.registry import connector_for
from app.ingest import add_price, ensure_listing
from app.models import (
    Delivery,
    EvaluationState,
    Event,
    IdempotencyRecord,
    Job,
    Listing,
    Monitor,
    Store,
    new_id,
    utcnow,
)
from app.pricing import unit_price
from app.retrieval import fetch


def parse_threshold(value) -> str | None:
    """A validated maximum price as a string, or None. Raises ValueError when invalid."""
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("maximum_price must be a number") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("maximum_price must be zero or more")
    return str(amount)


def existing_monitor(db: Session, owner_id: str, store_id: str, listing_id: str) -> Monitor | None:
    """The owner's active exact monitor for this listing, if any."""
    candidates = db.scalars(select(Monitor).where(
        Monitor.owner_id == owner_id, Monitor.store_id == store_id,
        Monitor.type == "exact_product", Monitor.enabled.is_(True)))
    return next((m for m in candidates if m.conditions.get("listing_id") == listing_id), None)


def process_add_product(db: Session, job: Job) -> dict:
    payload = job.payload or {}
    store = db.get(Store, payload.get("store_id"))
    connector = connector_for(store.retailer_id) if store else None
    if not store or not connector:
        return {"status": "unsupported", "detail": "The store or its connector is no longer available"}
    sku = payload["sku"]
    snapshot, skipped = fetch(db, store, connector, "online", ScanRequest(skus=(sku,)))
    if skipped:
        return skipped
    status = snapshot.effective_status
    if status != "success":
        detail = snapshot.detail or ("This store does not list that product"
                                     if status == "not_found" else None)
        return {"status": status, "complete": False, "detail": detail,
                "missing": list(snapshot.missing), "requests": snapshot.requests}
    item = next((p for p in snapshot.prices if p.sku == sku), None)
    if (item is None or item.currency != "SEK" or not item.current_price.is_finite()
            or item.current_price < 0 or item.channel != "online"):
        return {"status": "invalid_data", "complete": False,
                "detail": "The store returned no usable price for that product"}
    listing = ensure_listing(db, store, item)
    add_price(db, listing, item, new_id(), utcnow())
    unit = unit_price(item.current_price, item.package_value, item.package_unit)
    product = {"name": listing.name, "sku": sku, "listing_id": listing.id,
               "product_id": listing.product_id,
               "current_price": str(item.current_price), "currency": item.currency,
               "unit_price": {"amount": str(unit[0]), "unit": unit[1]} if unit else None,
               "availability": item.availability, "source_url": item.source_url}
    existing = existing_monitor(db, job.owner_id, store.id, listing.id)
    if existing:
        return {"status": "success", "complete": True, "already_monitored": True,
                "monitor_id": existing.id, "product": product, "requests": snapshot.requests}
    active = db.scalar(select(func.count()).select_from(Monitor).where(
        Monitor.owner_id == job.owner_id, Monitor.enabled.is_(True)))
    if active >= settings.max_monitors_per_user:
        return {"status": "rejected", "complete": False, "product": product,
                "detail": "Monitor limit reached; remove one before adding another"}
    conditions = {"listing_id": listing.id}
    if payload.get("maximum_price") is not None:
        conditions["maximum_price"] = payload["maximum_price"]
    monitor = Monitor(owner_id=job.owner_id, name=payload.get("name") or listing.name[:200],
                      type="exact_product", store_id=store.id, channel="online",
                      conditions=conditions, interval_minutes=payload["interval_minutes"],
                      notify_existing_offers=bool(payload.get("notify_existing_offers")))
    db.add(monitor)
    db.flush()
    audit(db, job.owner_id, "monitor.created", "monitor", monitor.id,
          {"via": "product_link", "sku": sku, "retailer": store.retailer_id})
    return {"status": "success", "complete": True, "already_monitored": False,
            "monitor_id": monitor.id, "product": product, "requests": snapshot.requests}


def delete_monitor_permanently(db: Session, monitor: Monitor) -> None:
    """Remove a monitor and everything that belongs only to it. The product's price history is
    part of the shared catalogue and stays."""
    event_ids = select(Event.id).where(Event.monitor_id == monitor.id)
    job_ids = select(Job.id).where(Job.monitor_id == monitor.id)
    db.execute(delete(Delivery).where(Delivery.event_id.in_(event_ids)))
    db.execute(delete(Event).where(Event.monitor_id == monitor.id))
    db.execute(delete(EvaluationState).where(EvaluationState.monitor_id == monitor.id))
    db.execute(delete(IdempotencyRecord).where(
        (IdempotencyRecord.resource_id == monitor.id) | IdempotencyRecord.resource_id.in_(job_ids)))
    db.execute(delete(Job).where(Job.monitor_id == monitor.id))
    db.delete(monitor)


__all__ = ["Listing", "delete_monitor_permanently", "existing_monitor", "parse_threshold",
           "process_add_product"]

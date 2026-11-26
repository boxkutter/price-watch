import hashlib
import json
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.registry import connector_for
from app.models import (
    Delivery,
    EvaluationState,
    Event,
    Job,
    Listing,
    Monitor,
    Offer,
    OfferObservation,
    PriceObservation,
    Product,
    Store,
    Webhook,
    new_id,
    utcnow,
)
from app.pricing import base_quantity, unit_price


def enqueue(db: Session, monitor: Monitor) -> Job:
    job = Job(monitor_id=monitor.id, kind="scan")
    db.add(job)
    db.commit()
    return job


def emit(db: Session, monitor: Monitor, subject: str, event_type: str,
         transition: str, payload: dict) -> None:
    exists = db.scalar(select(Event.id).where(Event.monitor_id == monitor.id,
        Event.subject_key == subject, Event.type == event_type, Event.transition_key == transition))
    if exists:
        return
    already_delivered_to_owner = db.scalar(select(Event.id).where(
        Event.owner_id == monitor.owner_id, Event.subject_key == subject,
        Event.type == event_type, Event.transition_key == transition)) is not None
    event = Event(owner_id=monitor.owner_id, monitor_id=monitor.id, subject_key=subject,
                  type=event_type, transition_key=transition, payload=payload)
    db.add(event)
    db.flush()
    if not already_delivered_to_owner:
        for hook in db.scalars(select(Webhook).where(Webhook.owner_id == monitor.owner_id,
                                                       Webhook.enabled.is_(True))):
            db.add(Delivery(event_id=event.id, webhook_id=hook.id))


def _price_event(db: Session, monitor: Monitor, listing: Listing, price: Decimal,
                 observed_at, baseline: bool) -> None:
    key = "price:" + listing.id
    state = db.scalar(select(EvaluationState).where(EvaluationState.monitor_id == monitor.id,
                                                    EvaluationState.subject_key == key).with_for_update())
    if not state:
        state = EvaluationState(monitor_id=monitor.id, subject_key=key)
        db.add(state)
    old = Decimal(state.last_value) if state.last_value is not None else None
    if old is not None and not baseline and old != price:
        event_type = "price.decreased" if price < old else "price.increased"
        emit(db, monitor, key, event_type, f"{old}:{price}:{observed_at.isoformat()}",
             {"listing_id": listing.id, "old": str(old), "new": str(price),
              "store_id": listing.store_id, "channel": listing.channel})
    threshold = monitor.conditions.get("maximum_price")
    if threshold is not None and not baseline and price <= Decimal(str(threshold)) and (
        old is None or old > Decimal(str(threshold))
    ):
        emit(db, monitor, key, "price.threshold_reached", f"{price}:{observed_at.isoformat()}",
             {"listing_id": listing.id, "price": str(price), "threshold": str(threshold)})
    state.last_value = str(price)
    state.last_observed_at = observed_at


def _availability_event(db: Session, monitor: Monitor, listing: Listing,
                        availability: str | None, observed_at, baseline: bool) -> None:
    if availability not in {"in_stock", "out_of_stock"}:
        return
    key = "availability:" + listing.id
    state = db.scalar(select(EvaluationState).where(EvaluationState.monitor_id == monitor.id,
        EvaluationState.subject_key == key).with_for_update())
    if not state:
        state = EvaluationState(monitor_id=monitor.id, subject_key=key)
        db.add(state)
    previous = state.last_value
    if previous in {"in_stock", "out_of_stock"} and previous != availability and not baseline:
        event_type = "product.restocked" if availability == "in_stock" else "product.unavailable"
        emit(db, monitor, key, event_type, f"{previous}:{availability}:{observed_at.isoformat()}",
             {"listing_id": listing.id, "old": previous, "new": availability,
              "store_id": listing.store_id, "channel": listing.channel})
    state.last_value = availability
    state.last_observed_at = observed_at


def process_scan(db: Session, job: Job) -> dict:
    monitor = db.get(Monitor, job.monitor_id)
    if not monitor or not monitor.enabled:
        return {"status": "cancelled"}
    store = db.get(Store, monitor.store_id)
    connector = connector_for(store.retailer_id) if store else None
    if not connector:
        return {"status": "unsupported", "complete": False}
    snapshot = connector.scan(store.external_id, monitor.channel)
    if not snapshot.complete:
        return {"status": "partial", "complete": False, "source": snapshot.source}
    now = utcnow()
    # A fresh run ID per execution: an admin retry of a partial job re-runs the same job row,
    # and observations from the first run are already stored under (listing, run_id).
    run_id = new_id()
    baseline = not monitor.baseline_complete
    price_count = 0
    offer_count = 0
    invalid_count = 0
    seen_skus: set[str] = set()
    seen_offers: set[str] = set()
    for item in snapshot.prices:
        if (item.currency != "SEK" or not item.current_price.is_finite() or
            item.current_price < 0 or item.channel != monitor.channel or
            item.sku in seen_skus):
            invalid_count += 1
            continue
        seen_skus.add(item.sku)
        listing = db.scalar(select(Listing).where(Listing.retailer_id == store.retailer_id,
            Listing.sku == item.sku, Listing.store_id == store.id, Listing.channel == item.channel))
        if not listing:
            product = Product(name=item.name, brand=item.brand, verified=False,
                              package_value=item.package_value, package_unit=item.package_unit)
            db.add(product)
            db.flush()
            listing = Listing(retailer_id=store.retailer_id, sku=item.sku, store_id=store.id,
                              channel=item.channel, product_id=product.id, name=item.name)
            db.add(listing)
            db.flush()
        calculated_unit_price = unit_price(item.current_price, item.package_value,
                                           item.package_unit)
        db.add(PriceObservation(listing_id=listing.id, run_id=run_id, current_price=item.current_price,
                                regular_price=item.regular_price, currency=item.currency,
                                unit_price=calculated_unit_price[0] if calculated_unit_price else None,
                                unit_price_unit=calculated_unit_price[1] if calculated_unit_price else None,
                                availability=item.availability, source_url=item.source_url,
                                observed_at=now))
        price_count += 1
        if monitor.type == "exact_product" and monitor.conditions.get("listing_id") != listing.id:
            continue
        if monitor.type == "generic_product":
            conditions = monitor.conditions
            query = str(conditions.get("query", "")).casefold()
            if query not in item.name.casefold():
                continue
            if any(term.casefold() in item.name.casefold() for term in
                   conditions.get("excluded_terms", [])):
                continue
            if conditions.get("brand") and (item.brand or "").casefold() != str(conditions["brand"]).casefold():
                continue
            package = base_quantity(item.package_value, item.package_unit)
            minimum_weight = conditions.get("minimum_package_weight_kg")
            if minimum_weight is not None and (not package or package[1] != "kg" or
                package[0] < Decimal(str(minimum_weight))):
                continue
            maximum_weight = conditions.get("maximum_package_weight_kg")
            if maximum_weight is not None and (not package or package[1] != "kg" or
                package[0] > Decimal(str(maximum_weight))):
                continue
            maximum_unit_price = conditions.get("maximum_unit_price")
            if maximum_unit_price is not None and (not calculated_unit_price or
                calculated_unit_price[1] != maximum_unit_price.get("unit") or
                calculated_unit_price[0] > Decimal(str(maximum_unit_price.get("amount")))):
                continue
            discovery_key = "product:" + listing.id
            discovered = db.scalar(select(EvaluationState).where(
                EvaluationState.monitor_id == monitor.id,
                EvaluationState.subject_key == discovery_key).with_for_update())
            if not discovered:
                db.add(EvaluationState(monitor_id=monitor.id, subject_key=discovery_key,
                                       last_value="seen", last_observed_at=now))
                if not baseline:
                    emit(db, monitor, discovery_key, "product.discovered", listing.id,
                         {"listing_id": listing.id, "name": listing.name})
        if monitor.type in ("exact_product", "generic_product"):
            _price_event(db, monitor, listing, item.current_price, now, baseline)
            _availability_event(db, monitor, listing, item.availability, now, baseline)
    for item in snapshot.offers:
        if (item.currency != "SEK" or item.channel != monitor.channel or
            item.identity_key in seen_offers or
            (item.price is not None and (not item.price.is_finite() or item.price < 0))):
            invalid_count += 1
            continue
        seen_offers.add(item.identity_key)
        content = json.dumps({"title": item.title, "terms": item.terms,
                              "price": str(item.price), "eligibility": item.eligibility}, sort_keys=True)
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        offer = db.scalar(select(Offer).where(Offer.retailer_id == store.retailer_id,
            Offer.store_id == store.id, Offer.channel == item.channel,
            Offer.identity_key == item.identity_key).with_for_update())
        created = offer is None
        if created:
            offer = Offer(retailer_id=store.retailer_id, store_id=store.id,
                          channel=item.channel, identity_key=item.identity_key,
                          title=item.title, terms=item.terms, price=item.price,
                          currency=item.currency, eligibility=item.eligibility,
                          content_hash=content_hash, source_url=item.source_url,
                          first_seen=now, last_seen=now)
            db.add(offer)
            db.flush()
        else:
            offer.title, offer.terms, offer.price = item.title, item.terms, item.price
            offer.content_hash, offer.last_seen = content_hash, now
        db.add(OfferObservation(offer_id=offer.id, run_id=run_id,
                                content_hash=content_hash, title=item.title, terms=item.terms,
                                price=item.price, currency=item.currency,
                                eligibility=item.eligibility, source_url=item.source_url,
                                observed_at=now))
        offer_count += 1
        if monitor.type == "store":
            subject = "offer:" + offer.id
            state = db.scalar(select(EvaluationState).where(EvaluationState.monitor_id == monitor.id,
                EvaluationState.subject_key == subject).with_for_update())
            previous_hash = state.last_value if state else None
            if not state:
                state = EvaluationState(monitor_id=monitor.id, subject_key=subject)
                db.add(state)
            if not baseline or monitor.notify_existing_offers:
                if previous_hash is None:
                    emit(db, monitor, subject, "offer.detected", offer.id,
                         {"offer_id": offer.id, "title": offer.title, "store_id": store.id})
                elif previous_hash != content_hash:
                    # Timestamp keeps a revision that returns to an earlier state (A, B, A, B)
                    # distinct; the evaluation state above already prevents repeats.
                    emit(db, monitor, subject, "offer.changed",
                         f"{content_hash[:16]}:{now.isoformat()}",
                         {"offer_id": offer.id, "title": offer.title, "store_id": store.id})
            state.last_value = content_hash
            state.last_observed_at = now
    if invalid_count == 0:
        monitor.baseline_complete = True
        monitor.last_success_at = now
    monitor.next_due_at = now + timedelta(minutes=monitor.interval_minutes)
    return {"status": "success" if invalid_count == 0 else "partial",
            "complete": invalid_count == 0, "prices": price_count,
            "offers": offer_count, "invalid_items": invalid_count, "source": snapshot.source}

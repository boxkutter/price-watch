import hashlib
import json
import random
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import ScanRequest
from app.connectors.registry import connector_for
from app.ingest import add_price, ensure_listing
from app.models import (
    Delivery,
    EvaluationState,
    Event,
    Job,
    Listing,
    Monitor,
    Offer,
    OfferObservation,
    Store,
    Webhook,
    new_id,
    utcnow,
)
from app.multibuy import parse_deal
from app.pricing import base_quantity
from app.retrieval import fetch


def next_due(interval_minutes: int):
    """Next check time with up to 10% jitter, so monitors do not all fire at the same instant."""
    jitter = random.uniform(0, interval_minutes * 6)  # seconds: 10% of the interval
    return utcnow() + timedelta(minutes=interval_minutes, seconds=jitter)


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


def _multibuy_event(db: Session, monitor: Monitor, offer, item, content_hash: str, matched: set[str],
                    single_price: dict, listing_of: dict) -> None:
    """A public multi-buy that brings the price per item down to the alert price ("2 för 35 kr" is
    17,50 kr each) is the same news as the single price reaching it. Member offers are not counted:
    the person may not be a member."""
    threshold = monitor.conditions.get("maximum_price")
    if threshold is None or monitor.type == "store" or item.eligibility != "public":
        return
    sku = next((s for s in item.skus if s in matched), None)
    deal = parse_deal(item.terms, single_price.get(sku)) if sku else None
    if not deal or not deal.reaches(Decimal(str(threshold))):
        return
    emit(db, monitor, "offer:" + offer.id, "price.threshold_reached", f"multibuy:{content_hash[:16]}",
         {"listing_id": listing_of[sku].id, "price": str(deal.unit), "threshold": str(threshold),
          "multibuy": True, "terms": item.terms, "quantity": deal.quantity, "offer_id": offer.id,
          "valid_to": offer.valid_to.isoformat() if offer.valid_to else None})


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


def scan_request(db: Session, monitor: Monitor) -> ScanRequest | None:
    """The targeted request a monitor needs. Live connectors refuse the empty (store) request."""
    if monitor.type == "exact_product":
        listing = db.get(Listing, str(monitor.conditions.get("listing_id")))
        return ScanRequest(skus=(listing.sku,)) if listing else None
    if monitor.type == "generic_product":
        return ScanRequest(query=str(monitor.conditions.get("query", "")).strip())
    return ScanRequest()


def process_scan(db: Session, job: Job) -> dict:
    monitor = db.get(Monitor, job.monitor_id)
    if not monitor or not monitor.enabled:
        return {"status": "cancelled"}
    store = db.get(Store, monitor.store_id)
    connector = connector_for(store.retailer_id) if store else None
    if not connector:
        return {"status": "unsupported", "complete": False}
    request = scan_request(db, monitor)
    if request is None:
        return {"status": "cancelled"}
    snapshot, skipped = fetch(db, store, connector, monitor.channel, request,
                              fresh=bool((job.payload or {}).get("fresh")))
    if skipped:
        return skipped
    status = snapshot.effective_status
    if status != "success":
        # Failed, refused or partial scans never create price, stock or offer transitions.
        return {"status": status, "complete": False, "source": snapshot.source,
                "detail": snapshot.detail, "missing": list(snapshot.missing),
                "requests": snapshot.requests}
    now = utcnow()
    run_id = new_id()  # per execution; an admin retry re-runs the same job row
    baseline = not monitor.baseline_complete
    price_count = offer_count = invalid_count = 0
    seen_skus: set[str] = set()
    seen_offers: set[str] = set()
    matched: set[str] = set()  # SKUs this monitor cares about, for product-level offers
    single_price: dict[str, Decimal] = {}  # the price of one item, for "3 för 2" style offers
    listing_of: dict[str, Listing] = {}
    for item in snapshot.prices:
        if (item.currency != "SEK" or not item.current_price.is_finite() or
            item.current_price < 0 or item.channel != monitor.channel or
            item.sku in seen_skus):
            invalid_count += 1
            continue
        seen_skus.add(item.sku)
        listing = ensure_listing(db, store, item)
        calculated_unit_price = add_price(db, listing, item, run_id, now)
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
        matched.add(item.sku)
        single_price[item.sku] = item.current_price
        listing_of[item.sku] = listing
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
        content_fields = {"title": item.title, "terms": item.terms, "price": str(item.price),
                          "eligibility": item.eligibility}
        # Dates join the hash only when known, so offers stored before dates existed keep their hash.
        if item.valid_from:
            content_fields["valid_from"] = item.valid_from.isoformat()
        if item.valid_to:
            content_fields["valid_to"] = item.valid_to.isoformat()
        content_hash = hashlib.sha256(json.dumps(content_fields, sort_keys=True).encode()).hexdigest()
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
                          first_seen=now, last_seen=now,
                          valid_from=item.valid_from, valid_to=item.valid_to)
            db.add(offer)
            db.flush()
        else:
            offer.title, offer.terms, offer.price = item.title, item.terms, item.price
            offer.eligibility = item.eligibility
            offer.valid_from, offer.valid_to = item.valid_from, item.valid_to
            offer.content_hash, offer.last_seen = content_hash, now
        db.add(OfferObservation(offer_id=offer.id, run_id=run_id,
                                content_hash=content_hash, title=item.title, terms=item.terms,
                                price=item.price, currency=item.currency,
                                eligibility=item.eligibility, source_url=item.source_url,
                                valid_from=item.valid_from, valid_to=item.valid_to,
                                observed_at=now))
        offer_count += 1
        # Store monitors follow every offer in a bounded feed; product monitors only the
        # promotions attached to the products they match.
        if monitor.type == "store" or set(item.skus) & matched:
            subject = "offer:" + offer.id
            state = db.scalar(select(EvaluationState).where(EvaluationState.monitor_id == monitor.id,
                EvaluationState.subject_key == subject).with_for_update())
            previous_hash = state.last_value if state else None
            if not state:
                state = EvaluationState(monitor_id=monitor.id, subject_key=subject)
                db.add(state)
            if not baseline or monitor.notify_existing_offers:
                payload = {"offer_id": offer.id, "title": offer.title, "store_id": store.id,
                           "terms": offer.terms, "eligibility": offer.eligibility,
                           "valid_to": offer.valid_to.isoformat() if offer.valid_to else None}
                if previous_hash is None:
                    emit(db, monitor, subject, "offer.detected", offer.id, payload)
                elif previous_hash != content_hash:
                    # Timestamp keeps a revision that returns to an earlier state (A, B, A, B)
                    # distinct; the evaluation state above already prevents repeats.
                    emit(db, monitor, subject, "offer.changed",
                         f"{content_hash[:16]}:{now.isoformat()}", payload)
                if previous_hash != content_hash:
                    _multibuy_event(db, monitor, offer, item, content_hash, matched, single_price, listing_of)
            state.last_value = content_hash
            state.last_observed_at = now
    if invalid_count == 0:
        monitor.baseline_complete = True
        monitor.last_success_at = now
    monitor.next_due_at = next_due(monitor.interval_minutes)
    return {"status": "success" if invalid_count == 0 else "partial",
            "complete": invalid_count == 0, "prices": price_count,
            "offers": offer_count, "invalid_items": invalid_count, "source": snapshot.source,
            "truncated": snapshot.truncated, "detail": snapshot.detail,
            "requests": snapshot.requests}

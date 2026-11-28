"""Compare one product's price across a person's stores, starting from a scanned barcode.

Standing in shop A you scan a barcode; this finds the same item in your other stores. What the
catalogue already knows is returned at once (with its age), then a worker job checks each store
live: one request per store, through the same gate as every other lookup (spacing, cache, pause,
daily budget), and capped at `max_compare_stores`.
"""
from decimal import Decimal

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.barcodes import product_for_gtin, product_summary, search_form
from app.connectors import registry
from app.connectors.base import ScanRequest
from app.connectors.registry import connector_for
from app.deals import best_dict, best_for, recorded_offers
from app.ingest import add_price, ensure_listing
from app.models import Job, Listing, PriceObservation, Store, aware, new_id, utcnow
from app.retrieval import fetch


def request_for_barcode(db: Session, retailer_id: str, gtin14: str,
                        given: str | None = None) -> ScanRequest | None:
    """How to ask this retailer about a barcode. A connector that can look barcodes up is asked
    directly; one that cannot is asked about the SKUs already linked to that barcode (None when
    none are known yet)."""
    info = registry.retailer(retailer_id) or {}
    if info.get("capabilities", {}).get("barcode_search") == "supported":
        return ScanRequest(gtins=(search_form(gtin14, given),))
    product = product_for_gtin(db, gtin14)
    if not product:
        return None
    skus = tuple(db.scalars(select(Listing.sku).where(
        Listing.product_id == product.id, Listing.retailer_id == retailer_id).distinct()))
    return ScanRequest(skus=skus) if skus else None


def _money(value: Decimal) -> str:
    """Prices read the same whether they came from the catalogue or a live lookup."""
    return str(value.quantize(Decimal("0.01")))


def _row(store: Store, status: str, **fields) -> dict:
    return {"store_id": store.id, "store_name": store.name, "retailer_id": store.retailer_id,
            "status": status, **fields}


def _found(store: Store, name: str, sku: str, price: Decimal, regular: Decimal | None,
           currency: str, unit: tuple | None, availability: str | None, observed_at,
           source: str, url: str | None, best: dict | None = None) -> dict:
    """A store's price for the item. `current_price` is what one costs on the shelf; `best` is the
    cheapest way this person can buy it (a multi-buy, a percentage off, a member price) and
    `effective_price` the price per item that comes to: the one stores are ranked by."""
    return _row(store, "found", product_name=name, sku=sku, current_price=_money(price),
                regular_price=_money(regular) if regular is not None else None, currency=currency,
                effective_price=best["unit_price"] if best else _money(price), best=best,
                unit_price={"amount": str(unit[0]), "unit": unit[1]} if unit else None,
                availability=availability, observed_at=observed_at,
                age_seconds=max(0, int((utcnow() - aware(observed_at)).total_seconds())),
                source=source, source_url=url)


def known_rows(db: Session, gtin14: str, stores: list[Store], user_id: str | None = None) -> list[dict]:
    """What the catalogue already knows about this barcode in each store, with its age, and the best
    price [user_id] can get there with the offers on record."""
    product = product_for_gtin(db, gtin14)
    rows = []
    for store in stores:
        listing = db.scalar(select(Listing).where(
            Listing.product_id == product.id, Listing.store_id == store.id,
            Listing.channel == "online")) if product else None
        obs = db.scalar(select(PriceObservation).where(
            PriceObservation.listing_id == listing.id, PriceObservation.account_id.is_(None),
            PriceObservation.eligibility == "public")
            .order_by(PriceObservation.observed_at.desc()).limit(1)) if listing else None
        if not obs or obs.current_price is None:
            rows.append(_row(store, "unknown", detail="Not checked in this store yet"))
            continue
        unit = (obs.unit_price, obs.unit_price_unit) if obs.unit_price is not None else None
        best = best_dict(best_for(db, user_id, store.retailer_id, obs.current_price,
                                  recorded_offers(db, store.id, "online", listing.sku)))
        rows.append(_found(store, listing.name, listing.sku, obs.current_price, obs.regular_price,
                           obs.currency, unit, obs.availability, obs.observed_at, "catalogue",
                           obs.source_url, best))
    return rows


def summarize(rows: list[dict], here_store_id: str | None) -> dict:
    """Mark each found row against the cheapest and against the store you are standing in, by the
    price per item this person can actually get (`effective_price`), not the shelf price alone."""
    found = [r for r in rows if r["status"] == "found"]
    cheapest = min(found, key=lambda r: Decimal(r["effective_price"]), default=None)
    here = next((r for r in found if r["store_id"] == here_store_id), None)

    def gap(price: str, reference: str) -> dict:
        diff = Decimal(price) - Decimal(reference)
        pct = (diff / Decimal(reference) * 100).quantize(Decimal("0.1")) if Decimal(reference) else None
        return {"amount": str(diff.quantize(Decimal("0.01"))), "percent": str(pct) if pct is not None else None}

    for row in found:
        row["vs_cheapest"] = gap(row["effective_price"], cheapest["effective_price"])
        if here:
            row["vs_here"] = gap(row["effective_price"], here["effective_price"])
        row["is_cheapest"] = row is cheapest
    return {"cheapest": {"store_id": cheapest["store_id"], "store_name": cheapest["store_name"],
                         "current_price": cheapest["current_price"],
                         "effective_price": cheapest["effective_price"]} if cheapest else None,
            "saving_vs_here": (gap(cheapest["effective_price"], here["effective_price"])
                               if cheapest and here and cheapest is not here else None)}


def process_compare(db: Session, job: Job) -> dict:
    payload = job.payload or {}
    gtin14, given = payload["gtin14"], payload.get("given")
    stores = [s for s in (db.get(Store, sid) for sid in payload["store_ids"]) if s]
    run_id, now = new_id(), utcnow()
    rows: list[dict] = []
    requests, complete = 0, True
    for store in stores:
        connector = connector_for(store.retailer_id)
        if connector is None:
            rows.append(_row(store, "unsupported", detail="The store's connector is not installed"))
            complete = False
            continue
        request = request_for_barcode(db, store.retailer_id, gtin14, given)
        if request is None:
            rows.append(_row(store, "unknown_mapping", detail=(
                "This store cannot search by barcode and no listing of this product is linked to "
                "it yet; link its barcode to the product here first")))
            complete = False
            continue
        snapshot, skipped = fetch(db, store, connector, "online", request)
        if skipped:
            rows.append(_row(store, skipped["status"], detail=skipped.get("detail")))
            complete = False
            continue
        requests += snapshot.requests
        status = snapshot.effective_status
        if status == "not_found" or (status == "success" and not snapshot.prices):
            rows.append(_row(store, "not_found", detail="This store does not list that barcode"))
            continue
        if status != "success" or not snapshot.prices:
            rows.append(_row(store, status, detail=snapshot.detail))
            complete = False
            continue
        item = snapshot.prices[0]
        if (item.currency != "SEK" or not item.current_price.is_finite() or item.current_price < 0
                or item.channel != "online"):
            rows.append(_row(store, "invalid_data", detail="No usable price"))
            complete = False
            continue
        listing = ensure_listing(db, store, item)
        unit = add_price(db, listing, item, run_id, now)
        offers = [o for o in snapshot.offers if item.sku in o.skus]
        best = best_dict(best_for(db, job.owner_id, store.retailer_id, item.current_price, offers))
        rows.append(_found(store, listing.name, item.sku, item.current_price, item.regular_price,
                           item.currency, unit, item.availability, now, "live", item.source_url, best))
    product = product_for_gtin(db, gtin14)
    summary = summarize(rows, payload.get("here_store_id"))
    # The result is stored as JSON, so timestamps become ISO strings here.
    return jsonable_encoder({
        "status": "success" if complete else "partial", "complete": complete,
        "gtin14": gtin14, "here_store_id": payload.get("here_store_id"),
        "product": product_summary(db, product) if product else None,
        "rows": rows, **summary, "requests": requests})

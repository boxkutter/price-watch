from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.registry import connector_for
from app.models import Job, Listing, PriceObservation, Product, Store, new_id, utcnow
from app.pricing import unit_price


def process_search(db: Session, job: Job) -> dict:
    payload = job.payload or {}
    query = str(payload.get("query") or "").casefold().strip()
    gtin14 = str(payload.get("gtin") or "").zfill(14)
    known_skus = set()
    if payload.get("gtin"):
        product = db.scalar(select(Product).where(Product.gtin14 == gtin14,
                                                   Product.verified.is_(True)))
        if product:
            known_skus = set(db.execute(select(Listing.retailer_id, Listing.sku).where(
                Listing.product_id == product.id)).all())
    channel = payload.get("channel", "online")
    run_id = new_id()  # per execution, so an admin retry cannot collide with the first run
    items = []
    coverage = []
    for store_id in payload.get("store_ids", []):
        store = db.get(Store, store_id)
        connector = connector_for(store.retailer_id) if store else None
        if not connector:
            coverage.append({"store_id": store_id, "status": "unsupported"})
            continue
        snapshot = connector.scan(store.external_id, channel)
        if not snapshot.complete:
            coverage.append({"store_id": store_id, "status": "partial", "source": snapshot.source})
            continue
        coverage.append({"store_id": store_id, "status": "complete", "source": snapshot.source})
        seen_skus: set[str] = set()
        for item in snapshot.prices:
            if item.sku in seen_skus:
                continue
            seen_skus.add(item.sku)
            matches = ((bool(query) and query in item.name.casefold()) or
                       (store.retailer_id, item.sku) in known_skus)
            if (not matches or item.channel != channel or
                item.currency != "SEK" or not item.current_price.is_finite() or
                item.current_price < 0):
                continue
            listing = db.scalar(select(Listing).where(Listing.retailer_id == store.retailer_id,
                Listing.sku == item.sku, Listing.store_id == store.id,
                Listing.channel == channel))
            if not listing:
                product = Product(name=item.name, brand=item.brand,
                                  package_value=item.package_value,
                                  package_unit=item.package_unit, verified=False)
                db.add(product)
                db.flush()
                listing = Listing(retailer_id=store.retailer_id, sku=item.sku,
                                  store_id=store.id, channel=channel,
                                  product_id=product.id, name=item.name)
                db.add(listing)
                db.flush()
            unit = unit_price(item.current_price, item.package_value, item.package_unit)
            db.add(PriceObservation(listing_id=listing.id, run_id=run_id,
                                    current_price=item.current_price,
                                    regular_price=item.regular_price,
                                    unit_price=unit[0] if unit else None,
                                    unit_price_unit=unit[1] if unit else None,
                                    currency=item.currency, availability=item.availability,
                                    source_url=item.source_url, observed_at=utcnow()))
            items.append({"product_id": listing.product_id, "listing_id": listing.id,
                          "store_id": store.id, "channel": channel, "name": item.name,
                          "current_price": str(item.current_price), "currency": item.currency})
    complete = all(scope["status"] == "complete" for scope in coverage)
    return {"status": "success" if complete else "partial", "complete": complete,
            "items": items, "coverage": coverage,
            "barcode_mapping": "verified" if known_skus else "unknown" if payload.get("gtin") else None}

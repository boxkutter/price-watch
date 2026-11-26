from sqlalchemy import select
from sqlalchemy.orm import Session

from app.barcodes import product_for_gtin  # noqa: F401
from app.compare import request_for_barcode
from app.connectors import registry
from app.connectors.base import ScanRequest
from app.connectors.registry import connector_for
from app.gtin import normalize_gtin
from app.ingest import add_price, ensure_listing
from app.models import Job, Listing, Product, Store, new_id, utcnow
from app.retrieval import fetch


def process_search(db: Session, job: Job) -> dict:
    payload = job.payload or {}
    query = str(payload.get("query") or "").casefold().strip()
    gtin = str(payload.get("gtin") or "")
    gtin14 = normalize_gtin(gtin) if gtin else None
    known_skus = set()
    if gtin14:
        product = db.scalar(select(Product).where(Product.gtin14 == gtin14,
                                                   Product.verified.is_(True)))
        if product:
            known_skus = set(db.execute(select(Listing.retailer_id, Listing.sku).where(
                Listing.product_id == product.id)).all())
    channel = payload.get("channel", "online")
    run_id = new_id()  # per execution, so an admin retry cannot collide with the first run
    items = []
    coverage = []
    barcode_found = False
    for store_id in payload.get("store_ids", []):
        store = db.get(Store, store_id)
        connector = connector_for(store.retailer_id) if store else None
        if not connector:
            coverage.append({"store_id": store_id, "status": "unsupported"})
            continue
        # Ask a store only for what its manifest says it can do.
        searchable = (registry.retailer(store.retailer_id) or {}).get(
            "capabilities", {}).get("product_search") == "supported"
        if query and not searchable and not gtin14:
            coverage.append({"store_id": store_id, "status": "unsupported",
                             "detail": "This store cannot be searched by name; add its products "
                                       "by their page link"})
            continue
        request = ScanRequest(query=(query or None) if searchable else None)
        if gtin14:
            by_barcode = request_for_barcode(db, store.retailer_id, gtin14, gtin)
            if by_barcode is None and not request.query:
                coverage.append({"store_id": store_id, "status": "unknown_mapping",
                                 "detail": "This store cannot search by barcode and no listing of "
                                           "this product is linked to it yet"})
                continue
            if by_barcode is not None:
                request = ScanRequest(query=request.query, skus=by_barcode.skus, gtins=by_barcode.gtins)
        snapshot, skipped = fetch(db, store, connector, channel, request)
        if skipped:
            coverage.append({"store_id": store_id, "status": skipped["status"],
                             "detail": skipped.get("detail")})
            continue
        if snapshot.effective_status not in ("success", "not_found"):
            coverage.append({"store_id": store_id, "status": snapshot.effective_status,
                             "source": snapshot.source, "detail": snapshot.detail})
            continue
        # not_found is a complete answer for a barcode/ID search: the retailer was asked and
        # returned nothing. It says nothing about whether the product exists elsewhere.
        coverage.append({"store_id": store_id,
                         "status": "complete" if snapshot.effective_status == "success" else "not_found",
                         "source": snapshot.source, "detail": snapshot.detail,
                         "truncated": snapshot.truncated})
        seen_skus: set[str] = set()
        for item in snapshot.prices:
            if item.sku in seen_skus:
                continue
            seen_skus.add(item.sku)
            # A connector that honours the request has already filtered; others return a full
            # snapshot that is matched here by name or by a verified barcode mapping.
            matches = snapshot.targeted or (
                (bool(query) and query in item.name.casefold()) or
                (store.retailer_id, item.sku) in known_skus)
            if (not matches or item.channel != channel or
                item.currency != "SEK" or not item.current_price.is_finite() or
                item.current_price < 0):
                continue
            listing = ensure_listing(db, store, item)
            add_price(db, listing, item, run_id, utcnow())
            if gtin14 and item.gtin:
                try:
                    barcode_found = barcode_found or normalize_gtin(item.gtin) == gtin14
                except ValueError:
                    pass
            product = db.get(Product, listing.product_id) if listing.product_id else None
            items.append({"product_id": listing.product_id, "listing_id": listing.id,
                          "barcode": ({"gtin14": product.gtin14, "source": product.gtin_source}
                                      if product and product.gtin14 else None),
                          "store_id": store.id, "channel": channel, "name": item.name,
                          "current_price": str(item.current_price), "currency": item.currency})
    complete = all(scope["status"] in ("complete", "not_found") for scope in coverage)
    return {"status": "success" if complete else "partial", "complete": complete,
            "items": items, "coverage": coverage,
            "barcode_mapping": ("verified" if known_skus or barcode_found
                                else "unknown" if gtin else None)}

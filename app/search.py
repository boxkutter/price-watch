from dataclasses import replace
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import glossary, names
from app.barcodes import product_for_gtin  # noqa: F401
from app.compare import request_for_barcode
from app.connectors import registry
from app.connectors.base import ScanRequest
from app.connectors.registry import connector_for
from app.glossary import fold
from app.gtin import normalize_gtin
from app.ingest import add_price, ensure_listing
from app.models import Job, Listing, Product, Store, new_id, utcnow
from app.naming import (
    Described,
    describe,
    display_name,
    make_narrow,
    normalize_brand,
    parse_query,
    size_label,
    tokens,
)
from app.reference import best_image, reference_info
from app.retrieval import fetch


def money(value: Decimal) -> str:
    """A price with at least two decimals: 15.5 is shown as 15.50."""
    return format(value.quantize(Decimal("0.01")) if value.as_tuple().exponent > -2 else value, "f")


def process_search(db: Session, job: Job) -> dict:
    payload = job.payload or {}
    typed = parse_query(str(payload.get("query") or "").strip()) if payload.get("query") else None
    narrow = make_narrow(payload.get("brand"), payload.get("pack_min"), payload.get("pack_max"),
                         payload.get("weight_min"),
                         payload.get("weight_max"), payload.get("weight_unit") or "g")
    if typed is not None and payload.get("brand") and not set(narrow.brand) <= set(tokens(typed.store_text)):
        typed = replace(typed, store_text=f"{typed.store_text} {payload['brand'].strip()}")
    glossary.refresh(db)        # the words people added
    query = typed.store_text.casefold() if typed else ""    # what the stores are asked (no size)
    # A typed English word is also asked in Swedish: shops here are Swedish.
    translated = glossary.translate(typed.store_text).casefold() if typed else ""
    extra_query = translated if translated and fold(translated) != fold(query) else ""
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
    described: dict[str, Described] = {}
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
        batches = [(snapshot, None)]
        if extra_query and searchable and not gtin14:     # the same words, in Swedish
            more, skipped_more = fetch(db, store, connector, channel, ScanRequest(query=extra_query))
            if not skipped_more and more.effective_status == "success":
                batches.append((more, None))
        returned = {price.sku for snap, _ in batches for price in snap.prices}
        if typed is not None and searchable and not gtin14:
            # products this store lists under a name the typed words do not match, but another of
            # the product's names does (an alternative name, or another shop's wording)
            skus = names.matching_skus(db, store, typed, returned)
            if skus:
                by_name, skipped_names = fetch(db, store, connector, channel,
                                               ScanRequest(skus=tuple(skus)))
                if not skipped_names and by_name.effective_status == "success":
                    batches.append((by_name, "alternative name"))
        scope = {"store_id": store_id,
                 "status": "complete" if snapshot.effective_status == "success" else "not_found",
                 "source": snapshot.source, "detail": snapshot.detail,
                 "truncated": snapshot.truncated, "filtered_out": 0}
        coverage.append(scope)
        seen_skus: set[str] = set()
        entries = [(price, snap.targeted, via) for snap, via in batches for price in snap.prices]
        offers_of: dict[str, list] = {}
        for snap, _via in batches:
            for offer in snap.offers:
                for offer_sku in offer.skus:
                    offers_of.setdefault(offer_sku, []).append(offer)
        for item, targeted, via in entries:
            if item.sku in seen_skus:
                continue
            seen_skus.add(item.sku)
            # A connector that honours the request has already filtered; others return a full
            # snapshot that is matched here by name or by a verified barcode mapping.
            matches = targeted or (
                (typed is not None and typed.accepts(item.name, item.brand, None)) or
                (store.retailer_id, item.sku) in known_skus)
            if (not matches or item.channel != channel or
                item.currency != "SEK" or not item.current_price.is_finite() or
                item.current_price < 0):
                continue
            parts = describe(item.name, item.brand, item.package_value, item.package_unit)
            if via is None:
                typed_ok = typed is None or typed.accepts(item.name, item.brand, parts.size)
            else:       # found by another of its names: the typed words need not be in this one
                typed_ok = typed is None or not (typed.size and parts.size and typed.size != parts.size)
            if not (typed_ok and narrow.accepts(item.name, item.brand, parts.size, parts.pack_count)):
                scope["filtered_out"] += 1     # the store returned it, but it is not what was typed
                continue
            listing = ensure_listing(db, store, item)
            add_price(db, listing, item, run_id, utcnow())
            if gtin14 and item.gtin:
                try:
                    barcode_found = barcode_found or normalize_gtin(item.gtin) == gtin14
                except ValueError:
                    pass
            product = db.get(Product, listing.product_id) if listing.product_id else None
            item_gtin = product.gtin14 if product else None
            like = parts.like_key(item_gtin)
            described[listing.id] = parts
            items.append({"product_id": listing.product_id, "listing_id": listing.id,
                          "barcode": ({"gtin14": product.gtin14, "source": product.gtin_source}
                                      if product and product.gtin14 else None),
                          "brand": normalize_brand(item.brand), "brand_key": parts.brand_key,
                          "url": item.source_url, "size": parts.size_text,
                          "image": best_image(db, listing.product_id, job.owner_id),
                          "reference": reference_info(db, item_gtin, parts),
                          "display_name": display_name(item.name, item.brand),
                          "pack": parts.pack,
                          "like_key": f"{like[0]}:{like[1]}" if like else None,
                          "via": via,
                          "availability": item.availability, "stock_quantity": item.stock_quantity,
                          "offers": [{"terms": o.terms, "title": o.title,
                                      "price": str(o.price) if o.price is not None else None,
                                      "eligibility": o.eligibility,
                                      "valid_to": o.valid_to.isoformat() if o.valid_to else None}
                                     for o in offers_of.get(item.sku, [])],
                          "store_id": store.id, "channel": channel, "name": item.name,
                          "current_price": money(item.current_price), "currency": item.currency})
    suggest_same(db, items, described)
    complete = all(scope["status"] in ("complete", "not_found") for scope in coverage)
    return {"status": "success" if complete else "partial", "complete": complete,
            "items": items, "coverage": coverage,
            "query": ({"terms": list(typed.terms), "size": size_label(typed.size),
                       "also_searched_as": extra_query or None} if typed else None),
            "barcode_mapping": ("verified" if known_skus or barcode_found
                                else "unknown" if gtin else None)}


def suggest_same(db: Session, items: list[dict], described: dict) -> None:
    """Add what else each result is called and which other results are probably the same item."""
    cores: dict[str, list] = {}
    for item in items:
        product = db.get(Product, item["product_id"]) if item["product_id"] else None
        if product is not None and product.id not in cores:
            cores[product.id] = names.name_cores(db, product)
        item["also_called"] = names.also_called(db, product, item["name"]) if product else []
    names.likely_same(items, cores, described)

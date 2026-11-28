import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import glossary, names, progress
from app.barcodes import product_for_gtin  # noqa: F401
from app.compare import request_for_barcode
from app.config import settings
from app.connectors import registry
from app.connectors.base import ScanRequest
from app.connectors.registry import connector_for
from app.db import SessionLocal
from app.deals import best_dict, best_for, deal_fields
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


@dataclass
class Retrieved:
    """What was fetched from one store, before any of it is stored or shown."""
    store_id: str
    outcome: dict | None = None            # a coverage entry when nothing was fetched (and why)
    snapshot: object | None = None
    batches: list = field(default_factory=list)          # (Snapshot, how found)
    translated: bool = False


class SearchRun:
    """One search across several stores. Stores of different retailers are asked at the same time
    (each retailer is its own website with its own spacing); stores of one retailer share a
    connection and go one after another. Nothing here writes results: that happens afterwards."""

    def __init__(self, job_id: str, typed, query: str, extra_query: str, gtin: str, gtin14: str | None,
                 channel: str, skips: dict, total: int, fresh: bool = False):
        self.job_id, self.typed, self.query, self.extra_query = job_id, typed, query, extra_query
        self.gtin, self.gtin14, self.channel, self.skips = gtin, gtin14, channel, skips
        self.fresh = fresh
        self.deadline = time.monotonic() + settings.search_time_budget_seconds
        self.lock, self.active, self.finished, self.total = threading.Lock(), [], 0, total

    def late(self) -> bool:
        return time.monotonic() > self.deadline

    def _say(self) -> None:
        names_now = self.active[:2]
        if not names_now:
            return
        more = f" and {len(self.active) - 2} more" if len(self.active) > 2 else ""
        verb = "Looking up the barcode in" if self.gtin14 else "Searching"
        progress.report(self.job_id, f"{verb} {', '.join(names_now)}{more}", self.finished, self.total)

    def _started(self, name: str) -> None:
        with self.lock:
            self.active.append(name)
            self._say()

    def _done(self, name: str) -> None:
        with self.lock:
            self.active.remove(name)
            self.finished += 1
            self._say()

    def retrieve_group(self, store_ids: list[str]) -> dict[str, Retrieved]:
        found: dict[str, Retrieved] = {}
        with SessionLocal.begin() as db:
            for store_id in store_ids:
                store = db.get(Store, store_id)
                name = store.name if store else "a store"
                if self.late():
                    found[store_id] = Retrieved(store_id, outcome={
                        "store_id": store_id, "status": "timed_out",
                        "detail": "Not searched: the search ran out of time. Search this store on its own."})
                    self._done_without_start()
                    continue
                self._started(name)
                try:
                    found[store_id] = self.retrieve_store(db, store)
                finally:
                    self._done(name)
        return found

    def _done_without_start(self) -> None:
        with self.lock:
            self.finished += 1

    def retrieve_store(self, db: Session, store: Store | None) -> Retrieved:
        store_id = store.id if store else ""
        connector = connector_for(store.retailer_id) if store else None
        if not connector:
            return Retrieved(store_id, outcome={"store_id": store_id, "status": "unsupported"})
        # Ask a store only for what its manifest says it can do.
        searchable = (registry.retailer(store.retailer_id) or {}).get(
            "capabilities", {}).get("product_search") == "supported"
        if self.query and not searchable and not self.gtin14:
            return Retrieved(store_id, outcome={
                "store_id": store_id, "status": "unsupported",
                "detail": "This store cannot be searched by name; add its products by their page link"})
        skip = self.skips.get(store_id, 0) if searchable else 0
        text_search = bool(self.query) and searchable and not self.gtin14
        request = ScanRequest(query=(self.query or None) if searchable else None, skip=skip,
                              take=settings.search_page_size if text_search else None)
        if self.gtin14:
            by_barcode = request_for_barcode(db, store.retailer_id, self.gtin14, self.gtin)
            if by_barcode is None and not request.query:
                return Retrieved(store_id, outcome={
                    "store_id": store_id, "status": "unknown_mapping",
                    "detail": "This store cannot search by barcode and no listing of this product "
                              "is linked to it yet"})
            if by_barcode is not None:
                request = ScanRequest(query=request.query, skus=by_barcode.skus, gtins=by_barcode.gtins)
        snapshot, skipped = fetch(db, store, connector, self.channel, request, fresh=self.fresh)
        if skipped:
            return Retrieved(store_id, outcome={"store_id": store_id, "status": skipped["status"],
                                                "detail": skipped.get("detail")})
        if snapshot.effective_status not in ("success", "not_found"):
            return Retrieved(store_id, outcome={"store_id": store_id, "status": snapshot.effective_status,
                                                "source": snapshot.source, "detail": snapshot.detail})
        batches, translated = [(snapshot, None)], False
        first_page = skip == 0
        if text_search and first_page and self.extra_query and not self.late():
            # the same words in Swedish, but only when this store answered the first with little
            good = sum(1 for p in snapshot.prices if self.typed.accepts(p.name, p.brand, None))
            if good < settings.search_translate_below:
                more, skipped_more = fetch(db, store, connector, self.channel,
                                           ScanRequest(query=self.extra_query, take=settings.search_page_size),
                                           fresh=self.fresh)
                if not skipped_more and more.effective_status == "success":
                    batches.append((more, None))
                    translated = True
        if text_search and first_page and not self.late():
            # products this store lists under a name the typed words do not match, but another of
            # the product's names does (an alternative name, or another shop's wording)
            returned = {price.sku for snap, _ in batches for price in snap.prices}
            skus = names.matching_skus(db, store, self.typed, returned)
            if skus:
                by_name, skipped_names = fetch(db, store, connector, self.channel,
                                               ScanRequest(skus=tuple(skus)), fresh=self.fresh)
                if not skipped_names and by_name.effective_status == "success":
                    batches.append((by_name, "alternative name"))
        return Retrieved(store_id, snapshot=snapshot, batches=batches, translated=translated)


def process_search(db: Session, job: Job) -> dict:
    started = time.monotonic()
    payload = job.payload or {}
    typed = parse_query(str(payload.get("query") or "").strip()) if payload.get("query") else None
    narrow = make_narrow(payload.get("brand"), payload.get("pack_min"), payload.get("pack_max"),
                         payload.get("weight_min"),
                         payload.get("weight_max"), payload.get("weight_unit") or "g")
    if typed is not None and payload.get("brand") and not set(narrow.brand) <= set(tokens(typed.store_text)):
        typed = replace(typed, store_text=f"{typed.store_text} {payload['brand'].strip()}")
    glossary.refresh(db)        # the words people added
    query = typed.store_text.casefold() if typed else ""    # what the stores are asked (no size)
    # A typed English word is also asked in Swedish (when a store gives few results): shops here are Swedish.
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
    store_ids = payload.get("store_ids", [])
    skips = {k: int(v) for k, v in (payload.get("skip") or {}).items()}

    # ---- 1. ask the stores: one thread per retailer, so the websites are asked at the same time
    run = SearchRun(job.id, typed, query, extra_query, gtin, gtin14, channel, skips, len(store_ids),
                    fresh=bool(payload.get("fresh")))
    groups: dict[str, list[str]] = {}
    for store_id in store_ids:
        store = db.get(Store, store_id)
        groups.setdefault(store.retailer_id if store else "?", []).append(store_id)
    progress.report(job.id, "Asking the stores...", 0, len(store_ids))
    retrieved: dict[str, Retrieved] = {}
    if len(groups) <= 1:
        for group in groups.values():
            retrieved.update(run.retrieve_group(group))
    else:
        with ThreadPoolExecutor(max_workers=min(len(groups), settings.search_max_parallel)) as pool:
            for result in [f.result() for f in [pool.submit(run.retrieve_group, g) for g in groups.values()]]:
                retrieved.update(result)

    # ---- 2. store and describe what came back, store by store in the order asked
    progress.report(job.id, "Putting the results together", len(store_ids), len(store_ids))
    for store_id in store_ids:
        got = retrieved[store_id]
        if got.outcome:
            coverage.append(got.outcome)
            continue
        store = db.get(Store, store_id)
        snapshot, batches = got.snapshot, got.batches
        # not_found is a complete answer for a barcode/ID search: the retailer was asked and
        # returned nothing. It says nothing about whether the product exists elsewhere.
        scope = {"store_id": store_id,
                 "status": "complete" if snapshot.effective_status == "success" else "not_found",
                 "source": snapshot.source, "detail": snapshot.detail,
                 "truncated": snapshot.truncated, "next_skip": snapshot.next_skip, "filtered_out": 0}
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
                          "best": best_dict(best_for(db, job.owner_id, store.retailer_id, item.current_price,
                                                     offers_of.get(item.sku, []))),
                          "offers": [{"terms": o.terms, "title": o.title,
                                      "price": str(o.price) if o.price is not None else None,
                                      "eligibility": o.eligibility,
                                      "valid_to": o.valid_to.isoformat() if o.valid_to else None,
                                      **deal_fields(o.terms, item.current_price)}
                                     for o in offers_of.get(item.sku, [])],
                          "store_id": store.id, "channel": channel, "name": item.name,
                          "current_price": money(item.current_price), "currency": item.currency})
    suggest_same(db, items, described)
    complete = all(scope["status"] in ("complete", "not_found") for scope in coverage)
    used_translation = any(r.translated for r in retrieved.values())
    return {"status": "success" if complete else "partial", "complete": complete,
            "items": items, "coverage": coverage,
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "query": ({"terms": list(typed.terms), "size": size_label(typed.size),
                       "also_searched_as": extra_query if used_translation else None} if typed else None),
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

"""An open reference database (Open Food Facts and its sister sites) for what a barcode is.

Looked up by the worker only (slowly, through the same polite client the store connectors use),
cached in the database, and shown next to the store's own data: a canonical name, brand and size,
and a picture we keep our own reduced copy of. It never changes a barcode, and it never overrides
the store: where the two disagree we say so and leave the choice to the person.
"""
import logging
import time
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.connectors.base import (
    AccessBlocked,
    ConnectorError,
    ExtractionError,
    HttpStatusError,
    RateLimited,
    TransientError,
)
from app.connectors.http import PoliteClient
from app.images import BadImage, reduce_image
from app.models import Job, Product, ProductImage, ReferenceProduct, utcnow
from app.naming import Described, brand_key, size_in_text, tokens

log = logging.getLogger(__name__)

# Asked in this order: food first, then the sister projects for everything else (batteries,
# toothpaste), so a product that is not food can still be found.
SITES = (("Open Food Facts", "https://world.openfoodfacts.org"),
         ("Open Products Facts", "https://world.openproductsfacts.org"),
         ("Open Beauty Facts", "https://world.openbeautyfacts.org"))
ALLOWED_HOSTS = ["*.openfoodfacts.org", "*.openproductsfacts.org", "*.openbeautyfacts.org"]
FIELDS = ("code,product_name,product_name_sv,product_name_en,brands,quantity,countries_tags,"
          "image_front_url,image_url,categories,ingredients_text,allergens_tags,nutriscore_grade")
CREDIT = "{site} contributors, CC BY-SA"

_client: PoliteClient | None = None
_transport = None              # a test seam: replaces the network
_cooldown_until = 0.0          # after a refusal or a rate limit, ask for nothing until then


def client() -> PoliteClient:
    global _client
    if _client is None:
        _client = PoliteClient(ALLOWED_HOSTS, settings.reference_min_interval_seconds, timeout=10,
                               user_agent=settings.reference_user_agent, transport=_transport)
    return _client


@dataclass
class Found:
    site: str
    name: str | None
    brand: str | None
    quantity: str | None
    page_url: str
    image_url: str | None
    details: dict


def _first(*values):
    return next((str(v).strip() for v in values if v and str(v).strip()), None)


def parse_product(site: str, base: str, code: str, product: dict) -> Found:
    """What we keep of a reference record. The Swedish name wins when there is one."""
    details = {"site": site}
    for key in ("categories", "ingredients_text", "nutriscore_grade"):
        if product.get(key):
            details[key] = str(product[key])[:2000]
    if isinstance(product.get("allergens_tags"), list):
        details["allergens"] = [str(t) for t in product["allergens_tags"][:30]]
    return Found(site=site, name=_first(product.get("product_name_sv"), product.get("product_name"),
                                        product.get("product_name_en")),
                 brand=_first(product.get("brands")), quantity=_first(product.get("quantity")),
                 page_url=f"{base}/product/{code}",
                 image_url=_first(product.get("image_front_url"), product.get("image_url")),
                 details=details)


def lookup(gtin14: str) -> Found | None:
    """Ask each reference site in turn; the first that knows the barcode answers."""
    code = gtin14.lstrip("0") or gtin14
    for site, base in SITES:
        try:
            payload = client().get_json(f"{base}/api/v2/product/{code}.json",
                                        params={"fields": FIELDS})
        except HttpStatusError as exc:
            if exc.status == 404:
                continue
            raise
        product = payload.get("product") if isinstance(payload, dict) else None
        if isinstance(payload, dict) and payload.get("status") != 0 and isinstance(product, dict):
            return parse_product(site, base, code, product)
    return None


def fetch_picture(url: str) -> tuple[bytes, int, int] | None:
    """Our own reduced copy of a reference picture, or None when it cannot be had."""
    try:
        raw = client().request_bytes("GET", url, headers={"Accept": "image/*"})
        return reduce_image(raw)
    except (BadImage, ConnectorError):
        return None


def process_reference(db: Session, job: Job) -> dict:
    global _cooldown_until
    gtin14 = str((job.payload or {}).get("gtin14") or "")
    product = db.scalar(select(Product).where(Product.gtin14 == gtin14)) if gtin14 else None
    if not product:
        return {"status": "unsupported", "detail": "No product has that barcode"}
    if time.monotonic() < _cooldown_until:       # asked to slow down: do not send even one more
        return {"status": "rate_limited", "detail": "Waiting after the reference database asked us to slow down"}
    try:
        found = lookup(gtin14)
        picture = fetch_picture(found.image_url) if found and found.image_url else None
    except RateLimited as exc:
        _cooldown_until = time.monotonic() + (exc.retry_after or 600)
        return {"status": "rate_limited", "detail": "The reference database asked us to slow down"}
    except AccessBlocked as exc:
        _cooldown_until = time.monotonic() + 6 * 3600
        return {"status": "access_blocked", "detail": str(exc)}
    except ExtractionError as exc:
        found, picture = None, None
        log.warning("reference lookup unreadable gtin=%s: %s", gtin14, exc)
    except TransientError:
        raise                                   # the worker retries with a delay
    now = utcnow()
    row = db.get(ReferenceProduct, gtin14)
    if row is None:
        row = ReferenceProduct(gtin14=gtin14, status="not_found")
        db.add(row)
    row.fetched_at = now
    if found is None:
        row.status = "not_found"
        row.next_check_at = now + timedelta(days=settings.reference_recheck_days_missing)
        return {"status": "success", "found": False}
    row.status, row.source = "found", found.site
    row.name, row.brand, row.quantity_text = found.name, found.brand, found.quantity
    row.page_url, row.image_url, row.details = found.page_url, found.image_url, found.details
    row.next_check_at = now + timedelta(days=settings.reference_recheck_days_found)
    if picture:
        for old in db.scalars(select(ProductImage).where(ProductImage.product_id == product.id,
                                                         ProductImage.source == "reference")):
            db.delete(old)
        data, width, height = picture
        db.add(ProductImage(product_id=product.id, source="reference", content_type="image/webp",
                            data=data, width=width, height=height, source_url=found.image_url,
                            credit=CREDIT.format(site=found.site)))
    return {"status": "success", "found": True, "picture": bool(picture)}


def schedule(db: Session, limit: int = 2) -> int:
    """Queue lookups for barcodes the reference has not been asked about (or is due again).
    Capped per day and paused after a refusal, so it stays a light, polite background task."""
    if not settings.reference_enabled or time.monotonic() < _cooldown_until:
        return 0
    since = utcnow() - timedelta(days=1)
    today = db.scalar(select(func.count()).select_from(Job).where(
        Job.kind == "reference_lookup", Job.created_at >= since))
    in_flight_count = db.scalar(select(func.count()).select_from(Job).where(
        Job.kind == "reference_lookup", Job.state.in_(["queued", "running"])))
    # a few at a time: upkeep must never build a backlog
    room = min(limit - (in_flight_count or 0), settings.reference_max_lookups_per_day - (today or 0))
    if room <= 0:
        return 0
    in_flight = {(job.payload or {}).get("gtin14") for job in db.scalars(select(Job).where(
        Job.kind == "reference_lookup", Job.state.in_(["queued", "running"])))}
    due = db.scalars(select(Product.gtin14).outerjoin(
        ReferenceProduct, ReferenceProduct.gtin14 == Product.gtin14).where(
        Product.gtin14.is_not(None),
        or_(ReferenceProduct.gtin14.is_(None), ReferenceProduct.next_check_at <= utcnow()))
        .order_by(Product.id).limit(room + len(in_flight)))
    queued = 0
    for gtin14 in due:
        if gtin14 in in_flight or queued >= room:
            continue
        db.add(Job(kind="reference_lookup", payload={"gtin14": gtin14}))
        queued += 1
    return queued


# ---- reading it back ------------------------------------------------------------------------

def best_image(db: Session, product_id: str | None, user_id: str | None) -> dict | None:
    """The picture to show: the person's own first, then the store's, then the reference's."""
    if not product_id:
        return None
    rank = {"user": 0, "store": 1, "reference": 2}
    rows = db.execute(select(ProductImage.id, ProductImage.source, ProductImage.credit,
                             ProductImage.source_url, ProductImage.owner_id)
                      .where(ProductImage.product_id == product_id,
                             or_(ProductImage.owner_id.is_(None), ProductImage.owner_id == user_id))
                      .order_by(ProductImage.created_at.desc())).all()
    if not rows:
        return None
    best = min(rows, key=lambda r: rank.get(r.source, 9))
    return {"url": f"/images/{best.id}", "source": best.source, "credit": best.credit}


def mismatches(ours: Described, ref_brand: str | None, ref_quantity: str | None) -> list[dict]:
    """Where the reference and the store disagree. Only things both sides state are compared."""
    flags = []
    found = size_in_text(ref_quantity or "")
    if ours.size and found and found[0] != ours.size:
        flags.append({"field": "size", "ours": ours.size_text,
                      "reference": ref_quantity})
    if ours.brand_key and ref_brand:
        ref_words = {w for part in ref_brand.split(",") for w in tokens(brand_key(part) or "")}
        mine = set(ours.brand_key.split())
        if ref_words and not any(a in b or b in a for a in mine for b in ref_words):
            flags.append({"field": "brand", "ours": ours.brand, "reference": ref_brand})
    return flags


def reference_info(db: Session, gtin14: str | None, ours: Described | None) -> dict | None:
    row = db.get(ReferenceProduct, gtin14) if gtin14 else None
    if row is None or row.status != "found":
        return None
    return {"source": row.source, "name": row.name, "brand": row.brand, "quantity": row.quantity_text,
            "url": row.page_url, "details": row.details or {},
            "flags": mismatches(ours, row.brand, row.quantity_text) if ours else []}

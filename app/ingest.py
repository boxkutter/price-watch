"""Turning connector items into catalogue rows, shared by monitor scans and search jobs."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import PriceData
from app.gtin import normalize_gtin
from app.models import Listing, PriceObservation, Product, Store
from app.pricing import unit_price


def _product_for(db: Session, item: PriceData) -> Product:
    """Match on a retailer-published GTIN (valid check digit) before creating a product.

    A GTIN the retailer prints on its own product record is source-stated, so it is stored as a
    verified mapping; the same item at another store then resolves to the same product. Items
    without a GTIN stay separate provisional products and are never merged by name.
    """
    gtin = None
    if item.gtin:
        try:
            gtin = normalize_gtin(item.gtin)
        except ValueError:
            gtin = None
    if gtin and gtin[1:].startswith("2"):
        # EAN-13 prefix 2 is "restricted circulation": store-internal codes for loose or
        # weighed items, not global product numbers. Matching on them would link unrelated products.
        gtin = None
    if gtin:
        existing = db.scalar(select(Product).where(Product.gtin14 == gtin))
        if existing:
            return existing
    product = Product(name=item.name, brand=item.brand, gtin14=gtin, verified=gtin is not None,
                      package_value=item.package_value, package_unit=item.package_unit)
    db.add(product)
    db.flush()
    return product


def ensure_listing(db: Session, store: Store, item: PriceData) -> Listing:
    listing = db.scalar(select(Listing).where(
        Listing.retailer_id == store.retailer_id, Listing.sku == item.sku,
        Listing.store_id == store.id, Listing.channel == item.channel))
    if listing:
        return listing
    product = _product_for(db, item)
    listing = Listing(retailer_id=store.retailer_id, sku=item.sku, store_id=store.id,
                      channel=item.channel, product_id=product.id, name=item.name)
    db.add(listing)
    db.flush()
    return listing


def add_price(db: Session, listing: Listing, item: PriceData, run_id: str,
              observed_at) -> tuple | None:
    """Append an observation; returns the computed (unit price, unit) or None if unknown."""
    unit = unit_price(item.current_price, item.package_value, item.package_unit)
    db.add(PriceObservation(
        listing_id=listing.id, run_id=run_id, current_price=item.current_price,
        regular_price=item.regular_price, currency=item.currency,
        unit_price=unit[0] if unit else None, unit_price_unit=unit[1] if unit else None,
        availability=item.availability, source_url=item.source_url, observed_at=observed_at))
    return unit

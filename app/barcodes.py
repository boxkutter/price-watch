"""Barcodes: validating them, linking one to a product, and merging duplicates of the same item.

A barcode links a product across stores and retailers, so it is shared by everyone using the
catalogue. Where it came from is recorded (`gtin_source`): a barcode a retailer publishes cannot be
overridden by a person, a barcode a person scanned can be corrected or removed, and two products
are only ever merged when someone asks for it explicitly.
"""
import re

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.gtin import normalize_gtin
from app.models import Listing, Product, ProductAlias, ProductImage, utcnow


class BarcodeError(ValueError):
    """The barcode itself is unusable; the message is safe to show."""


class BarcodeConflict(Exception):
    """The change is not allowed (for example, overriding a retailer's barcode)."""


class BarcodeTaken(Exception):
    """The barcode already belongs to another product."""

    def __init__(self, product: Product):
        super().__init__(product.name)
        self.product = product


def is_restricted(gtin14: str) -> bool:
    """Store-internal codes: EAN-13 prefixes 02x and 2xx are 'restricted circulation', used for
    loose or weighed items. They are not product barcodes, so they cannot identify an item in
    another store (or even in the same one, next week)."""
    if not gtin14.startswith("0"):
        return False  # a real GTIN-14 (a carton), not an item code
    ean13 = gtin14[1:]
    return ean13.startswith(("2", "02"))


def clean_barcode(text: str) -> str:
    """The barcode as a normalised GTIN-14. Accepts EAN-8, UPC-A, EAN-13 and GTIN-14, with
    spaces or hyphens as printed. Raises BarcodeError with a plain explanation."""
    digits = re.sub(r"[\s\-]", "", text or "")
    try:
        gtin14 = normalize_gtin(digits)
    except ValueError as exc:
        raise BarcodeError(f"{exc}. Check the digits under the barcode.") from exc
    if is_restricted(gtin14):
        raise BarcodeError("That is a store-internal code (it starts with 2), used for loose or "
                           "weighed items. It is not a product barcode, so it cannot identify "
                           "the item in other stores.")
    return gtin14


def search_form(gtin14: str, given: str | None = None) -> str:
    """The digits to send to a retailer: as scanned if that was a valid form, else EAN-13."""
    digits = re.sub(r"[\s\-]", "", given or "")
    try:
        if digits and normalize_gtin(digits) == gtin14:
            return digits
    except ValueError:
        pass
    return gtin14.removeprefix("0")


def product_for_gtin(db: Session, gtin14: str) -> Product | None:
    return db.scalar(select(Product).where(Product.gtin14 == gtin14))


def product_summary(db: Session, product: Product) -> dict:
    listings = db.scalars(select(Listing).where(Listing.product_id == product.id)).all()
    return {"id": product.id, "name": product.name, "brand": product.brand,
            "barcode": ({"gtin14": product.gtin14, "source": product.gtin_source,
                         "set_at": product.gtin_set_at} if product.gtin14 else None),
            "retailers": sorted({listing.retailer_id for listing in listings}),
            "listings": len(listings)}


def set_barcode(db: Session, product: Product, gtin14: str, source: str,
                user_id: str | None) -> str:
    """Link a barcode to a product. Returns "set", "replaced" or "unchanged"."""
    if product.gtin14 == gtin14:
        return "unchanged"
    if product.gtin14 and product.gtin_source == "retailer":
        raise BarcodeConflict("This product's barcode is published by the store and cannot be "
                              "changed.")
    other = db.scalar(select(Product).where(Product.gtin14 == gtin14, Product.id != product.id))
    if other:
        raise BarcodeTaken(other)
    replaced = bool(product.gtin14)
    product.gtin14, product.verified = gtin14, True
    product.gtin_source, product.gtin_set_by, product.gtin_set_at = source, user_id, utcnow()
    db.flush()
    return "replaced" if replaced else "set"


def clear_barcode(db: Session, product: Product) -> None:
    if not product.gtin14:
        return
    if product.gtin_source == "retailer":
        raise BarcodeConflict("This product's barcode is published by the store and cannot be "
                              "removed.")
    product.gtin14, product.verified = None, False
    product.gtin_source = product.gtin_set_by = product.gtin_set_at = None
    db.flush()


def merge_products(db: Session, source: Product, target: Product) -> int:
    """Treat `source` as the same item as `target`: its listings move to `target` and the empty
    product is deleted. Price history is attached to listings, so nothing is lost. Returns the
    number of listings moved."""
    if source.id == target.id:
        return 0
    if source.gtin14 and source.gtin14 != target.gtin14:
        raise BarcodeConflict("This product already has a different barcode; remove it first.")
    moved = db.execute(update(Listing).where(Listing.product_id == source.id)
                       .values(product_id=target.id)).rowcount
    _move_attachments(db, source, target)
    if target.package_value is None and source.package_value is not None:
        target.package_value, target.package_unit = source.package_value, source.package_unit
    db.flush()
    db.expire(source)
    db.delete(source)
    db.flush()
    return moved


def _move_attachments(db: Session, source: Product, target: Product) -> None:
    """What hangs off a product follows the merge: a person's own pictures and the names people
    gave it move to the target (a name the target already has is dropped); the source's shared
    reference picture is dropped because the target has its own."""
    db.execute(delete(ProductImage).where(ProductImage.product_id == source.id,
                                          ProductImage.owner_id.is_(None)))
    db.execute(update(ProductImage).where(ProductImage.product_id == source.id)
               .values(product_id=target.id))
    have = set(db.scalars(select(ProductAlias.folded).where(ProductAlias.product_id == target.id)))
    for alias in db.scalars(select(ProductAlias).where(ProductAlias.product_id == source.id)):
        if alias.folded in have:
            db.delete(alias)
        else:
            alias.product_id = target.id
            have.add(alias.folded)

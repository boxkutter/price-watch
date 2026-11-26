"""Every name a product goes by, and what that makes possible.

A product has the names its store listings carry (each shop words it its own way), the name the
reference database gives it, and the alternative names people added. All of them are "also called"
names. They let a typed search find a product a shop calls something else, and they feed the
"probably the same" check that suggests two results are one item.
"""
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.glossary import canonical_tokens, fold
from app.models import Listing, Product, ProductAlias, ReferenceProduct, Store
from app.naming import Described, Query, describe

MAX_ALIAS_LENGTH = 120


@dataclass(frozen=True)
class Name:
    name: str
    source: str                   # store | reference | user
    id: str | None = None         # the alias id, for a user's own names
    retailer_id: str | None = None


def _dedupe(names: list[Name]) -> list[Name]:
    seen, out = set(), []
    for item in names:
        key = " ".join(canonical_tokens(item.name))
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def product_names(db: Session, product: Product) -> list[Name]:
    """All of a product's names, store listings first, without repeats."""
    names = [Name(row.name, "store", retailer_id=row.retailer_id) for row in db.scalars(
        select(Listing).where(Listing.product_id == product.id).order_by(Listing.retailer_id))]
    ref = db.get(ReferenceProduct, product.gtin14) if product.gtin14 else None
    if ref is not None and ref.status == "found" and ref.name:
        names.append(Name(ref.name, "reference"))
    names += [Name(row.name, "user", id=row.id) for row in db.scalars(
        select(ProductAlias).where(ProductAlias.product_id == product.id)
        .order_by(ProductAlias.created_at))]
    return _dedupe(names)


def also_called(db: Session, product: Product, own_name: str, limit: int = 6) -> list[dict]:
    """The names other than the one being shown, for the 'also called' line."""
    own = " ".join(canonical_tokens(own_name))
    shown = [n for n in product_names(db, product) if " ".join(canonical_tokens(n.name)) != own]
    return [{"name": n.name, "source": n.source, "id": n.id} for n in shown[:limit]]


def add_alias(db: Session, product: Product, name: str, user_id: str | None) -> ProductAlias | None:
    """Add a name. None when the product already goes by it."""
    clean = " ".join(name.split())
    folded = fold(clean)
    if not folded:
        return None
    if any(" ".join(canonical_tokens(n.name)) == " ".join(canonical_tokens(clean))
           for n in product_names(db, product)):
        return None
    row = ProductAlias(product_id=product.id, name=clean[:MAX_ALIAS_LENGTH], folded=folded[:200],
                       created_by=user_id)
    db.add(row)
    db.flush()
    return row


def name_cores(db: Session, product: Product) -> list[frozenset[str]]:
    """The core words of every name of a product (brand and size taken out of each)."""
    return [describe(n.name, product.brand, product.package_value, product.package_unit).core
            for n in product_names(db, product)]


def matching_skus(db: Session, store: Store, typed: Query, exclude: set[str],
                  cap: int = 5) -> list[str]:
    """Listings in this store whose product has *another* name that matches what was typed, though
    the store's own wording does not (so the store's search would not have found them). Lets an
    alternative name find the product in a shop that calls it something else."""
    mine = db.execute(select(Listing.product_id, Listing.sku, Listing.name).where(
        Listing.store_id == store.id, Listing.channel == "online",
        Listing.product_id.is_not(None))).all()
    if not mine:
        return []
    wanted = {row.product_id for row in mine}
    others: dict[str, list[str]] = defaultdict(list)
    for row in db.execute(select(Listing.product_id, Listing.name).where(
            Listing.product_id.in_(wanted), Listing.store_id != store.id)):
        others[row.product_id].append(row.name)
    for row in db.execute(select(ProductAlias.product_id, ProductAlias.name).where(
            ProductAlias.product_id.in_(wanted))):
        others[row.product_id].append(row.name)
    for row in db.execute(select(Product.id, ReferenceProduct.name).join(
            ReferenceProduct, ReferenceProduct.gtin14 == Product.gtin14).where(
            Product.id.in_(wanted), ReferenceProduct.status == "found")):
        if row.name:
            others[row.id].append(row.name)
    brands = dict(db.execute(select(Product.id, Product.brand).where(Product.id.in_(wanted))).all())
    found = []
    for row in mine:
        if row.sku in exclude or not others.get(row.product_id):
            continue
        brand = brands.get(row.product_id)
        if typed.accepts(row.name, brand, None):
            continue                              # the store's own search would have found it
        if any(typed.accepts(name, brand, None) for name in others[row.product_id]):
            found.append(row.sku)
        if len(found) >= cap:
            break
    return found


# ---- "probably the same" ---------------------------------------------------------------------

def _same_word(x: str, y: str) -> bool:
    """Equal, or one is a clipped form of the other (hazeln / hazelnut)."""
    return x == y or (min(len(x), len(y)) >= 4 and (x.startswith(y) or y.startswith(x)))


def similarity(a: frozenset[str], b: frozenset[str]) -> tuple[float, list[str]]:
    """How alike two sets of name words are (0 to 1) and the words only one of them has."""
    hit_a = {x for x in a if any(_same_word(x, y) for y in b)}
    hit_b = {y for y in b if any(_same_word(x, y) for x in a)}
    differs = sorted((a - hit_a) | (b - hit_b))
    if not hit_a:
        return 0.0, differs
    jaccard = len(hit_a) / (len(a) + len(b) - len(hit_b))
    # one name's words all appear in the other, which only adds a word or two
    contained = (len(hit_a) == len(a) and len(b - hit_b) <= 2) or (len(hit_b) == len(b) and len(a - hit_a) <= 2)
    return (max(jaccard, 0.75) if contained else jaccard), differs


def likely_same(items: list[dict], cores: dict[str, list[frozenset[str]]],
                described: dict[str, Described], limit: int = 3, threshold: float = 0.5) -> None:
    """For each result that has no barcode, the other results that are probably the same item (same
    brand, size and pack, and names that mostly agree), with what differs. Only a suggestion: a
    person confirms it. A result that has a barcode is not offered anything; the ones without one
    are offered it."""
    for item in items:
        item["likely_same"] = []
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for item in items:
        parts = described[item["listing_id"]]
        if parts.brand_key and parts.size:
            groups[(parts.brand_key, parts.size, parts.pack)].append(item)
    for group in groups.values():
        for item in group:
            if item["barcode"]:
                continue
            found: dict[str, dict] = {}
            for other in group:
                if other["product_id"] == item["product_id"]:
                    continue
                pairs = [similarity(a, b) for a in cores.get(item["product_id"], [])
                         for b in cores.get(other["product_id"], [])]
                score, differs = max(pairs, key=lambda s: (s[0], -len(s[1])), default=(0.0, []))
                if score >= threshold and score > found.get(other["product_id"], {}).get("score", 0):
                    found[other["product_id"]] = {
                        "listing_id": other["listing_id"], "product_id": other["product_id"],
                        "store_id": other["store_id"], "name": other["display_name"],
                        "barcode": other["barcode"]["gtin14"] if other["barcode"] else None,
                        "differs": differs[:6], "score": round(score, 2)}
            item["likely_same"] = sorted(found.values(), key=lambda c: -c["score"])[:limit]

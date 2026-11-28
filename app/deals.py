"""A person's best price for a product: the shelf price, or an offer they can use.

"Cheapest" means cheapest for *this person*: a multi-buy ("2 för 35 kr" is 17,50 kr each), a
percentage off, and, when they are a member of the store, its member prices and offers. Monitors,
search results and comparisons all rank by this, never by the plain shelf price alone.
"""
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Offer, UserMembership, aware, utcnow
from app.multibuy import Best, best_price, parse_offer_price

# An offer is kept in the catalogue until a scan stops seeing it; to rank by it, it must have been
# seen lately, or a monitor's checks would have refreshed it.
RECENT = timedelta(hours=48)


def memberships(db: Session, user_id: str | None) -> set[str]:
    """The chains this person is a member of (retailer ids)."""
    if not user_id:
        return set()
    return set(db.scalars(select(UserMembership.retailer_id).where(UserMembership.user_id == user_id)))


def is_member(db: Session, user_id: str | None, retailer_id: str) -> bool:
    """Whether this person said they are a member of the chain's loyalty scheme."""
    return bool(user_id) and db.get(UserMembership, (user_id, retailer_id)) is not None


def recorded_offers(db: Session, store_id: str, channel: str, sku: str,
                    seen_since=None) -> list[Offer]:
    """The offers the catalogue has on one product at one store that still apply: not past their
    end date, and seen lately (or since [seen_since])."""
    pattern = sku.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ":%"
    now = utcnow()
    fresh_after = aware(seen_since) - timedelta(minutes=5) if seen_since else now - RECENT
    rows = db.scalars(select(Offer).where(
        Offer.store_id == store_id, Offer.channel == channel, Offer.status == "active",
        Offer.identity_key.like(pattern, escape="\\"))).all()
    return [o for o in rows if not (o.valid_to and aware(o.valid_to) < now)
            and aware(o.last_seen) >= fresh_after]


def best_for(db: Session, user_id: str | None, retailer_id: str, single_price: Decimal | None,
             offers) -> tuple[Best, object] | None:
    """The best price this person can get among [offers] (objects with `terms`, `eligibility`), and
    the offer it comes from."""
    if single_price is None:
        return None
    member = is_member(db, user_id, retailer_id)
    by_terms = {}
    for offer in offers:
        by_terms.setdefault((offer.terms, offer.eligibility), offer)
    best = best_price(single_price, list(by_terms), member)
    return (best, by_terms[(best.terms, "member" if best.member else "public")]) if best else None


def best_dict(found: tuple[Best, object] | None) -> dict | None:
    """The best price as clients see it. `quantity` is how many to buy to get it (1 for a member
    price or a percentage off)."""
    if not found:
        return None
    best, offer = found
    valid_to = getattr(offer, "valid_to", None)
    return {"unit_price": str(best.unit), "quantity": best.quantity, "terms": best.terms,
            "member": best.member, "valid_to": valid_to.isoformat() if valid_to else None}


def deal_fields(terms: str | None, single_price) -> dict:
    """What one offer costs per item, when its wording says: a multi-buy, a member price, a percentage."""
    deal = parse_offer_price(terms, single_price)
    return ({"quantity": deal.quantity, "unit_price": str(deal.unit)} if deal
            else {"quantity": None, "unit_price": None})

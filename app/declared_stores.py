"""Stores that connectors declare in their manifest (`stores:`), created without discovery.

A retailer with one national online assortment has nothing to discover, so its manifest names the
store. This makes sure a row exists for each (never deleting or renaming away anything a person
has monitored), on start-up, when a package is installed, and when the worker notices a change.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors import registry
from app.models import Store


def seed(db: Session) -> int:
    """Create missing declared stores and refresh their names. Returns how many were created."""
    created = 0
    for retailer in registry.retailers():
        for spec in retailer.get("stores") or []:
            row = db.scalar(select(Store).where(Store.retailer_id == retailer["id"],
                                                Store.external_id == spec["external_id"]))
            if row is None:
                db.add(Store(retailer_id=retailer["id"], external_id=spec["external_id"],
                             name=spec["name"]))
                created += 1
            elif row.name != spec["name"]:
                row.name = spec["name"]
    return created

"""Store directory refresh, run by the worker so only one process contacts retailers."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import connector_admin, connector_state
from app.connectors.base import AccessBlocked, AuthRequired, ExtractionError, RateLimited
from app.connectors.registry import connector_for
from app.models import Job, Store
from app.retrieval import retailer_info


def process_discovery(db: Session, job: Job) -> dict:
    retailer_id = (job.payload or {}).get("retailer_id", "")
    connector = connector_for(retailer_id)
    if not connector or not hasattr(connector, "discover_stores"):
        return {"status": "unsupported", "detail": "Connector has no store directory"}
    refused = connector_state.refusal(db, retailer_id,
                                      retailer_info(retailer_id).get("max_requests_per_day", 10**9))
    if refused:
        return {"status": refused[0], "detail": refused[1]}
    before = getattr(getattr(connector, "client", None), "requests_made", 0)
    inputs = (job.payload or {}).get("inputs") or {}
    connector_admin.apply_config(db, retailer_id, connector)
    try:
        stores = connector.discover_stores(**inputs)
    except AuthRequired as exc:
        return {"status": "auth_required", "detail": str(exc)}
    except AccessBlocked as exc:
        connector_state.pause(db, retailer_id, f"Access blocked by the retailer ({exc}). "
                                               "Review before resuming.")
        return {"status": "access_blocked", "detail": str(exc)}
    except RateLimited as exc:
        connector_state.start_cooldown(db, retailer_id, exc.retry_after)
        return {"status": "rate_limited", "detail": "Retailer asked us to slow down"}
    except ExtractionError as exc:
        # Discovery is operator-triggered with operator-supplied inputs, so a failure may be a
        # mistyped input rather than a layout change; it does not count towards pausing.
        return {"status": "extraction_failed", "detail": str(exc)}
    finally:
        made = getattr(getattr(connector, "client", None), "requests_made", 0) - before
        connector_state.add_requests(db, retailer_id, made)
    created = updated = 0
    for info in stores:
        row = db.scalar(select(Store).where(Store.retailer_id == retailer_id,
                                            Store.external_id == info.external_id))
        if row:
            updated += row.name != info.name
            row.name = info.name
        else:
            db.add(Store(retailer_id=retailer_id, external_id=info.external_id, name=info.name))
            created += 1
    connector_state.record_success(db, retailer_id)
    return {"status": "success", "stores": len(stores), "created": created, "updated": updated}

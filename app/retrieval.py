"""The single gate every retailer scan passes through.

Applies, in order: pause (a retailer refused us), cooldown (a 429 with Retry-After), the daily
request budget, a short in-process cache (so two monitors on the same product or search do not
fetch twice), then accounts for the requests made and reacts to blocks. Only the worker calls
this; it is the one process that talks to retailers.
"""
import time

from sqlalchemy.orm import Session

from app import connector_state
from app.connectors.base import ScanRequest, Snapshot
from app.connectors.registry import RETAILERS
from app.models import Store

_CACHE: dict[tuple, tuple[float, Snapshot]] = {}
_CACHE_LIMIT = 256


def retailer_info(retailer_id: str) -> dict:
    return next((item for item in RETAILERS if item["id"] == retailer_id), {})


def fetch(db: Session, store: Store, connector, channel: str,
          request: ScanRequest) -> tuple[Snapshot | None, dict | None]:
    """Returns (snapshot, None) or (None, result) when the scan must not run right now."""
    info = retailer_info(store.retailer_id)
    refused = connector_state.refusal(db, store.retailer_id, info.get("max_requests_per_day", 10**9))
    if refused:
        return None, {"status": refused[0], "complete": False, "detail": refused[1]}
    ttl = info.get("cache_seconds", 0)
    key = (id(connector), store.external_id, channel, request)
    if ttl:
        hit = _CACHE.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1], None
    snapshot = connector.scan(store.external_id, channel, request)
    connector_state.add_requests(db, store.retailer_id, snapshot.requests)
    status = snapshot.effective_status
    if status == "access_blocked":
        connector_state.pause(db, store.retailer_id,
                              f"Access blocked by the retailer ({snapshot.detail}). "
                              "Review before resuming; the client is not disguised or retried.")
    elif status == "rate_limited":
        connector_state.start_cooldown(db, store.retailer_id, snapshot.retry_after)
    elif status in ("extraction_failed", "invalid_data"):
        connector_state.record_failure(db, store.retailer_id)
    elif status in ("success", "partial", "not_found"):
        connector_state.record_success(db, store.retailer_id)
    if status == "success" and ttl:
        if len(_CACHE) >= _CACHE_LIMIT:
            _CACHE.clear()
        _CACHE[key] = (time.monotonic(), snapshot)
    return snapshot, None


def clear_cache() -> None:
    _CACHE.clear()

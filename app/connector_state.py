"""Shared operational state for retailer connectors: pause, cooldown and request budget.

Lives in the database so the API and worker agree, and so a restart cannot forget that a
retailer refused us. Only the worker should make retailer requests; the API only reads this.
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import ConnectorState, aware, utcnow

FAILURE_PAUSE_THRESHOLD = 5      # consecutive extraction failures before pausing for review
DEFAULT_COOLDOWN_SECONDS = 900   # when a 429 carries no Retry-After
MIN_COOLDOWN_SECONDS = 60
WINDOW = timedelta(hours=24)


def get_state(db: Session, retailer_id: str) -> ConnectorState:
    state = db.get(ConnectorState, retailer_id)
    if not state:
        state = ConnectorState(retailer_id=retailer_id, state="active", consecutive_failures=0,
                               requests_in_window=0)
        db.add(state)
        db.flush()
    return state


def refusal(db: Session, retailer_id: str, daily_limit: int,
            now: datetime | None = None) -> tuple[str, str] | None:
    """(status, detail) if a request must not be made right now, else None."""
    now = now or utcnow()
    state = get_state(db, retailer_id)
    if state.state == "paused":
        return "paused", state.reason or "Paused for review"
    if state.cooldown_until and aware(state.cooldown_until) > now:
        return "rate_limited", f"Cooling down until {aware(state.cooldown_until).isoformat()}"
    in_window = state.window_start and now - aware(state.window_start) < WINDOW
    if in_window and state.requests_in_window >= daily_limit:
        return "rate_limited", f"Daily request budget of {daily_limit} reached"
    return None


def add_requests(db: Session, retailer_id: str, count: int, now: datetime | None = None) -> None:
    if count <= 0:
        return
    now = now or utcnow()
    state = get_state(db, retailer_id)
    if not state.window_start or now - aware(state.window_start) >= WINDOW:
        state.window_start, state.requests_in_window = now, 0
    state.requests_in_window += count


def record_success(db: Session, retailer_id: str) -> None:
    state = get_state(db, retailer_id)
    state.consecutive_failures = 0
    state.cooldown_until = None


def record_failure(db: Session, retailer_id: str) -> None:
    """An extraction/layout failure. Repeated ones pause the connector for review."""
    state = get_state(db, retailer_id)
    state.consecutive_failures += 1
    if state.consecutive_failures >= FAILURE_PAUSE_THRESHOLD and state.state != "paused":
        pause(db, retailer_id, f"{state.consecutive_failures} consecutive extraction failures")


def start_cooldown(db: Session, retailer_id: str, retry_after: int | None,
                   now: datetime | None = None) -> None:
    seconds = max(retry_after or DEFAULT_COOLDOWN_SECONDS, MIN_COOLDOWN_SECONDS)
    get_state(db, retailer_id).cooldown_until = (now or utcnow()) + timedelta(seconds=seconds)


def pause(db: Session, retailer_id: str, reason: str) -> None:
    state = get_state(db, retailer_id)
    state.state, state.reason, state.updated_at = "paused", reason[:500], utcnow()


def resume(db: Session, retailer_id: str) -> None:
    state = get_state(db, retailer_id)
    state.state, state.reason, state.consecutive_failures = "active", None, 0
    state.cooldown_until, state.updated_at = None, utcnow()

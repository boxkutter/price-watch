import logging
import time
from datetime import timedelta

from sqlalchemy import select

from app import declared_stores
from app.compare import process_compare
from app.connectors import registry
from app.db import SessionLocal
from app.discovery import process_discovery
from app.models import Job, Monitor, Store, utcnow
from app.monitoring import next_due, process_scan
from app.products import process_add_product
from app.search import process_search
from app.webhooks import due_delivery, send_delivery

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def schedule_due() -> int:
    """Queue a scan for every monitor that is due. Returns how many were queued.

    A connector can state when scheduled visits are welcome (`scheduled_window_utc`). A monitor
    whose store is closed right now simply stays due and is picked up when the window opens.
    """
    with SessionLocal.begin() as db:
        due = db.scalars(select(Monitor).where(Monitor.enabled.is_(True),
            Monitor.next_due_at <= utcnow()).order_by(Monitor.next_due_at)
            .limit(20).with_for_update(skip_locked=True)).all()
        queued = 0
        for monitor in due:
            store = db.get(Store, monitor.store_id)
            if store and not registry.in_scheduled_window(store.retailer_id):
                continue
            db.add(Job(monitor_id=monitor.id, kind="scan"))
            monitor.next_due_at = next_due(monitor.interval_minutes)
            queued += 1
        return queued


def claim_job(db):
    # LIMIT 1 matters on PostgreSQL: without it FOR UPDATE locks every matching row.
    return db.scalar(select(Job).where(
        ((Job.state == "queued") | ((Job.state == "running") & (Job.lease_until < utcnow()))),
        Job.run_after <= utcnow()).order_by(Job.created_at)
        .limit(1).with_for_update(skip_locked=True))


def run_job() -> bool:
    with SessionLocal.begin() as db:
        job = claim_job(db)
        if not job:
            return False
        job.state = "running"
        job.attempts += 1
        job.lease_until = utcnow() + timedelta(minutes=5)
        job_id = job.id
    try:
        with SessionLocal.begin() as db:
            job = db.get(Job, job_id, with_for_update=True)
            if job is None:  # deleted while queued (its monitor was permanently removed)
                return True
            if job.kind == "search":
                result = process_search(db, job)
            elif job.kind == "discover_stores":
                result = process_discovery(db, job)
            elif job.kind == "add_product":
                result = process_add_product(db, job)
            elif job.kind == "compare":
                result = process_compare(db, job)
            else:
                result = process_scan(db, job)
            job.result = result
            job.state = "succeeded" if result["status"] == "success" else result["status"]
            job.lease_until = None
    except Exception:
        log.exception("scan failed job=%s", job_id)
        with SessionLocal.begin() as db:
            job = db.get(Job, job_id, with_for_update=True)
            if job is None:
                return True
            if job.attempts >= 5:
                job.state = "failed"
            else:
                job.state = "queued"
                job.run_after = utcnow() + timedelta(seconds=min(600, 15 * 2 ** job.attempts))
            job.lease_until = None
    return True


def run_delivery() -> bool:
    with SessionLocal.begin() as db:
        delivery = due_delivery(db)
        if not delivery:
            return False
        send_delivery(db, delivery)
    return True


def main() -> None:
    while True:
        try:
            if registry.refresh():  # pick up connectors installed or removed through the API
                with SessionLocal.begin() as db:
                    declared_stores.seed(db)
            schedule_due()
            did_work = run_job()
            did_work = run_delivery() or did_work
            if not did_work:
                time.sleep(2)
        except Exception:
            log.exception("worker loop error")
            time.sleep(5)


if __name__ == "__main__":
    main()

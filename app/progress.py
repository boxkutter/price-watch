"""Saying what a running job is doing, for clients that show it ("Searching City Gross Höör").

A job runs inside one database transaction that is committed only at the end, so progress is written
on its own connection, straight away. It is a courtesy, never a requirement: if it cannot be
written (or the database is SQLite, which cannot take a second writer while a job holds its lock)
it is skipped, and the job carries on.
"""
import logging
from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert

from app.db import engine
from app.models import JobProgress, utcnow

log = logging.getLogger(__name__)


def report(job_id: str, message: str, done: int = 0, total: int = 0) -> None:
    if engine.dialect.name != "postgresql":
        return
    values = {"job_id": job_id, "text": message[:200], "done": done, "total": total, "updated_at": utcnow()}
    try:
        with engine.begin() as conn:
            conn.execute(insert(JobProgress).values(**values).on_conflict_do_update(
                index_elements=["job_id"], set_={k: v for k, v in values.items() if k != "job_id"}))
    except Exception:       # progress is best-effort: it must never stop a job
        log.debug("could not write progress for job %s", job_id, exc_info=True)


def purge(older_than: timedelta = timedelta(hours=2)) -> int:
    """Forget the progress of jobs that finished long ago."""
    with engine.begin() as conn:
        return conn.execute(delete(JobProgress).where(JobProgress.updated_at < utcnow() - older_than)).rowcount


__all__ = ["purge", "report"]

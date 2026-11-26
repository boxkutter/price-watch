"""A running search says which store it is asking, for a client that shows progress."""
from datetime import timedelta

from app import progress
from app.db import SessionLocal, engine
from app.models import Job, JobProgress, utcnow
from tests.test_politeness import AXA, job_of, search
from tests.test_regressions import admin_client


def test_a_search_reports_each_store_as_it_goes(cg, monkeypatch):
    seen = []
    monkeypatch.setattr(progress, "report", lambda job_id, text, done=0, total=0: seen.append((text, done, total)))
    search(cg, query="havregryn")
    assert seen == [("Asking the stores...", 0, 1), ("Searching City Gross Borås", 0, 1),
                    ("Putting the results together", 1, 1)]
    seen.clear()
    job_of(cg, cg.client.post("/v1/search/jobs", json={"gtin": AXA, "store_ids": [cg.store_id]}))
    assert ("Looking up the barcode in City Gross Borås", 0, 1) in seen


def test_progress_is_shown_for_a_running_job_only(fresh_db):
    admin = admin_client()
    with SessionLocal.begin() as db:
        job = Job(owner_id=admin.get("/v1/me").json()["id"], kind="search", state="running", payload={})
        db.add(job)
        db.flush()
        job_id = job.id
    progress.report(job_id, "Searching City Gross Höör", 2, 5)
    shown = admin.get(f"/v1/jobs/{job_id}").json()["progress"]
    if engine.dialect.name == "postgresql":        # SQLite cannot take a second writer; it skips progress
        assert shown == {"text": "Searching City Gross Höör", "done": 2, "total": 5}
    else:
        assert shown is None
    with SessionLocal.begin() as db:
        db.get(Job, job_id).state = "succeeded"
    assert admin.get(f"/v1/jobs/{job_id}").json()["progress"] is None


def test_old_progress_notes_are_forgotten_and_a_failed_write_never_stops_a_job(fresh_db, monkeypatch):
    if engine.dialect.name == "postgresql":
        progress.report("old-job", "Searching", 1, 2)
        with SessionLocal.begin() as db:
            db.get(JobProgress, "old-job").updated_at = utcnow() - timedelta(hours=5)
        assert progress.purge() >= 1
    monkeypatch.setattr(progress.engine, "begin", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(progress.engine.dialect, "name", "postgresql", raising=False)
    progress.report("job", "Searching", 0, 1)         # does not raise

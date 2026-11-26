import os
import tempfile

import pytest

# Must run before any `app` import so settings and the engine pick these up.
# Set TEST_DATABASE_URL to run the suite against PostgreSQL instead of SQLite.
TEST_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_URL"] = (os.environ.get("TEST_DATABASE_URL")
                              or "sqlite:///" + TEST_DIR.name + "/retail.db")
os.environ["APP_SECRET"] = "0123456789abcdef0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = "0123456789abcdef"
# Hermetic: environment variables beat .env, so pin everything a developer's own .env could change.
os.environ["ADMIN_EMAIL"] = "admin@example.invalid"
os.environ["PUBLIC_BASE_URL"] = "http://localhost:8085"
os.environ["ALLOW_HTTP_WEBHOOKS"] = "false"
os.environ["WEBHOOK_PRIVATE_HOSTS"] = ""
os.environ["INSTALLED_CONNECTORS_DIR"] = os.path.join(TEST_DIR.name, "connectors")
os.environ["MAX_MONITORS_PER_USER"] = "50"
os.environ["MAX_MY_STORES"] = "15"
os.environ["MAX_COMPARE_STORES"] = "8"
os.environ["MAX_PACKAGE_BYTES"] = "5000000"
os.environ["CONNECTOR_USER_AGENT"] = "Mozilla/5.0 (X11; Linux x86_64) test-agent"
# Limits are exercised explicitly in tests/test_politeness.py; relax them for everything else.
os.environ["API_RATE_LIMIT_PER_MINUTE"] = "100000"
os.environ["MIN_MANUAL_CHECK_INTERVAL_SECONDS"] = "0"
os.environ["MAX_SEARCHES_PER_HOUR"] = "1000"
os.environ["MAX_PENDING_SEARCHES_PER_USER"] = "1000"


from app import connector_admin

connector_admin._dotenv = dict  # never read a developer's .env for connector settings


@pytest.fixture
def fresh_db():
    from app.bootstrap import main as bootstrap
    from app.db import Base, engine

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    bootstrap()
    yield


@pytest.fixture
def cg(fresh_db, monkeypatch):
    """A simulated City Gross wired into the registry, a store row, and an admin API client."""
    from types import SimpleNamespace

    from app import retrieval
    from app.connectors.registry import CONNECTORS
    from app.db import SessionLocal
    from app.models import Store
    from tests.test_citygross import FakeSite, connector
    from tests.test_regressions import admin_client

    retrieval.clear_cache()
    site = FakeSite()
    monkeypatch.setitem(CONNECTORS, "se.citygross", connector(site))
    with SessionLocal.begin() as db:
        store = Store(retailer_id="se.citygross", external_id="3204", name="City Gross Borås")
        db.add(store)
        db.flush()
        store_id = store.id
    return SimpleNamespace(site=site, client=admin_client(), store_id=store_id)

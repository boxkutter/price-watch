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


@pytest.fixture
def fresh_db():
    from app.bootstrap import main as bootstrap
    from app.db import Base, engine

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    bootstrap()
    yield

"""The suite must never depend on a developer's own .env."""
import os

from app.config import Settings


def test_every_setting_is_pinned_by_conftest_so_a_local_env_file_cannot_change_the_tests():
    unpinned = [name for name in Settings.model_fields if name.upper() not in os.environ]
    assert unpinned == [], (
        f"Pin {[n.upper() for n in unpinned]} in tests/conftest.py: environment variables beat .env, "
        "so an unpinned setting lets a developer's .env change test behaviour")

"""Startup refuses placeholder or weak settings, and says exactly which and why."""
from types import SimpleNamespace

import pytest

from app.bootstrap import config_problems, main

GOOD = {"app_secret": "x" * 40, "admin_password": "y" * 20, "database_url": "postgresql://u:p@db/x"}


def config(**changes):
    return SimpleNamespace(**{**GOOD, **changes})


def test_good_settings_have_no_problems():
    assert config_problems(config()) == []


@pytest.mark.parametrize("changes,expected", [
    ({"app_secret": "replace-with-at-least-32-random-characters"}, "APP_SECRET is still the example"),
    ({"app_secret": "short"}, "APP_SECRET is too short"),
    ({"admin_password": "replace-with-a-long-random-password"}, "ADMIN_PASSWORD is still the example"),
    ({"admin_password": "tooshort"}, "ADMIN_PASSWORD is too short"),
    ({"database_url": "postgresql://retail:change-me@postgres/retail"}, "DATABASE_URL still contains"),
])
def test_each_problem_is_named(changes, expected):
    (problem,) = config_problems(config(**changes))
    assert expected in problem


def test_all_problems_are_reported_together_and_values_are_never_printed(monkeypatch):
    bad = config(app_secret="replace-me", admin_password="short",
                 database_url="postgresql://retail:change-me@postgres/retail")
    monkeypatch.setattr("app.bootstrap.settings", bad)
    with pytest.raises(SystemExit) as exit_info:
        main()
    message = str(exit_info.value)
    assert message.count("\n  - ") == 3 and message.startswith("Cannot start: fix these settings in .env")
    assert "replace-me" not in message and "short" not in message.replace("too short", "")

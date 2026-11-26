from pathlib import Path

import pytest

from app.connectors.registry import load_registry


def test_fourth_connector_loads_from_manifest_without_core_changes(tmp_path: Path):
    (tmp_path / "fourth.yaml").write_text("""
id: test.fourth
name: Fourth fixture
country: SE
protocol_version: 1
connector_version: 0.1.0
enabled: true
access_review: approved
implementation: tests.fixture_fourth:FourthConnector
capabilities:
  store_offers: supported
""")
    retailers, connectors = load_registry(tmp_path)
    assert retailers[0]["id"] == "test.fourth"
    assert connectors["test.fourth"].scan("demo", "online").complete

    (tmp_path / "fourth.yaml").write_text((tmp_path / "fourth.yaml").read_text().replace(
        "access_review: approved", "access_review: unverified"))
    with pytest.raises(ValueError, match="access review"):
        load_registry(tmp_path)

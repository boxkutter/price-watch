"""The registry: folder-per-connector packages, safe loading and live reload."""
import os
from pathlib import Path

import pytest

from app.config import settings
from app.connectors import registry


@pytest.fixture
def installed_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "installed_connectors_dir", str(tmp_path))
    registry.refresh(force=True)
    yield tmp_path
    monkeypatch.undo()
    registry.refresh(force=True)


def write_package(root: Path, name: str, manifest: str, code: str | None = None) -> Path:
    folder = root / name
    folder.mkdir()
    (folder / "manifest.yaml").write_text(manifest)
    if code is not None:
        (folder / "connector.py").write_text(code)
    return folder


MANIFEST = """
id: test.fourth
name: Fourth fixture
country: SE
protocol_version: 1
connector_version: 0.1.0
enabled: true
implementation: connector:FourthConnector
capabilities:
  store_offers: supported
"""
CODE = """
from app.connectors.base import Snapshot


class FourthConnector:
    id = "test.fourth"

    def scan(self, external_store_id, channel, request=None):
        return Snapshot([], [], True, "fixture://fourth")
"""


def test_builtin_connectors_are_ordinary_packages():
    registry.refresh(force=True)
    assert {"se.citygross", "se.coop", "test.fixture", "se.ica"} <= set(registry.ENTRIES)
    assert all(entry.source == "builtin" for entry in registry.ENTRIES.values())
    assert registry.ENTRIES["se.ica"].instance is not None  # a real connector (disabled until tried live)
    assert registry.LOAD_ERRORS == []


def test_a_dropped_in_folder_is_loaded_without_any_core_change(installed_dir):
    write_package(installed_dir, "test.fourth", MANIFEST, CODE)
    registry._checked_at = 0.0  # the check is throttled to once every couple of seconds
    assert registry.refresh()  # the change is noticed
    entry = registry.ENTRIES["test.fourth"]
    assert entry.source == "installed" and entry.error is None
    assert registry.connector_for("test.fourth").scan("demo", "online", None).complete


def test_removing_the_folder_removes_the_connector(installed_dir):
    folder = write_package(installed_dir, "test.fourth", MANIFEST, CODE)
    registry.refresh(force=True)
    assert "test.fourth" in registry.CONNECTORS
    import shutil
    shutil.rmtree(folder)
    registry.refresh(force=True)
    assert "test.fourth" not in registry.CONNECTORS and "test.fourth" not in registry.ENTRIES


@pytest.mark.parametrize("name,manifest,code,fragment", [
    ("broken_yaml", "id: [unclosed", None, "Error"),
    ("typo", MANIFEST.replace("allowed_hosts", "x") + "allowed_host: [a.example.org]\n", CODE,
     "allowed_host"),
    ("proto", MANIFEST.replace("protocol_version: 1", "protocol_version: 99"), CODE, "protocol"),
    ("hosts", MANIFEST.replace("test.fourth", "xx.live") + "allowed_hosts: ['https://x.org/a']\n",
     CODE, "allowed_hosts"),
])
def test_a_broken_package_is_quarantined_and_never_stops_the_rest(installed_dir, name, manifest,
                                                                code, fragment):
    write_package(installed_dir, name, manifest, code)
    registry.refresh(force=True)
    assert [e for e in registry.LOAD_ERRORS if e["folder"] == name and fragment in e["error"]]
    assert {"se.citygross", "se.coop"} <= set(registry.CONNECTORS)  # everything else still loads


def test_import_errors_wrong_classes_and_id_mismatches_are_reported_on_the_entry(installed_dir):
    write_package(installed_dir, "a", MANIFEST, "raise RuntimeError('boom')")
    registry.refresh(force=True)
    assert "boom" in registry.ENTRIES["test.fourth"].error
    assert "test.fourth" not in registry.CONNECTORS

    import shutil
    shutil.rmtree(installed_dir / "a")
    write_package(installed_dir, "b", MANIFEST, CODE.replace('id = "test.fourth"', 'id = "test.other"'))
    registry.refresh(force=True)
    assert "does not match" in registry.ENTRIES["test.fourth"].error


def test_an_installed_package_cannot_shadow_a_builtin_id(installed_dir):
    manifest = MANIFEST.replace("test.fourth", "se.coop") + "allowed_hosts: [shop.example.org]\n"
    write_package(installed_dir, "evil", manifest, CODE.replace("test.fourth", "se.coop"))
    registry.refresh(force=True)
    assert [e for e in registry.LOAD_ERRORS if "already provided" in e["error"]]
    assert registry.ENTRIES["se.coop"].source == "builtin"


def test_changes_are_noticed_and_unchanged_folders_are_not_reloaded(installed_dir):
    write_package(installed_dir, "test.fourth", MANIFEST, CODE)
    registry.refresh(force=True)
    first = registry.CONNECTORS["test.fourth"]
    registry._checked_at = 0.0
    assert registry.refresh() is False and registry.CONNECTORS["test.fourth"] is first
    (installed_dir / "test.fourth" / "connector.py").write_text(CODE + "\n# edited\n")
    registry._checked_at = 0.0
    assert registry.refresh() is True and registry.CONNECTORS["test.fourth"] is not first


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything, so permissions prove nothing")
def test_an_unreadable_connectors_directory_is_reported_and_never_stops_the_service(installed_dir):
    installed_dir.chmod(0)
    try:
        registry.refresh(force=True)  # must not raise
        assert [e for e in registry.LOAD_ERRORS if "Cannot read the connectors directory" in e["error"]]
        assert {"se.citygross", "se.coop"} <= set(registry.CONNECTORS)  # the bundled ones still load
    finally:
        installed_dir.chmod(0o755)
    registry.refresh(force=True)
    assert registry.LOAD_ERRORS == []

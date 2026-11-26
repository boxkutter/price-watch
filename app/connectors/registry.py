"""Connector registry: finds connector packages, loads each safely, and reloads on change.

A connector package is a folder holding `manifest.yaml` and the module its `implementation`
names. Packages come from two places, with exactly the same structure:

- built-in: `app/connectors/builtin/<name>/`, shipped with the service
- installed: `<CONNECTORS_DIR>/<id>/`, put there by the admin interface

A package that fails to load (bad manifest, import error, wrong class) is quarantined and shown
to the administrator; it never stops the service or the other connectors. The API and the worker
are separate processes, so each notices changes on disk by itself (cheap, throttled) and no
restart is needed after an install.

Whether a connector is *enabled* is not decided here: the manifest only gives the default, and
the administrator's override lives in the database (see app/connector_admin.py).
"""
import hashlib
import importlib
import importlib.util
import inspect
import sys
import threading
import time
import types
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.connectors.manifest import Manifest, load_manifest

BUILTIN_DIR = Path(__file__).parent / "builtin"
CHECK_INTERVAL_SECONDS = 2.0


@dataclass
class Entry:
    manifest: Manifest
    source: str              # "builtin" | "installed"
    path: Path
    instance: object | None = None
    error: str | None = None
    sha256: str | None = None

    @property
    def id(self) -> str:
        return self.manifest.id


# Live containers, updated in place so existing references (and tests) keep working.
RETAILERS: list[dict] = []
CONNECTORS: dict[str, object] = {}
ENTRIES: dict[str, Entry] = {}
LOAD_ERRORS: list[dict] = []

_lock = threading.RLock()
_managed: set[str] = set()
_fingerprint: tuple | None = None
_checked_at = 0.0


def connector_dirs() -> list[tuple[Path, str]]:
    dirs = [(BUILTIN_DIR, "builtin")]
    installed = Path(settings.connectors_dir)
    if installed.is_dir():
        dirs.append((installed, "installed"))
    return dirs


def package_folders(directory: Path) -> list[Path]:
    return sorted(path for path in directory.iterdir()
                  if path.is_dir() and not path.name.startswith((".", "_"))
                  and (path / "manifest.yaml").exists())


def package_files(folder: Path) -> list[Path]:
    return sorted(path for path in folder.rglob("*")
                  if path.is_file() and "__pycache__" not in path.parts
                  and path.suffix in {".py", ".yaml", ".yml", ".json"})


def folder_hash(folder: Path) -> str:
    digest = hashlib.sha256()
    for path in package_files(folder):
        digest.update(str(path.relative_to(folder)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _scan_fingerprint(dirs: list[tuple[Path, str]]) -> tuple:
    parts: list = []
    for directory, source in dirs:
        generation = directory / ".generation"
        parts.append((source, generation.stat().st_mtime_ns if generation.exists() else 0))
        for folder in package_folders(directory):
            parts.extend((str(path), path.stat().st_mtime_ns, path.stat().st_size)
                         for path in package_files(folder))
    return tuple(parts)


def _load_class(folder: Path, source: str, manifest: Manifest, sha: str):
    module_name, _, class_name = manifest.implementation.partition(":")
    if "." not in module_name and (folder / f"{module_name}.py").exists():
        if source == "builtin":
            module = importlib.import_module(f"app.connectors.builtin.{folder.name}.{module_name}")
        else:
            # A uniquely named synthetic package per content hash, so an updated package is
            # imported afresh and sibling imports (`from . import helpers`) work.
            package = f"_connector_{manifest.id.replace('.', '_').replace('-', '_')}_{sha[:12]}"
            if package not in sys.modules:
                holder = types.ModuleType(package)
                holder.__path__ = [str(folder)]
                sys.modules[package] = holder
            module = importlib.import_module(f"{package}.{module_name}")
    else:
        module = importlib.import_module(module_name)
    factory = getattr(module, class_name, None)
    if factory is None or not callable(getattr(factory, "scan", None)):
        raise ImportError(f"{manifest.implementation} is not a connector class with scan()")
    return factory


def _load_folder(folder: Path, source: str) -> Entry:
    manifest = load_manifest(folder / "manifest.yaml")
    entry = Entry(manifest, source, folder, sha256=folder_hash(folder))
    if manifest.implementation:
        try:
            factory = _load_class(folder, source, manifest, entry.sha256)
            takes_manifest = "manifest" in inspect.signature(factory).parameters
            instance = factory(manifest=manifest.model_dump()) if takes_manifest else factory()
            if instance.id != manifest.id:
                raise ValueError(f"connector id {instance.id!r} does not match manifest {manifest.id!r}")
            entry.instance = instance
        except Exception as exc:  # noqa: BLE001  quarantine anything a package can raise
            entry.error = f"{type(exc).__name__}: {exc}"[:500]
    return entry


def retailer_dict(entry: Entry) -> dict:
    manifest = entry.manifest
    return {"id": manifest.id, "name": manifest.name, "country": manifest.country,
            "description": manifest.description, "author": manifest.author,
            "source": entry.source, "installable": bool(manifest.implementation),
            "load_error": entry.error, "sha256": entry.sha256,
            "enabled": manifest.enabled,  # the manifest default; the runtime state is in the database
            "access_review": manifest.access_review, "access_note": manifest.access_note,
            "capabilities": manifest.capabilities, "connector_version": manifest.connector_version,
            "settings": [spec.model_dump() for spec in manifest.settings],
            "store_discovery_inputs": [spec.model_dump() for spec in manifest.store_discovery_inputs],
            "allowed_hosts": manifest.allowed_hosts,
            "minimum_monitor_interval_minutes": manifest.minimum_monitor_interval_minutes,
            "max_requests_per_day": manifest.max_requests_per_day,
            "cache_seconds": manifest.cache_seconds}


def _load_all(dirs: list[tuple[Path, str]]) -> None:
    entries: dict[str, Entry] = {}
    errors: list[dict] = []
    for directory, source in dirs:
        for folder in package_folders(directory):
            try:
                entry = _load_folder(folder, source)
            except Exception as exc:  # noqa: BLE001
                errors.append({"folder": folder.name, "source": source,
                               "error": f"{type(exc).__name__}: {exc}"[:500]})
                continue
            if entry.id in entries:
                errors.append({"folder": folder.name, "source": source, "id": entry.id,
                               "error": f"id {entry.id} is already provided by "
                                        f"{entries[entry.id].source} connector '{entries[entry.id].path.name}'"})
                continue
            entries[entry.id] = entry
    ENTRIES.clear()
    ENTRIES.update(entries)
    RETAILERS[:] = [retailer_dict(entry) for entry in entries.values()]
    LOAD_ERRORS[:] = errors
    for retailer_id in _managed - set(entries):
        CONNECTORS.pop(retailer_id, None)
    _managed.clear()
    for entry in entries.values():
        if entry.instance is not None:
            CONNECTORS[entry.id] = entry.instance
            _managed.add(entry.id)
        else:
            CONNECTORS.pop(entry.id, None)


def refresh(force: bool = False) -> bool:
    """Reload if any connector folder changed. Throttled; returns True when it reloaded."""
    global _fingerprint, _checked_at
    with _lock:
        now = time.monotonic()
        if not force and now - _checked_at < CHECK_INTERVAL_SECONDS:
            return False
        _checked_at = now
        dirs = connector_dirs()
        fingerprint = _scan_fingerprint(dirs)
        if not force and fingerprint == _fingerprint:
            return False
        _fingerprint = fingerprint
        _load_all(dirs)
        return True


def retailers() -> list[dict]:
    refresh()
    return RETAILERS


def retailer(retailer_id: str) -> dict | None:
    refresh()
    return next((item for item in RETAILERS if item["id"] == retailer_id), None)


def entry(retailer_id: str) -> Entry | None:
    refresh()
    return ENTRIES.get(retailer_id)


def connector_for(retailer_id: str):
    refresh()
    return CONNECTORS.get(retailer_id)


def load_errors() -> list[dict]:
    refresh()
    return LOAD_ERRORS


refresh(force=True)

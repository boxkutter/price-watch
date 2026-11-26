"""Connector packages: validate an uploaded zip, stage it for preview, install and remove it.

A package is a zip of a connector folder: `manifest.yaml`, the module the manifest's
`implementation` names, and optionally helper modules, fixtures, tests and a README.

Nothing is executed until the package is installed. `inspect_zip` only reads it: it rejects unsafe
archives (path traversal, symlinks, encrypted or oversized entries, unexpected file types),
validates the manifest, and statically checks that the named class exists. Installation is atomic
and is rolled back automatically if the new package fails to load.

Connector code runs inside the service with its privileges. Installing a package is therefore an
administrator trusting its author, exactly like installing any plugin. The host allowlist and the
politeness limits constrain the standard HTTP client; they are not a sandbox.
"""
import ast
import hashlib
import importlib.util
import io
import os
import re
import shutil
import stat
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from app.config import settings
from app.connectors import registry
from app.connectors.manifest import Manifest, parse_manifest

ALLOWED_SUFFIXES = {".py", ".yaml", ".yml", ".json", ".md", ".txt"}
MAX_FILES = 200
MAX_COMPRESSION_RATIO = 200
STAGING_TTL_SECONDS = 3600
_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_RISKY_IMPORTS = {"subprocess", "ctypes", "socket", "multiprocessing", "pickle", "marshal"}
_RISKY_CALLS = {"eval", "exec", "compile", "__import__"}


class PackageError(ValueError):
    """The package is unacceptable; the message is safe to show to the administrator."""


@dataclass
class PackageInfo:
    manifest: Manifest
    files: dict[str, bytes]              # path inside the package -> content
    sha256: str
    warnings: list[str] = field(default_factory=list)
    replaces: dict | None = None         # the installed version this would replace

    def summary(self) -> dict:
        manifest = self.manifest
        return {"id": manifest.id, "name": manifest.name, "version": manifest.connector_version,
                "description": manifest.description, "author": manifest.author,
                "country": manifest.country, "allowed_hosts": manifest.allowed_hosts,
                "capabilities": manifest.capabilities,
                "settings": [spec.name for spec in manifest.settings],
                "default_enabled": manifest.enabled, "sha256": self.sha256,
                "files": [{"path": path, "bytes": len(content)}
                          for path, content in sorted(self.files.items())],
                "warnings": self.warnings, "replaces": self.replaces}


def installed_dir() -> Path:
    path = Path(settings.connectors_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_path(name: str) -> str:
    if (not name or len(name) > 200 or "\\" in name or "\x00" in name or name.startswith("/")
            or re.match(r"^[A-Za-z]:", name)):
        raise PackageError(f"Unsafe file name in package: {name!r}")
    parts = PurePosixPath(name).parts
    if any(part in ("..", ".") or part.startswith(".") for part in parts):
        raise PackageError(f"Unsafe file name in package: {name!r}")
    return "/".join(parts)


def _read_entries(data: bytes) -> dict[str, bytes]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PackageError("The upload is not a valid zip file") from exc
    entries: dict[str, bytes] = {}
    total = 0
    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if not infos:
            raise PackageError("The zip file is empty")
        if len(infos) > MAX_FILES:
            raise PackageError(f"The package has too many files (limit {MAX_FILES})")
        for info in infos:
            path = _safe_path(info.filename)
            if stat.S_ISLNK(info.external_attr >> 16):
                raise PackageError(f"Symbolic links are not allowed: {path}")
            if info.flag_bits & 0x1:
                raise PackageError(f"Encrypted entries are not allowed: {path}")
            if Path(path).suffix.lower() not in ALLOWED_SUFFIXES:
                raise PackageError(f"File type not allowed in a connector package: {path}")
            total += info.file_size
            if total > settings.max_package_bytes:
                raise PackageError(f"The package is too large (limit {settings.max_package_bytes} bytes)")
            if info.file_size > 100_000 and info.file_size / max(info.compress_size, 1) > MAX_COMPRESSION_RATIO:
                raise PackageError(f"Suspicious compression ratio: {path}")
            if path in entries:
                raise PackageError(f"Duplicate entry in package: {path}")
            entries[path] = archive.read(info)
    return entries


def _strip_root(entries: dict[str, bytes]) -> dict[str, bytes]:
    """Accept both a zip of the folder's contents and a zip containing the folder itself."""
    if "manifest.yaml" in entries:
        return entries
    roots = {path.split("/", 1)[0] for path in entries if "/" in path}
    if len(roots) == 1 and all(path.startswith(next(iter(roots)) + "/") for path in entries):
        root = next(iter(roots)) + "/"
        return {path[len(root):]: content for path, content in entries.items()}
    return entries


def _static_checks(manifest: Manifest, files: dict[str, bytes]) -> list[str]:
    module_name, _, class_name = manifest.implementation.partition(":")
    if "." in module_name:
        raise PackageError("A package's implementation must name a module inside the package, "
                           "for example 'connector:MyConnector'")
    source_path = f"{module_name}.py"
    if source_path not in files:
        raise PackageError(f"manifest.yaml names '{manifest.implementation}' but "
                           f"{source_path} is not in the package")
    warnings: list[str] = []
    siblings = {Path(path).stem for path in files if path.endswith(".py")}
    for path, content in sorted(files.items()):
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(content.decode("utf-8"), filename=path)
        except (SyntaxError, UnicodeDecodeError) as exc:
            raise PackageError(f"{path} does not parse: {exc}") from exc
        if path == source_path and not any(
                isinstance(node, ast.ClassDef) and node.name == class_name for node in tree.body):
            raise PackageError(f"{source_path} defines no top-level class named {class_name}")
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in _RISKY_IMPORTS:
                    warnings.append(f"{path} imports '{name}', which can reach beyond HTTP requests")
                elif name not in siblings and importlib.util.find_spec(name) is None:
                    warnings.append(f"{path} imports '{name}', which is not installed in this service")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _RISKY_CALLS:
                warnings.append(f"{path} calls {node.func.id}()")
    return sorted(set(warnings))


def inspect_zip(data: bytes) -> PackageInfo:
    """Validate an uploaded package. Reads only; executes nothing."""
    files = _strip_root(_read_entries(data))
    if "manifest.yaml" not in files:
        raise PackageError("manifest.yaml is missing from the package")
    try:
        manifest = parse_manifest(files["manifest.yaml"].decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        detail = " ".join(str(exc).split())[:400]
        raise PackageError(f"manifest.yaml is invalid: {detail}") from exc
    if not manifest.implementation:
        raise PackageError("manifest.yaml has no implementation; there is nothing to install")
    builtin = registry.ENTRIES.get(manifest.id)
    if builtin and builtin.source == "builtin":
        raise PackageError(f"'{manifest.id}' is a built-in connector and cannot be replaced")
    warnings = _static_checks(manifest, files)
    digest = hashlib.sha256()
    for path, content in sorted(files.items()):
        digest.update(path.encode() + b"\0" + content + b"\0")
    registry.refresh()
    current = registry.ENTRIES.get(manifest.id)
    replaces = ({"version": current.manifest.connector_version, "sha256": current.sha256}
                if current and current.source == "installed" else None)
    return PackageInfo(manifest, files, digest.hexdigest(), warnings, replaces)


# ---- staging: upload, preview, then install ---------------------------------------------

def _staging() -> Path:
    path = installed_dir() / ".staging"
    path.mkdir(exist_ok=True)
    return path


def stage(data: bytes) -> tuple[str, PackageInfo]:
    info = inspect_zip(data)
    directory = _staging()
    for old in directory.glob("*.zip"):  # expire abandoned uploads
        if time.time() - old.stat().st_mtime > STAGING_TTL_SECONDS:
            old.unlink(missing_ok=True)
    token = uuid.uuid4().hex
    path = directory / f"{token}.zip"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return token, info


def _staged_path(token: str) -> Path:
    if not _TOKEN.match(token or ""):
        raise PackageError("Unknown upload")
    path = _staging() / f"{token}.zip"
    if not path.exists() or time.time() - path.stat().st_mtime > STAGING_TTL_SECONDS:
        raise PackageError("The upload has expired; upload the package again")
    return path


def _touch_generation(directory: Path) -> None:
    (directory / ".generation").write_text(uuid.uuid4().hex)


def install_staged(token: str) -> PackageInfo:
    """Install a previewed upload. Returns the info; raises PackageError (and leaves the
    previous state untouched) if the package cannot be loaded."""
    staged = _staged_path(token)
    info = inspect_zip(staged.read_bytes())  # re-validate: never trust the staged copy blindly
    base = installed_dir()
    folder, scratch, backup = base / info.manifest.id, base / f".tmp-{token}", base / f".old-{token}"
    target = scratch / info.manifest.id
    for path, content in info.files.items():
        destination = (target / path).resolve()
        if not destination.is_relative_to(target.resolve()):
            raise PackageError(f"Unsafe file name in package: {path!r}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    had_previous = folder.exists()
    try:
        if had_previous:
            folder.rename(backup)
        target.rename(folder)
        _touch_generation(base)
        registry.refresh(force=True)
        loaded = registry.ENTRIES.get(info.manifest.id)
        problem = (loaded.error if loaded else None) or next(
            (item["error"] for item in registry.LOAD_ERRORS if item.get("folder") == folder.name), None)
        if loaded is None or problem or loaded.source != "installed":
            raise PackageError(f"The package failed to load: {problem or 'not recognised'}")
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        if had_previous and backup.exists():
            backup.rename(folder)
        _touch_generation(base)
        registry.refresh(force=True)
        raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    shutil.rmtree(backup, ignore_errors=True)
    staged.unlink(missing_ok=True)
    return info


def uninstall(retailer_id: str) -> None:
    entry = registry.entry(retailer_id)
    if entry is None or entry.source != "installed":
        raise PackageError("Only connectors installed through the admin interface can be removed")
    folder = entry.path.resolve()
    if not folder.is_relative_to(installed_dir().resolve()) or folder == installed_dir().resolve():
        raise PackageError("Refusing to remove a folder outside the installed connectors directory")
    shutil.rmtree(folder, ignore_errors=True)
    _touch_generation(installed_dir())
    registry.refresh(force=True)

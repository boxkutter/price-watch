"""Build an uploadable connector package from a folder.

    python -m app.connectors.build path/to/connector-folder [-o out.zip]
    python -m app.connectors.build --schema          # JSON Schema for manifest.yaml

The zip is validated with exactly the checks the admin upload applies, so problems show up here.
"""
import argparse
import io
import json
import sys
import zipfile
from pathlib import Path

from app.connectors.manifest import Manifest
from app.connectors.packages import ALLOWED_SUFFIXES, PackageError, inspect_zip


def build(folder: Path) -> bytes:
    if not (folder / "manifest.yaml").exists():
        raise PackageError(f"{folder} has no manifest.yaml")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            relative = path.relative_to(folder)
            if (path.is_file() and "__pycache__" not in relative.parts
                    and not any(part.startswith(".") for part in relative.parts)
                    and path.suffix.lower() in ALLOWED_SUFFIXES):
                archive.write(path, str(relative))
    return buffer.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path, nargs="?")
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--schema", action="store_true",
                        help="print the JSON Schema of manifest.yaml and exit")
    args = parser.parse_args(argv)
    if args.schema:
        print(json.dumps(Manifest.model_json_schema(), indent=2))
        return 0
    if args.folder is None:
        parser.error("a connector folder is required (or use --schema)")
    try:
        data = build(args.folder)
        info = inspect_zip(data)
    except PackageError as exc:
        print(f"Package is not valid: {exc}", file=sys.stderr)
        return 1
    output = args.output or Path(f"{info.manifest.id}-{info.manifest.connector_version}.zip")
    output.write_bytes(data)
    print(f"Built {output} ({len(data)} bytes, sha256 {info.sha256[:16]}...)")
    for warning in info.warnings:
        print(f"  warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

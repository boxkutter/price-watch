"""The connector documentation cannot drift from the code or ship incomplete."""
import json
import re
from pathlib import Path

from app.connectors.build import build
from app.connectors.manifest import Manifest
from app.connectors.packages import inspect_zip

DOCS = Path(__file__).parent.parent / "docs"
TEMPLATE = DOCS / "connector-template"


def test_the_committed_manifest_schema_matches_the_code():
    committed = json.loads((TEMPLATE / "manifest.schema.json").read_text())
    assert committed == Manifest.model_json_schema(), (
        "regenerate it: python -m app.connectors.build --schema > "
        "docs/connector-template/manifest.schema.json")


def test_every_manifest_key_is_documented_in_the_reference_and_the_template():
    reference = (DOCS / "connectors.md").read_text()
    template = (TEMPLATE / "manifest.yaml").read_text()
    for key in Manifest.model_fields:
        assert f"`{key}`" in reference, f"docs/connectors.md does not document `{key}`"
        assert re.search(rf"^\s*{key}:|^#?\s*{key}\b", template, re.MULTILINE) or key in (
            "protocol_version",), f"the template manifest never mentions {key}"


def test_the_instructions_travel_inside_every_package_built_from_the_template():
    files = inspect_zip(build(TEMPLATE)).files
    assert {"AGENTS.md", "README.md", "manifest.schema.json", "test_connector.py"} <= set(files)


def test_relative_links_in_the_connector_docs_resolve():
    for page in (DOCS / "connectors.md", DOCS / "connector-guide.md", TEMPLATE / "README.md"):
        for target in re.findall(r"\]\((?!https?:|#)([^)#]+)", page.read_text()):
            assert (page.parent / target).exists(), f"{page.name} links to missing {target}"


def test_the_guide_covers_what_a_new_author_needs():
    guide = (DOCS / "connector-guide.md").read_text()
    for needle in ("robots.txt", "access_blocked", "Definition of done", "fixtures", "short page",
                   "store number", "Never get around a refusal", "RUN_LIVE"):
        assert needle in guide or needle.lower() in guide.lower(), needle
    agents = (TEMPLATE / "AGENTS.md").read_text()
    for needle in ("Hard rules", "Definition of done", "Stop and ask the human"):
        assert needle in agents, needle

"""The documented author workflow works: the template's own tests pass, and its package installs."""
import subprocess
import sys
from pathlib import Path

TEMPLATE = Path(__file__).parent.parent / "connectors" / "_template"


def test_the_template_connectors_own_tests_pass_as_documented():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(TEMPLATE)],
        cwd=TEMPLATE.parent.parent, check=False, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_build_command_produces_a_valid_package_and_rejects_a_bad_folder(tmp_path):
    out = tmp_path / "pkg.zip"
    built = subprocess.run([sys.executable, "-m", "app.connectors.build", str(TEMPLATE), "-o", str(out)],
                           cwd=TEMPLATE.parent.parent, check=False, capture_output=True, text=True)
    assert built.returncode == 0 and out.exists(), built.stderr
    empty = tmp_path / "empty"
    empty.mkdir()
    bad = subprocess.run([sys.executable, "-m", "app.connectors.build", str(empty)],
                         cwd=TEMPLATE.parent.parent, check=False, capture_output=True, text=True)
    assert bad.returncode == 1 and "no manifest.yaml" in bad.stderr

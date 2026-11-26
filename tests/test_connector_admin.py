"""Admin control of connectors: enable/disable, settings, and installing uploaded packages."""
import io
import json
import os
import stat
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import retrieval
from app.config import settings
from app.connectors import registry
from app.connectors.build import build
from app.db import SessionLocal
from app.main import app
from app.models import AuditLog, ConnectorSetting, Store
from tests.test_coop import FakeCoop
from tests.test_coop import connector as coop_connector
from tests.test_politeness import check, job_of, monitor, search
from tests.test_regressions import admin_client

TEMPLATE = Path(__file__).parent.parent / "connectors" / "_template"
ZIP = {"Content-Type": "application/zip"}


@pytest.fixture
def lab(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "installed_connectors_dir", str(tmp_path / "connectors"))
    monkeypatch.delenv("COOP_API_KEY", raising=False)
    registry.refresh(force=True)
    retrieval.clear_cache()
    yield SimpleNamespace(client=admin_client(), dir=tmp_path / "connectors")
    monkeypatch.undo()
    registry.refresh(force=True)


def zipped(files: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def template_files(**changes) -> dict[str, str]:
    files = {name: (TEMPLATE / name).read_text() for name in ("manifest.yaml", "connector.py")}
    files.update(changes)
    return files


def upload(lab, data: bytes):
    return lab.client.post("/v1/admin/connector-packages/inspect", content=data, headers=ZIP)


def install(lab, data: bytes):
    preview = upload(lab, data)
    assert preview.status_code == 201, preview.text
    return lab.client.post(f"/v1/admin/connector-packages/{preview.json()['upload_id']}/install")


def listing(lab) -> dict:
    return {item["id"]: item for item in lab.client.get("/v1/admin/connectors").json()["items"]}


# ---- the admin view --------------------------------------------------------------------

def test_the_admin_view_shows_every_connector_with_an_honest_status(lab):
    items = listing(lab)
    assert {"se.citygross", "se.coop", "test.fixture", "se.ica"} <= set(items)
    assert items["se.citygross"]["status"] == "ready" and items["se.citygross"]["source"] == "builtin"
    assert items["se.coop"]["status"] == "needs_configuration"  # its key is not set
    assert items["se.coop"]["settings"][0]["name"] == "COOP_API_KEY"
    assert items["se.coop"]["settings"][0]["configured"] is False
    assert items["se.ica"]["status"] == "not_installable" and items["se.ica"]["enabled"] is False
    assert lab.client.get("/v1/admin/connectors").json()["load_errors"] == []


def test_connector_administration_is_admin_only(lab):
    user = TestClient(app)
    assert user.get("/v1/admin/connectors").status_code == 401
    token = lab.client.post("/v1/admin/invitations", json={"email": "u@example.invalid"}).json()
    user.post("/v1/auth/accept-invitation", json={"token": token["invitation_token"],
                                                  "password": "a-long-user-password"})
    user.headers["Authorization"] = "Bearer " + user.post("/v1/auth/login", json={
        "email": "u@example.invalid", "password": "a-long-user-password"}).json()["access_token"]
    for method, path in (("get", "/v1/admin/connectors"),
                         ("post", "/v1/admin/connectors/se.coop/enable"),
                         ("put", "/v1/admin/connectors/se.coop/settings/COOP_API_KEY"),
                         ("post", "/v1/admin/connector-packages/inspect"),
                         ("delete", "/v1/admin/connector-packages/se.coop")):
        assert getattr(user, method)(path).status_code == 403, path
    assert user.get("/v1/connectors").status_code == 200  # the plain listing is for everyone


# ---- enable / disable ------------------------------------------------------------------

def test_disabling_a_connector_stops_all_requests_and_new_monitors_and_enabling_restores_it(cg):
    mid = monitor(cg, "generic_product", query="havregryn").json()["id"]
    assert check(cg, mid)["state"] == "succeeded"
    calls = len(cg.site.calls)
    assert cg.client.post("/v1/admin/connectors/se.citygross/disable").status_code == 204
    health = cg.client.get("/v1/connectors/se.citygross/health").json()
    assert health["enabled"] is False and health["status"] == "disabled"
    assert check(cg, mid)["state"] == "disabled" and len(cg.site.calls) == calls
    assert search(cg, query="havregryn")["result"]["coverage"][0]["status"] == "disabled"
    refused = monitor(cg, "generic_product", query="mjölk")
    assert refused.status_code == 422 and "disabled" in refused.json()["message"]
    assert cg.client.post("/v1/admin/retailers/se.citygross/stores/discover").status_code == 404
    # its stores disappear for users (list, per-retailer list and "my stores") and return with it
    assert cg.store_id not in [s["id"] for s in cg.client.get("/v1/stores").json()["items"]]
    assert cg.client.get("/v1/retailers/se.citygross/stores").json()["items"] == []
    assert cg.client.get("/v1/me/stores").json()["items"] == []
    shown = {i["id"]: i for i in cg.client.get("/v1/admin/connectors").json()["items"]}
    assert shown["se.citygross"]["overridden"] and shown["se.citygross"]["default_enabled"]
    assert next(i for i in cg.client.get("/v1/connectors").json()["items"]
                if i["id"] == "se.citygross")["enabled"] is False

    assert cg.client.post("/v1/admin/connectors/se.citygross/enable").status_code == 204
    assert check(cg, mid)["state"] == "succeeded"


def test_only_a_loaded_connector_can_be_enabled(lab):
    assert lab.client.post("/v1/admin/connectors/se.ica/enable").status_code == 409
    assert lab.client.post("/v1/admin/connectors/no.such/enable").status_code == 404
    assert lab.client.post("/v1/admin/connectors/no.such/disable").status_code == 404


# ---- settings --------------------------------------------------------------------------

def test_settings_are_write_only_encrypted_and_reach_the_connector(fresh_db, monkeypatch):
    monkeypatch.delenv("COOP_API_KEY", raising=False)
    retrieval.clear_cache()
    site = FakeCoop()
    monkeypatch.setitem(registry.CONNECTORS, "se.coop", coop_connector(site, key=None))
    with SessionLocal.begin() as db:
        db.add(Store(retailer_id="se.coop", external_id="255700", name="s"))
    client = admin_client()
    store_id = next(s["id"] for s in client.get("/v1/stores").json()["items"]
                    if s["retailer_id"] == "se.coop")
    ns = SimpleNamespace(client=client, site=site, store_id=store_id)
    mid = monitor(ns, "generic_product", query="havregryn").json()["id"]
    assert check(ns, mid)["state"] == "auth_required" and site.calls == []

    secret = "admin-entered-key-" + "z" * 14
    assert client.put("/v1/admin/connectors/se.coop/settings/COOP_API_KEY",
                      json={"value": secret}).status_code == 204
    assert client.put("/v1/admin/connectors/se.coop/settings/NOPE", json={"value": "x"}).status_code == 404
    shown = {i["id"]: i for i in client.get("/v1/admin/connectors").json()["items"]}["se.coop"]
    assert shown["status"] == "ready"
    assert shown["settings"][0]["configured"] and shown["settings"][0]["source"] == "admin"
    assert check(ns, mid)["state"] == "succeeded"
    assert site.calls[-1].headers["ocp-apim-subscription-key"] == secret  # resolved from the database

    with SessionLocal() as db:
        row = db.scalars(select(ConnectorSetting)).one()
        assert secret not in row.ciphertext  # encrypted at rest
        assert all(secret not in json.dumps(a.details) for a in db.scalars(select(AuditLog)))
    everything = [client.get(path).text for path in (
        "/v1/admin/connectors", "/v1/connectors", "/v1/admin/jobs", "/v1/admin/audit")]
    assert all(secret not in text for text in everything)  # never returned anywhere

    assert client.delete("/v1/admin/connectors/se.coop/settings/COOP_API_KEY").status_code == 204
    assert client.delete("/v1/admin/connectors/se.coop/settings/COOP_API_KEY").status_code == 404
    retrieval.clear_cache()  # identical lookups are cached for a few minutes, by design
    assert check(ns, mid)["state"] == "auth_required"


def test_an_environment_setting_is_used_when_nothing_was_entered(fresh_db, monkeypatch):
    monkeypatch.setenv("COOP_API_KEY", "from-the-environment-" + "e" * 11)
    shown = {i["id"]: i for i in admin_client().get("/v1/admin/connectors").json()["items"]}["se.coop"]
    assert shown["settings"][0]["source"] == "environment" and shown["status"] == "ready"


# ---- installing an uploaded package -----------------------------------------------------

def test_upload_previews_without_installing_anything(lab):
    response = upload(lab, build(TEMPLATE))
    assert response.status_code == 201
    package = response.json()["package"]
    assert package["id"] == "ex.exampleshop" and package["allowed_hosts"] == ["api.exampleshop.example.org"]
    assert package["settings"] == ["EXAMPLESHOP_TOKEN"] and package["replaces"] is None
    assert {f["path"] for f in package["files"]} >= {"manifest.yaml", "connector.py"}
    assert "ex.exampleshop" not in registry.ENTRIES
    assert not (lab.dir / "ex.exampleshop").exists()  # staged only; nothing executed or installed


def test_the_full_lifecycle_install_configure_enable_use_update_and_remove(lab):
    done = install(lab, build(TEMPLATE))
    assert done.status_code == 201 and done.json()["status"] == "disabled"  # installed != running
    item = listing(lab)["ex.exampleshop"]
    assert item["source"] == "installed" and item["installed_at"] and item["enabled"] is False
    assert (lab.dir / "ex.exampleshop" / "connector.py").exists()
    assert "ex.exampleshop" in registry.CONNECTORS  # live without a restart

    assert lab.client.put("/v1/admin/connectors/ex.exampleshop/settings/EXAMPLESHOP_TOKEN",
                          json={"value": "token-value"}).status_code == 204
    assert lab.client.post("/v1/admin/connectors/ex.exampleshop/enable").status_code == 204
    assert listing(lab)["ex.exampleshop"]["status"] == "ready"

    # use it for real through the queue and worker, against a fake site
    def shop(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer token-value"
        if request.url.path == "/v1/stores":
            return httpx.Response(200, json={"stores": [{"id": 12, "name": "Example Central"}]})
        hits = [{"sku": "A1", "name": "Oat flakes", "price": "24.90", "size": "1 kg"}]
        return httpx.Response(200, json={"products": hits if request.url.params.get("q") else []})

    connector = registry.CONNECTORS["ex.exampleshop"]
    connector.client._client = httpx.Client(transport=httpx.MockTransport(shop), follow_redirects=False)
    connector.client.limiter.interval = 0
    ns = SimpleNamespace(client=lab.client)
    found = job_of(ns, lab.client.post("/v1/admin/retailers/ex.exampleshop/stores/discover"))
    assert found["result"]["status"] == "success" and found["result"]["stores"] == 1
    card = listing(lab)["ex.exampleshop"]    # the admin card reports what the lookup did
    assert card["stores"] == 1 and card["last_discovery"]["state"] == "succeeded"
    assert card["last_discovery"]["result"]["created"] == 1
    store_id = next(s["id"] for s in lab.client.get("/v1/stores").json()["items"]
                    if s["retailer_id"] == "ex.exampleshop")
    ns.store_id = store_id
    result = search(ns, query="oat")
    assert result["state"] == "succeeded" and result["result"]["items"][0]["name"] == "Oat flakes"

    # updating keeps the enabled state; the preview says what it replaces
    update = upload(lab, build(TEMPLATE)).json()
    assert update["package"]["replaces"]["version"] == "0.1.0"
    assert lab.client.post(f"/v1/admin/connector-packages/{update['upload_id']}/install").status_code == 201
    assert listing(lab)["ex.exampleshop"]["enabled"] is True

    # removal is refused while monitors use it, then keeps the data
    mid = monitor(ns, "generic_product", query="oat").json()["id"]
    assert lab.client.delete("/v1/admin/connector-packages/ex.exampleshop").status_code == 409
    assert lab.client.delete("/v1/admin/connector-packages/ex.exampleshop?disable_monitors=true"
                             ).status_code == 204
    assert "ex.exampleshop" not in registry.CONNECTORS and not (lab.dir / "ex.exampleshop").exists()
    assert lab.client.get(f"/v1/monitors/{mid}").json()["enabled"] is False
    view = lab.client.get("/v1/admin/connectors").json()
    assert [o["id"] for o in view["orphans"]] == ["ex.exampleshop"] and view["orphans"][0]["stores"] == 1
    with SessionLocal() as db:
        assert db.scalars(select(ConnectorSetting)).all() == []  # its secrets are removed with it
        actions = [a.action for a in db.scalars(select(AuditLog))]
    assert {"connector.installed", "connector.enabled", "connector.uninstalled",
            "connector.setting_set"} <= set(actions)


def test_builtin_connectors_cannot_be_removed_or_replaced(lab):
    assert lab.client.delete("/v1/admin/connector-packages/se.coop").status_code == 404
    clash = template_files(**{"manifest.yaml": (TEMPLATE / "manifest.yaml").read_text().replace(
        "ex.exampleshop", "se.coop")})
    response = upload(lab, zipped(clash))
    assert response.status_code == 422 and "built-in" in response.json()["message"]


# ---- unsafe or broken packages are refused, and nothing is left behind -------------------

def symlink_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in template_files().items():
            archive.writestr(name, content)
        link = zipfile.ZipInfo("link.py")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "/etc/passwd")
    return buffer.getvalue()


BAD_MANIFEST = (TEMPLATE / "manifest.yaml").read_text()
BAD = {
    "path traversal": (zipped({**template_files(), "../evil.py": "x"}), "Unsafe file name"),
    "absolute path": (zipped({**template_files(), "/etc/evil.py": "x"}), "Unsafe file name"),
    "backslash path": (zipped({**template_files(), "a\\b.py": "x"}), "Unsafe file name"),
    "hidden file": (zipped({**template_files(), ".env": "SECRET=1"}), "Unsafe file name"),
    "symlink": (symlink_zip(), "Symbolic links"),
    "executable": (zipped({**template_files(), "payload.so": b"\x7fELF"}), "File type not allowed"),
    "not a zip": (b"this is not a zip file", "not a valid zip"),
    "empty zip": (zipped({}), "empty"),
    "no manifest": (zipped({"connector.py": "x = 1"}), "manifest.yaml is missing"),
    "unknown key": (zipped(template_files(**{"manifest.yaml": BAD_MANIFEST + "allowed_host: [x.org]\n"})),
                    "allowed_host"),
    "no allowed hosts": (zipped(template_files(**{"manifest.yaml": BAD_MANIFEST.replace(
        "allowed_hosts: [api.exampleshop.example.org]", "allowed_hosts: []")})), "allowed_hosts"),
    "url instead of host": (zipped(template_files(**{"manifest.yaml": BAD_MANIFEST.replace(
        "[api.exampleshop.example.org]", "['https://api.exampleshop.example.org/v1']")})), "allowed_hosts"),
    "invalid regex in product_urls": (zipped(template_files(**{"manifest.yaml": BAD_MANIFEST.replace(
        "(?P<sku>[A-Za-z0-9]+)/?$'", "(?P<sku>[A-Za-z0-9]+(/?$'")})), "regular expression"),
    "product_urls without a sku group": (zipped(template_files(**{"manifest.yaml": BAD_MANIFEST.replace(
        "(?P<sku>[A-Za-z0-9]+)", "(?P<id>[A-Za-z0-9]+)")})), "named group"),
    "future protocol": (zipped(template_files(**{"manifest.yaml": BAD_MANIFEST.replace(
        "protocol_version: 1", "protocol_version: 2")})), "protocol"),
    "missing module": (zipped({"manifest.yaml": BAD_MANIFEST}), "is not in the package"),
    "missing class": (zipped(template_files(**{"connector.py": "class Other:\n    pass\n"})),
                      "no top-level class"),
    "syntax error": (zipped(template_files(**{"connector.py": "def broken(:\n"})), "does not parse"),
    "dotted implementation": (zipped(template_files(**{"manifest.yaml": BAD_MANIFEST.replace(
        "connector:ExampleShopConnector", "os.path:join")})), "inside the package"),
    "zip bomb": (zipped({**template_files(), "big.json": "0" * 3_000_000}), "compression ratio"),
    "too many files": (zipped({**template_files(), **{f"f{i}.txt": "x" for i in range(250)}}),
                       "too many files"),
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_unsafe_or_broken_packages_are_refused_and_leave_nothing_behind(lab, name):
    data, fragment = BAD[name]
    response = upload(lab, data)
    assert response.status_code == 422, (name, response.text)
    assert fragment in response.json()["message"], (name, response.json()["message"])
    assert "ex.exampleshop" not in registry.ENTRIES
    leftovers = [p for p in lab.dir.rglob("*") if p.is_file()] if lab.dir.exists() else []
    assert leftovers == [], leftovers


def test_oversized_uploads_are_refused_before_being_read(lab, monkeypatch):
    monkeypatch.setattr(settings, "max_package_bytes", 1000)
    assert upload(lab, b"x" * 5000).status_code == 413
    streamed = lab.client.post("/v1/admin/connector-packages/inspect", headers=ZIP,
                               content=iter([b"x" * 600, b"x" * 600]))
    assert streamed.status_code == 413


def test_risky_code_is_flagged_in_the_preview_but_the_administrator_decides(lab):
    risky = template_files(**{"connector.py": (TEMPLATE / "connector.py").read_text()
                              + "\nimport subprocess\nimport not_a_real_module_xyz\nexec('1')\n"})
    preview = upload(lab, zipped(risky)).json()["package"]["warnings"]
    assert any("subprocess" in w for w in preview) and any("exec()" in w for w in preview)
    assert any("not_a_real_module_xyz" in w and "not installed" in w for w in preview)


def test_a_package_that_fails_to_load_is_rolled_back_and_the_previous_version_stays(lab):
    assert install(lab, build(TEMPLATE)).status_code == 201
    good = registry.CONNECTORS["ex.exampleshop"]
    broken = zipped(template_files(**{"connector.py": (TEMPLATE / "connector.py").read_text()
                                      + "\nraise RuntimeError('cannot start')\n"}))
    response = install(lab, broken)
    assert response.status_code == 422 and "cannot start" in response.json()["message"]
    assert registry.CONNECTORS["ex.exampleshop"].__class__.__name__ == good.__class__.__name__
    assert "cannot start" not in (lab.dir / "ex.exampleshop" / "connector.py").read_text()
    assert registry.LOAD_ERRORS == []
    # a brand-new package that passes the static checks but cannot start: nothing is left behind
    second = template_files(**{
        "manifest.yaml": BAD_MANIFEST.replace("ex.exampleshop", "ex.second"),
        "connector.py": (TEMPLATE / "connector.py").read_text().replace(
            'id = "ex.exampleshop"', 'id = "ex.second"') + "\nraise RuntimeError('nope')\n"})
    first_install = install(lab, zipped(second))
    assert first_install.status_code == 422 and "nope" in first_install.json()["message"]
    assert "ex.second" not in registry.ENTRIES and not (lab.dir / "ex.second").exists()


def test_uploads_expire_and_cannot_be_installed_twice_or_guessed(lab):
    token = upload(lab, build(TEMPLATE)).json()["upload_id"]
    staged = lab.dir / ".staging" / f"{token}.zip"
    os.utime(staged, (time.time() - 7200, time.time() - 7200))
    expired = lab.client.post(f"/v1/admin/connector-packages/{token}/install")
    assert expired.status_code == 422 and "expired" in expired.json()["message"]
    for bad in ("../../etc/passwd", "0" * 32, "x"):
        assert lab.client.post(f"/v1/admin/connector-packages/{bad}/install").status_code in (404, 422)
    token = upload(lab, build(TEMPLATE)).json()["upload_id"]
    assert lab.client.post(f"/v1/admin/connector-packages/{token}/install").status_code == 201
    assert lab.client.post(f"/v1/admin/connector-packages/{token}/install").status_code == 422

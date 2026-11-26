"""Administration of connectors: settings, status, and the combined view the admin page shows."""
import os
from functools import lru_cache

from dotenv import dotenv_values
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import connector_state
from app.connectors import registry
from app.models import ConnectorPackage, ConnectorSetting, Job, Monitor, Store, utcnow
from app.security import decrypt_secret, encrypt_secret


@lru_cache(maxsize=1)
def _dotenv() -> dict:
    return {key: value for key, value in dotenv_values(".env").items() if value}


def _from_environment(name: str) -> str | None:
    return os.environ.get(name) or _dotenv().get(name) or None


def resolve_config(db: Session, retailer_id: str) -> dict[str, str]:
    """The settings a connector declares, resolved: administrator-entered value first, then the
    environment. The connector only ever sees the values, never where they came from."""
    info = registry.retailer(retailer_id)
    if not info:
        return {}
    stored = {row.name: row for row in db.scalars(
        select(ConnectorSetting).where(ConnectorSetting.retailer_id == retailer_id))}
    values: dict[str, str] = {}
    for spec in info["settings"]:
        row = stored.get(spec["name"])
        value = decrypt_secret(row.ciphertext) if row else _from_environment(spec["name"])
        if value:
            values[spec["name"]] = value
    return values


def apply_config(db: Session, retailer_id: str, connector) -> None:
    if hasattr(connector, "configure"):
        connector.configure(resolve_config(db, retailer_id))


def settings_status(db: Session, retailer_id: str) -> list[dict]:
    """What the admin page shows: which settings are set and where from. Never the values."""
    info = registry.retailer(retailer_id)
    if not info:
        return []
    stored = {row.name for row in db.scalars(
        select(ConnectorSetting).where(ConnectorSetting.retailer_id == retailer_id))}
    result = []
    for spec in info["settings"]:
        source = "admin" if spec["name"] in stored else (
            "environment" if _from_environment(spec["name"]) else None)
        result.append({**spec, "configured": source is not None, "source": source})
    return result


def declared_setting(retailer_id: str, name: str) -> dict | None:
    info = registry.retailer(retailer_id)
    return next((spec for spec in (info or {}).get("settings", []) if spec["name"] == name), None)


def set_setting(db: Session, retailer_id: str, name: str, value: str) -> None:
    row = db.get(ConnectorSetting, (retailer_id, name))
    if row:
        row.ciphertext, row.updated_at = encrypt_secret(value), utcnow()
    else:
        db.add(ConnectorSetting(retailer_id=retailer_id, name=name,
                                ciphertext=encrypt_secret(value)))


def clear_setting(db: Session, retailer_id: str, name: str) -> bool:
    row = db.get(ConnectorSetting, (retailer_id, name))
    if row:
        db.delete(row)
    return row is not None


def status_of(db: Session, info: dict) -> str:
    """One word for the admin page, in priority order."""
    if info["load_error"]:
        return "error"
    if not info["installable"]:
        return "not_installable"
    if not connector_state.is_enabled(db, info["id"]):
        return "disabled"
    state = connector_state.get_state(db, info["id"])
    if state.state == "paused":
        return "paused"
    if any(spec["required"] and not spec["configured"]
           for spec in settings_status(db, info["id"])):
        return "needs_configuration"
    return "ready"


def last_discoveries(db: Session) -> dict[str, dict]:
    """The most recent store lookup per connector, so the admin page can show what it did."""
    latest: dict[str, dict] = {}
    for job in db.scalars(select(Job).where(Job.kind == "discover_stores")
                          .order_by(Job.created_at.desc()).limit(200)):
        payload = job.payload or {}
        latest.setdefault(payload.get("retailer_id", ""), {
            "job_id": job.id, "state": job.state, "inputs": payload.get("inputs") or {},
            "created_at": job.created_at, "result": job.result})
    return latest


def admin_view(db: Session) -> dict:
    discoveries = last_discoveries(db)
    stores = dict(db.execute(select(Store.retailer_id, func.count()).group_by(Store.retailer_id)).all())
    monitors = dict(db.execute(
        select(Store.retailer_id, func.count()).select_from(Monitor)
        .join(Store, Monitor.store_id == Store.id).where(Monitor.enabled.is_(True))
        .group_by(Store.retailer_id)).all())
    packages = {row.retailer_id: row for row in db.scalars(select(ConnectorPackage))}
    items = []
    for info in registry.retailers():
        state = connector_state.get_state(db, info["id"])
        package = packages.get(info["id"])
        items.append({
            **{key: info[key] for key in (
                "id", "name", "country", "description", "author", "source", "installable",
                "load_error", "sha256", "access_review", "access_note", "capabilities",
                "connector_version", "allowed_hosts", "minimum_monitor_interval_minutes",
                "max_requests_per_day", "store_discovery_inputs")},
            "enabled": connector_state.is_enabled(db, info["id"]),
            "default_enabled": info["enabled"],
            "overridden": state.enabled is not None,
            "status": status_of(db, info),
            "paused": state.state == "paused", "pause_reason": state.reason,
            "settings": settings_status(db, info["id"]),
            "last_discovery": discoveries.get(info["id"]),
            "stores": stores.get(info["id"], 0), "active_monitors": monitors.get(info["id"], 0),
            "installed_at": package.installed_at if package else None,
        })
    known = {info["id"] for info in registry.retailers()}
    orphans = [{"id": rid, "stores": count, "active_monitors": monitors.get(rid, 0)}
               for rid, count in stores.items() if rid not in known]
    return {"items": items, "load_errors": registry.load_errors(), "orphans": orphans}

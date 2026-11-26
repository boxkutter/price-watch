import importlib
import inspect
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from app.connectors.base import Connector

PROTOCOL_VERSION = 1


class Manifest(BaseModel):
    id: str = Field(pattern=r"^[a-z]{2,10}\.[a-z0-9_-]+$")
    name: str
    country: str = Field(min_length=2, max_length=2)
    protocol_version: int
    connector_version: str
    enabled: bool = False
    access_review: Literal["approved", "unverified", "denied"] = "unverified"
    implementation: str | None = None
    capabilities: dict[str, Literal["supported", "unsupported", "unverified"]] = Field(default_factory=dict)
    allowed_hosts: list[str] = Field(default_factory=list)
    minimum_request_interval_seconds: int = Field(default=60, ge=1)
    # Politeness limits. Live connectors must stay well inside what a person could do by hand.
    minimum_monitor_interval_minutes: int = Field(default=15, ge=1)
    max_requests_per_scan: int = Field(default=6, ge=1)
    max_requests_per_day: int = Field(default=500, ge=1)
    cache_seconds: int = Field(default=0, ge=0)
    access_note: str | None = None
    store_discovery_requires_postcode: bool = False


def load_registry(directory: Path | None = None) -> tuple[list[dict], dict[str, Connector]]:
    directory = directory or Path(__file__).parent / "manifests"
    retailers = []
    connectors = {}
    for file in sorted(directory.glob("*.yaml")):
        manifest = Manifest.model_validate(yaml.safe_load(file.read_text()))
        if manifest.protocol_version != PROTOCOL_VERSION:
            raise ValueError(f"Unsupported connector protocol: {manifest.id}")
        if manifest.id in {item["id"] for item in retailers}:
            raise ValueError(f"Duplicate retailer ID: {manifest.id}")
        if manifest.enabled:
            if manifest.access_review != "approved" or not manifest.implementation:
                raise ValueError(f"Enabled connector lacks access review or implementation: {manifest.id}")
            if not manifest.id.startswith("test.") and not manifest.allowed_hosts:
                raise ValueError(f"Enabled live connector lacks allowed hosts: {manifest.id}")
            module_name, separator, class_name = manifest.implementation.partition(":")
            if not separator:
                raise ValueError(f"Invalid connector import path: {manifest.id}")
            factory = getattr(importlib.import_module(module_name), class_name)
            # Connectors that take a `manifest` receive their own validated manifest (limits,
            # allowed hosts); simple test connectors need no arguments.
            takes_manifest = "manifest" in inspect.signature(factory).parameters
            instance = factory(manifest=manifest.model_dump()) if takes_manifest else factory()
            if instance.id != manifest.id:
                raise ValueError(f"Connector ID mismatch: {manifest.id}")
            connectors[manifest.id] = instance
        retailers.append({"id": manifest.id, "name": manifest.name,
                          "country": manifest.country, "enabled": manifest.enabled,
                          "access_review": manifest.access_review,
                          "capabilities": manifest.capabilities,
                          "connector_version": manifest.connector_version,
                          "minimum_monitor_interval_minutes": manifest.minimum_monitor_interval_minutes,
                          "max_requests_per_day": manifest.max_requests_per_day,
                          "cache_seconds": manifest.cache_seconds,
                          "access_note": manifest.access_note,
                          "store_discovery_requires_postcode": manifest.store_discovery_requires_postcode})
    return retailers, connectors


RETAILERS, CONNECTORS = load_registry()


def connector_for(retailer_id: str):
    return CONNECTORS.get(retailer_id)

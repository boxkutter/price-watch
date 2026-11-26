"""The connector manifest: everything the core needs to know about a connector as data.

A connector package is a folder with `manifest.yaml` and the module the manifest points at.
Unknown keys are rejected so a typo (`allowed_host`) is an error, not a silently ignored limit.
"""
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROTOCOL_VERSION = 1
_HOST = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_IMPLEMENTATION = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$")


class SettingSpec(BaseModel):
    """A value the connector needs from the operator (an API key, a token)."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")
    label: str | None = None
    description: str | None = None
    secret: bool = True
    required: bool = True


class DiscoveryInput(BaseModel):
    """An input the store lookup needs (for example a postcode)."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,30}$")
    label: str | None = None
    pattern: str | None = None
    required: bool = True

    @field_validator("pattern")
    @classmethod
    def compiles(cls, value: str | None) -> str | None:
        if value:
            re.compile(value)
        return value


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z]{2,10}\.[a-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=80)
    country: str = Field(min_length=2, max_length=2)
    protocol_version: int
    connector_version: str = Field(min_length=1, max_length=40)
    description: str | None = None
    author: str | None = None
    # Default state. An administrator can override it at runtime without editing this file.
    enabled: bool = False
    access_review: Literal["approved", "unverified", "denied"] = "unverified"
    access_note: str | None = None
    # "module:Class", where module is a file in this package (`connector:MyConnector`) or an
    # importable dotted path. Empty for a documented-but-not-installable placeholder.
    implementation: str | None = None
    capabilities: dict[str, Literal["supported", "unsupported", "unverified"]] = Field(
        default_factory=dict)
    allowed_hosts: list[str] = Field(default_factory=list)
    settings: list[SettingSpec] = Field(default_factory=list)
    store_discovery_inputs: list[DiscoveryInput] = Field(default_factory=list)
    # Politeness limits. Live connectors must stay well inside what a person could do by hand.
    minimum_request_interval_seconds: int = Field(default=60, ge=1)
    minimum_monitor_interval_minutes: int = Field(default=15, ge=1)
    max_requests_per_scan: int = Field(default=6, ge=1)
    max_requests_per_day: int = Field(default=500, ge=1)
    cache_seconds: int = Field(default=0, ge=0)

    @field_validator("allowed_hosts")
    @classmethod
    def hosts_are_plain_names(cls, hosts: list[str]) -> list[str]:
        for host in hosts:
            if not _HOST.match(host):
                raise ValueError(f"allowed_hosts entries must be host names or *.domain: {host!r}")
        return hosts

    @field_validator("implementation")
    @classmethod
    def implementation_format(cls, value: str | None) -> str | None:
        if value and not _IMPLEMENTATION.match(value):
            raise ValueError("implementation must look like 'module:ClassName'")
        return value

    @model_validator(mode="after")
    def consistent(self):
        names = [spec.name for spec in self.settings]
        if len(names) != len(set(names)):
            raise ValueError("settings names must be unique")
        if self.implementation and not self.id.startswith("test.") and not self.allowed_hosts:
            raise ValueError("a connector with an implementation must declare allowed_hosts")
        return self


def parse_manifest(text: str) -> Manifest:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("manifest.yaml must contain a mapping")  # noqa: TRY004
    manifest = Manifest.model_validate(data)
    if manifest.protocol_version != PROTOCOL_VERSION:
        raise ValueError(f"Unsupported connector protocol {manifest.protocol_version} "
                         f"(this service speaks {PROTOCOL_VERSION})")
    return manifest


def load_manifest(path: Path) -> Manifest:
    return parse_manifest(path.read_text(encoding="utf-8"))

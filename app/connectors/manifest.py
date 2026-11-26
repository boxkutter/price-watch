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
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"not a valid regular expression: {exc}") from exc
        return value


class StoreSpec(BaseModel):
    """A store the connector always has, so nobody has to discover it (for example a retailer
    with one national online assortment). The service creates it on start-up and on install."""
    model_config = ConfigDict(extra="forbid")
    external_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")  # what scan() receives as the store
    name: str = Field(min_length=1, max_length=120)


_WINDOW = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


class ProductUrl(BaseModel):
    """How to read one kind of this store's product page link (the link is parsed, not fetched)."""
    model_config = ConfigDict(extra="forbid")
    # A regular expression over the normalised URL (see app/connectors/urls.py): https, lower-case
    # host, percent-decoded path, no query, fragment or port. It must capture the product id in a
    # named group `sku`, and should be anchored (^ and $).
    pattern: str = Field(min_length=5, max_length=500)
    example: str | None = None  # a real product page link; the loader checks the pattern reads it

    @field_validator("pattern")
    @classmethod
    def has_sku_group(cls, value: str) -> str:
        try:
            compiled = re.compile(value)
        except re.error as exc:
            raise ValueError(f"not a valid regular expression: {exc}") from exc
        if "sku" not in compiled.groupindex:
            raise ValueError("the pattern must capture the product id in a named group (?P<sku>...)")
        return value

    @model_validator(mode="after")
    def example_matches(self):
        if self.example:
            from app.connectors.urls import match_product_url  # local: avoids an import cycle
            if not match_product_url([{"pattern": self.pattern}], self.example):
                raise ValueError(f"the pattern does not read its own example {self.example!r}")
        return self


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
    # Stores that exist without discovery (see StoreSpec).
    stores: list[StoreSpec] = Field(default_factory=list)
    # Lets a person add a product by pasting its page link. Optional; without it, products are
    # added by search or barcode only.
    product_urls: list[ProductUrl] = Field(default_factory=list)
    # Politeness limits. Live connectors must stay well inside what a person could do by hand.
    minimum_request_interval_seconds: int = Field(default=60, ge=1)
    minimum_monitor_interval_minutes: int = Field(default=15, ge=1)
    max_requests_per_scan: int = Field(default=6, ge=1)
    max_requests_per_day: int = Field(default=500, ge=1)
    cache_seconds: int = Field(default=0, ge=0)
    # "HH:MM-HH:MM" in UTC (may cross midnight). When the retailer states when automated visits are
    # welcome (for example in robots.txt), the service starts *scheduled* monitor checks only
    # inside this window. Anything a person asks for (search, add by link, compare, "check now")
    # is not scheduled work and is never delayed by it.
    scheduled_window_utc: str | None = None
    # Follow a redirect (at most 2, GET only) when it leads to another host in allowed_hosts, for a
    # store whose canonical page address differs from the one a link gives. A redirect anywhere
    # else is still a refusal. Each hop counts as a request.
    follow_redirects: bool = False

    @field_validator("scheduled_window_utc")
    @classmethod
    def window_format(cls, value: str | None) -> str | None:
        if value is not None and not _WINDOW.match(value):
            raise ValueError("scheduled_window_utc must look like '04:00-08:45' (UTC)")
        return value

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
        store_ids = [spec.external_id for spec in self.stores]
        if len(store_ids) != len(set(store_ids)):
            raise ValueError("stores external_id values must be unique")
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

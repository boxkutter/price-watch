# Connectors

A **connector** teaches the service about one store or service. Everything store-specific lives in
the connector; the core (monitors, history, events, notifications, politeness limits, the admin
page) knows nothing about any particular store. Connectors are installed, enabled and removed
without touching the core, in the admin page or by dropping a folder on the server.

Built-in connectors (`app/connectors/builtin/`) and installed ones have exactly the same structure.
Read `builtin/citygross/` for a real example and `docs/connector-template/` for one to copy.

## Where to start

| You are | Start with |
|---|---|
| A developer adding a store | [connector-guide.md](connector-guide.md): the steps, how to investigate a site, the pitfalls found on real stores |
| An AI coding agent | [connector-template/AGENTS.md](connector-template/AGENTS.md): hard rules, workflow, commands, checklist |
| Editing a manifest | [connector-template/manifest.schema.json](connector-template/manifest.schema.json) (JSON Schema, regenerate with `python -m app.connectors.build --schema`) and the table below |
| An administrator | The [Administration](#administration) section |

This page is the reference. The template folder is also what a package built with the build
command contains, so `AGENTS.md` and `README.md` travel with it.

## Access policy

Connectors follow the [access policy](../readme.md#access-policy): they fetch only what the owner
asked for (a text query, specific products, specific barcodes), never crawl or bulk-harvest, stay
well inside what a person could do by hand, and never disguise themselves or work around a refusal.
If a site refuses the client, the connector reports `access_blocked` and the service pauses it.

## A connector is a folder

```text
ex.exampleshop/
├── manifest.yaml      what the service needs to know, as data
├── connector.py       the code (any module name; the manifest points at it)
├── test_connector.py  optional: your tests (the template has them)
└── README.md          optional
```

`manifest.yaml` and the module its `implementation` names are the only required files. Helper
modules, JSON fixtures and docs are allowed (`.py .yaml .yml .json .md .txt`).

## Quick start

1. Copy `docs/connector-template/` and rename it.
2. Edit `manifest.yaml` (id, allowed hosts, settings, limits) and `connector.py` (`collect()`, and
   `discover()` if the store has a directory).
3. Describe the real API with a fake site in `test_connector.py` and run `pytest -q` in the folder.
   `check_connector(...)` runs the contract every connector must pass.
4. `python -m app.connectors.build path/to/folder` makes a zip, validated exactly like an upload.
5. In the admin page: **Install a connector**, review the preview, **Install**, set its settings,
   **Enable**.

## The manifest

Unknown keys are rejected, so a typo is an error rather than a silently ignored limit.

| Key | Meaning |
|---|---|
| `id` | `<2-10 letters>.<name>`, globally unique (`se.coop`). `test.` ids are for tests |
| `name`, `country`, `description`, `author` | Shown in the admin page |
| `protocol_version` | The connector protocol this was written for (currently `1`) |
| `connector_version` | Your version; shown in the admin page and the install preview |
| `enabled` | Default state. **Installing never enables**; an administrator does, and their choice overrides this |
| `access_review`, `access_note` | Informational: why automating this site is acceptable, and caveats |
| `implementation` | `module:Class` in this folder (`connector:MyConnector`). Omit for a documented placeholder (ICA) |
| `capabilities` | `store_discovery`, `product_search`, `barcode_search`, `online_prices`, `physical_store_prices`, `store_offers`, each `supported`, `unsupported` or `unverified`. The service only offers monitors a connector supports |
| `allowed_hosts` | The only hosts it may contact (HTTPS only): bare host names or `*.domain`. Required for any connector with an implementation |
| `settings` | Values the administrator provides: `name` (UPPER_CASE), `label`, `description`, `secret`, `required` |
| `store_discovery_inputs` | Inputs a store lookup needs: `name`, `label`, `pattern` (regex), `required` (Coop declares `postcode`) |
| `minimum_request_interval_seconds` | Gap between requests to the site |
| `minimum_monitor_interval_minutes` | How often one monitor may check |
| `max_requests_per_scan` / `max_requests_per_day` | Hard caps (per scan; per rolling 24 hours, kept in the database) |
| `cache_seconds` | Identical lookups within this window share one fetch |

## Writing `connector.py`

Extend `HttpJsonConnector` from `app.connectors.kit` and implement two methods:

```python
class MyConnector(HttpJsonConnector):
    id = "xx.mystore"
    store_id_pattern = r"\d+"           # how this store writes its ids

    def discover(self, **inputs) -> list[StoreInfo]: ...          # optional
    def collect(self, store: str, request: ScanRequest) -> Collected: ...
```

`request` says what to fetch: `request.query` (a search text), `request.skus` (the store's own
product ids) and `request.gtins` (barcodes). Return `Collected(prices=..., offers=..., missing=...,
truncated=..., fetched=..., skipped=...)`. The kit provides everything else: it refuses an untargeted
or unsupported-channel scan before any request, enforces the per-scan budget, counts requests, maps
every failure to a standard status, and never lets a secret into a result.

Helpers: `self.client` (a `PoliteClient`: host allowlist, spacing, no redirects, size and time
limits; use `get_json` / `post_json`), `self.require("SETTING")`, `to_decimal()`, `valid_gtin()`,
and `parse_size()` from `app.pricing` for Swedish package labels.

### Rules the contract tests enforce

- Fetch only what was asked. Never enumerate categories, "all offers" or the catalogue.
- Use `self.client`, never your own HTTP client. Put secrets in headers, never URLs.
- An unknown price is skipped, never `0`. Unknown stock is `None`, not "out of stock".
- A product that was asked for but not returned is `missing` (unknown), never "discontinued".
- Report a refusal once and stop; do not retry, rotate headers or probe.

### Result statuses

You raise or return; the service reacts.

| Status | Meaning | Service reaction |
|---|---|---|
| `success`, `partial`, `not_found` | Data (or a clean "none") | Processed; `partial` and `not_found` create no transitions |
| `unsupported`, `invalid_data` | Request this connector cannot serve | Reported on the job |
| `auth_required` | A required setting is missing | Reported; nothing requested; not a failure |
| `rate_limited` | HTTP 429 (pass `Retry-After`) | Cooldown honouring `Retry-After` |
| `access_blocked` | 401/403/redirect to a challenge | **Connector paused** until an administrator resumes it |
| `extraction_failed` | Response is not in the expected shape | Counts toward the pause threshold (5 in a row) |
| `TransientError` (raised) | Network error, timeout, 5xx | The job retries with backoff (at most 5) |

## Settings and secrets

A connector declares what it needs under `settings` in its manifest and reads it with
`self.require("NAME")`. An administrator provides the value in the admin page (stored encrypted
with a key derived from `APP_SECRET`) or as an environment variable of the same name; the admin
value wins. Secret values are write-only: never returned by the API, shown in the page, written to
the audit log or included in results.

## Contract tests

```python
check_connector(make, handler, manifest=manifest, store="12",
                request=ScanRequest(query="oat"), secrets={"MY_TOKEN": "s3cret"})
```

`make(transport, secrets)` builds your connector against a mock transport; `handler` is a fake site
that answers the happy path. The suite also checks, on its own: no request for untargeted scans or
unsupported channels, `auth_required` without settings, hosts stay inside `allowed_hosts`, each
refusal maps to the right status and is not retried, garbage responses do not crash, server errors
and timeouts are transient, secrets stay out of URLs and results, and store discovery.

## Administration

The admin page lists every connector with a status:

| Status | Meaning |
|---|---|
| Ready | Enabled, configured, not paused |
| Disabled | Off (the default for a newly installed connector) |
| Needs configuration | Enabled but a required setting is missing |
| Paused | The site refused it or it failed repeatedly; resume after review |
| Error | The package failed to load; the reason is shown and nothing else is affected |
| Not installable | A placeholder with no implementation (ICA) |

Actions: **Enable / Disable** (stored in the database, effective immediately in the API and the
worker, no restart), **Pause / Resume**, **settings**, **Find stores**, **Remove**, and
**Install a connector**.

Installing is two steps. *Review* reads the zip without executing anything and shows the connector,
the hosts it may contact, the settings it needs, every file, and warnings (risky imports such as
`subprocess`, `eval()`, modules that are not installed). *Install* then re-validates, unpacks
atomically, loads it, and **rolls back automatically** if it fails to load. A replacement keeps the
previous enabled state. Removing a connector keeps its stores, prices and history; monitors that use
it must be disabled first (or removed with it). Every install, removal, enable, disable and setting
change is in the audit log.

API (all admin only): `GET /v1/admin/connectors`, `POST /v1/admin/connectors/{id}/enable|disable|pause|resume`,
`PUT|DELETE /v1/admin/connectors/{id}/settings/{name}`, `POST /v1/admin/connector-packages/inspect`
(the zip as the request body), `POST /v1/admin/connector-packages/{upload_id}/install`,
`DELETE /v1/admin/connector-packages/{id}`, `POST /v1/admin/retailers/{id}/stores/discover`.

## Trust and security

Connector code runs inside the service, with its privileges (database access, `APP_SECRET`),
exactly like any plugin. **Installing a package means trusting its author.** The admin page is
administrator-only, never reachable with a client token, and says so before installing. The service
also enforces what it can without being a sandbox:

- uploads are checked without executing them: path traversal, absolute paths, hidden files,
  symlinks, encrypted entries, disallowed file types, size and file-count limits, compression ratio,
  manifest validity, and that the named class exists
- the manifest's `allowed_hosts` and politeness limits are enforced on the standard client; a
  connector that opens its own sockets is outside that protection, which is why the preview warns
  about `socket`, `subprocess`, `ctypes`, `eval()` and similar
- built-in connector ids cannot be shadowed or replaced
- a connector that fails to load, or raises when instantiated, is quarantined and never stops the
  API, the worker or other connectors

## Deployment

Installed packages live in `CONNECTORS_DIR` (default `./data/connectors`; `/data/connectors` in
Docker). The API installs into it and the worker runs from it, so it must be shared storage: the
`connectors_data` volume in `docker-compose.yml`. Each process notices changes on disk within a
couple of seconds, so no restart is needed. You may also drop a folder there by hand.

## Beyond HTTP and JSON

`HttpJsonConnector` covers the common case. Any class with an `id` and
`scan(external_store_id, channel, request) -> Snapshot` is a connector; implement it directly for
other transports and return the same typed `Snapshot` (`app/connectors/base.py`). Optional
`discover_stores(**inputs)` and `configure(values)` are used when present.

# Connectors

A **connector** teaches the service about one store or service. Everything store-specific lives in
the connector; the core (monitors, history, events, notifications, politeness limits, the admin
page) knows nothing about any particular store. Connectors are installed, enabled and removed
without touching the core, in the admin page or by dropping a folder on the server.

Bundled connectors live in [`connectors/`](../connectors/README.md) at the repository root; ones
installed from the admin page live in `INSTALLED_CONNECTORS_DIR`. Both have exactly the same
structure and the same loader. Read `connectors/se.citygross/` for a real example and
`connectors/_template/` for one to copy.

## Where to start

| You are | Start with |
|---|---|
| A developer adding a store | [connector-guide.md](connector-guide.md) and the [connectors folder](../connectors/README.md): the steps, how to investigate a site, the pitfalls found on real stores |
| An AI coding agent | [connectors/_template/AGENTS.md](../connectors/_template/AGENTS.md): hard rules, workflow, commands, checklist |
| Editing a manifest | [connectors/_template/manifest.schema.json](../connectors/_template/manifest.schema.json) (JSON Schema, regenerate with `python -m app.connectors.build --schema`) and the table below |
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

1. Copy `connectors/_template/` to `connectors/<id>/` (or anywhere, if you will upload it).
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
| `implementation` | `module:Class` in this folder (`connector:MyConnector`). Omit for a documented placeholder |
| `capabilities` | `store_discovery`, `product_search`, `barcode_search`, `online_prices`, `physical_store_prices`, `store_offers`, each `supported`, `unsupported` or `unverified`. The service only offers monitors a connector supports |
| `allowed_hosts` | The only hosts it may contact (HTTPS only): bare host names or `*.domain`. Required for any connector with an implementation |
| `settings` | Values the administrator provides: `name` (UPPER_CASE), `label`, `description`, `secret`, `required` |
| `store_discovery_inputs` | Inputs a store lookup needs: `name`, `label`, `pattern` (regex), `required` (Coop declares `postcode`) |
| `stores` | Optional. Stores the connector always has, as `{external_id, name}`, created by the service on start-up and install. For a retailer with one national online assortment there is nothing to discover (Willys, Lidl) |
| `product_urls` | Optional. How to read this store's product page links so a person can add a product by pasting one: a list of `{pattern, example}` (see [Product links](#product-links)) |
| `minimum_request_interval_seconds` | Gap between requests to the site |
| `minimum_monitor_interval_minutes` | How often one monitor may check |
| `max_requests_per_scan` / `max_requests_per_day` | Hard caps (per scan; per rolling 24 hours, kept in the database) |
| `cache_seconds` | Identical lookups within this window share one fetch (the bundled connectors use 3600: prices rarely move within an hour; a person's "refresh" or "check now" skips it) |
| `follow_redirects` | `false` by default: any redirect is a refusal. Set `true` when the site's canonical page address differs from the one you can build from an id (Lidl). Only a redirect (GET, up to 2 hops) to another host in `allowed_hosts` over HTTPS is followed; each hop counts as a request, is spaced and checked like any other, and a redirect anywhere else is still `access_blocked` |
| `scheduled_window_utc` | Optional `HH:MM-HH:MM` in UTC (may cross midnight). When the retailer states when automated visits are welcome (Willys' robots.txt does), *scheduled* monitor checks start only inside it; a monitor that comes due outside simply waits. Searches, add-by-link, comparisons and "check now" are requests a person made, are not scheduled work, and are never delayed by it |

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
product ids) and `request.gtins` (barcodes). A text query can also ask for a page: `request.skip` (where
to start) and `request.take` (how many; `None` means your own cap). Return `Collected(prices=..., offers=..., missing=...,
truncated=..., fetched=..., skipped=..., next_skip=...)`, where `next_skip` is where the next page starts
(set it only when there is more; a connector that cannot page ignores `skip`/`take` and leaves it empty). The kit provides everything else: it refuses an untargeted
or unsupported-channel scan before any request, enforces the per-scan budget, counts requests, maps
every failure to a standard status, and never lets a secret into a result.

Helpers: `self.client` (a `PoliteClient`: host allowlist, spacing, no redirects, size and time
limits; use `get_json` / `post_json`, or `get_text` for an HTML page), `self.require("SETTING")`,
`to_decimal()`, `valid_gtin()`, `json_ld(html)` (the schema.org objects a product page embeds),
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

## Protocol notes: what Willys and Lidl taught us

Writing Willys (a public JSON API with national prices) and Lidl (product pages only, no search)
tested whether the protocol is general. It held, and gained these, all optional and off by default:

| Difference | Handled by |
|---|---|
| A retailer with one national online assortment has no store directory | `stores` in the manifest: the service creates them on start-up and install, so nobody has to run discovery |
| The site publishes pages, not an API | `PoliteClient.get_text()` and `json_ld(html)` in the kit (the schema.org data a product page embeds) |
| A page's canonical address needs a name the id does not give | `follow_redirects` (same allowed host only) |
| The site cannot be searched, or cannot look up a barcode | The service now **asks only for what `capabilities` says**: a store without `product_search` answers a name search with `unsupported` (no request is made) and refuses a search monitor; one without `barcode_search` is asked about the SKUs already linked to a barcode (`unknown_mapping` when none are) |
| The retailer states when automated visits are welcome | `scheduled_window_utc` (background checks only; people's own requests are never delayed) |
| A product exists but shows no price | `Collected.notes` explains it ("in store only"); the product is `missing`, never a zero price |
| Member-only prices | Already supported: `OfferData.eligibility = "member"`; the shelf price is never replaced by a member price |
| The barcode appears only on the product detail, not in search results | Already supported: a lookup by id returns the `gtin`, which records the barcode (source `retailer`) |

Not (yet) modelled: prices that differ per store inside one online assortment (Willys can price some
items per store; the connector shows what an anonymous visitor sees), and weekly leaflet offers
(Lidl's grocery prices are only in the Lidl Plus app and the leaflet, which a bounded `store`
monitor could one day follow).

## Product links

A person can add a product by pasting its page link (`POST /v1/monitors/from-url`). The connector
makes that possible by declaring `product_urls`:

```yaml
product_urls:
  - pattern: '^https://(www\.)?citygross\.se/.+-p(?P<sku>\d+_[A-Za-z]{2})/?$'
    example: 'https://www.citygross.se/matvaror/frukt-och-gront/frukt/%C3%A4pple-ingrid-marie-p100142800_KG'
```

- The link is **parsed, never fetched**. The service only takes the product id from it, then looks
  that one product up through the connector like any other request (rate limits, pause and cache
  apply). A link can therefore never point the service at another host.
- The pattern runs on the *normalised* link: `https://` + lower-case host + percent-decoded path,
  with no query string, fragment, credentials or port. Anchor it (`^`, `$`) and capture the id in
  a named group `sku`, which must be the id your `collect()` accepts in `request.skus`. Only `https`
  links are accepted.
- Give a real `example`. The loader refuses a manifest whose pattern cannot read its own example,
  and the contract tests check that the pattern rejects unrelated links.
- The host does not have to be in `allowed_hosts` (Coop's pages are on `www.coop.se`, but only the
  API host is contacted), because the page is never requested.
- The link does not say which store, and prices differ by store, so the caller names a store (or the
  retailer has exactly one registered).

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
| Not installable | A placeholder with no implementation |

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

There are two places connectors live, with the same structure and the same loader:

| Location | What | In Docker |
|---|---|---|
| `connectors/` in the repository root | Bundled connectors, kept in git. Add a folder to add one, delete it to remove one | Copied into the image **and** bind-mounted read-only by `docker-compose.yml` (`./connectors:/service/connectors:ro,z`), so a folder you add or delete on the host takes effect without a rebuild |
| `INSTALLED_CONNECTORS_DIR` (default `./data/connectors`) | Connectors installed from the admin page | The `connectors_data` volume, shared by the API and the worker |

The API installs into the second location and the worker runs from both, so it must be shared
storage. Each process notices changes on disk within a couple of seconds, so no restart is needed.
The `z` on the bind mount is for SELinux hosts (Fedora, RHEL), which otherwise deny the container
access to the folder; other systems ignore it. An unreadable connectors directory is reported in
the admin page and never stops the service.

## Beyond HTTP and JSON

`HttpJsonConnector` covers the common case. Any class with an `id` and
`scan(external_store_id, channel, request) -> Snapshot` is a connector; implement it directly for
other transports and return the same typed `Snapshot` (`app/connectors/base.py`). Optional
`discover_stores(**inputs)` and `configure(values)` are used when present.

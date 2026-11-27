# Implementation status

The repository has an executable slice of Price Watch. It has a synthetic store (`test.fixture`) for tests and five live connectors, **City Gross** (`se.citygross`), **Coop** (`se.coop`), **Willys** (`se.willys`), **Dollarstore** (`se.dollarstore`) and **Lidl** (`se.lidl`, link-only), which make targeted, rate-limited requests. ICA has authorized use, and its connector (`se.ica`) is built but ships disabled and has not been run against the live shop (see below). The API and admin page are suitable for local evaluation, not a production launch.

The current worker polls PostgreSQL directly for durable jobs and delivery records. Redis and Celery from the original specification are not used yet; this keeps the first slice smaller while preserving queued work across restarts. The API readiness check requires the current Alembic schema revision.

## Run locally

Deploying on an **Unraid** server instead? Use the single compose file and env file in [deploy/unraid/](../deploy/unraid/README.md).

1. Copy `.env.example` to `.env`.
2. Set `POSTGRES_PASSWORD` and use the same password in `DATABASE_URL`. Set a random `APP_SECRET` of at least 32 characters, an `ADMIN_EMAIL`, and an `ADMIN_PASSWORD` of at least 16 characters.
3. Run `docker compose build` and `docker compose up -d`. If the `api` container keeps restarting (and `worker` never starts, because it waits for the API), run `docker compose logs api`: it lists exactly which `.env` settings are still placeholders or too weak. Postgres fixes its password the first time it starts, so if you change `POSTGRES_PASSWORD` afterwards, reset the (empty) database volume once with `docker compose down -v`.
4. Open `http://localhost:8085/admin` and sign in. API documentation is at `/docs`.

The admin page creates invitations, client credentials, and monitors, and shows connector state and recent jobs with manual retry. The invitation and client secret appear once; share or save them securely. A user accepts an invitation through `POST /v1/auth/accept-invitation`, then logs in through `POST /v1/auth/login`.

For app-to-app access, an admin creates a client credential at `POST /v1/clients`. The user grants that client a subset of scopes at `POST /v1/me/client-grants`. Client requests send both `Authorization: Bearer <user token>` and `X-Client-Token: <client token>`. Effective access is the intersection of user, client, and grant scopes. Revoking the grant removes client access to that user.

Clients can send `Idempotency-Key` with monitor creation, immediate check, and live search requests. Repeating the same request with that key returns the same resource. `GET /v1/events` supports `limit` and an `after` cursor so clients can reconcile missed webhooks.

Webhook deliveries are JSON bodies of the form `{"id": "<event id>", "type": "offer.detected", "occurred_at": "<ISO timestamp>", "data": {...}}`; this is simpler than the README's sketch (no `monitor_ids`, `retailer_id` or `store_id` at the top level yet).

Webhooks are configured per user at `POST /v1/webhooks`. The returned signing secret appears once. The sender signs `timestamp + "." + raw_json_body` with HMAC-SHA256 and sends `X-Retail-Timestamp`, `X-Retail-Delivery`, and `X-Retail-Signature: v1=<hex>`. Consumers should reject stale timestamps and deduplicate delivery IDs. HTTPS public destinations are accepted by default. For trusted home network destinations, configure `WEBHOOK_PRIVATE_HOSTS` and, only if needed, `ALLOW_HTTP_WEBHOOKS=true`.

## Using the API from your own app

[docs/api.md](api.md) is the full guide for developers and AI agents: access (users, tokens, API clients and grants), conventions (the error envelope, idempotency, the asynchronous job pattern), limits, every workflow with examples, events and signed webhooks, and an endpoint reference. A running service serves it at `/api-guide`, the machine-readable spec at `/openapi.json` (also committed as `docs/openapi.json`), an interactive reference at `/docs`, and a short index for agents at `/llms.txt`. [examples/shopper_client.py](../examples/shopper_client.py) is a small, tested Python client and worked demo. Tests fail if the guide drifts from the routes, scopes, limits, events or error codes.

## Example client and demo shop

Sign in on `/admin`, then open **the example client** (`/demo`). It opens a session as a regular, non-admin *example user* with a scoped API client (`POST /v1/admin/example-user`) and works only through the public API with a user token and a client token, so you see the service the way an app does: a watchlist with live prices and change badges, adding by link or by name, comparing a barcode across your stores, an activity feed, and an "API calls" tab listing every request. "Reset example data" clears what the example user did; the shared catalogue stays.

To see monitoring work without any real retailer there is a bundled synthetic connector, `test.demoshop`, with two stores (Demo Shop North and South). Its prices move on a short cycle, the oats have a multi-buy offer that switches on and off, and the stores sell the same barcodes, so price moves, alerts and cross-store comparison all happen offline. Demo link `https://demo.example.org/p/oats-1kg`; demo barcodes `9999990000019` and `9999990000026`.

## Searching

`POST /v1/search/jobs` asks the chosen stores (up to 5 per job; a client sends more stores as more jobs).

- **Fast by default.** Stores of different retailers are asked at the same time. Each store answers with its best matches first (30 by default), and a store that has more says where the next page starts (`next_skip`); the example client shows a "Show more results" button that sends `skip`. Swedish words (from the shared word list) are asked too only when a store's first answer has few matches. A search that runs past 45 seconds returns what it has, `partial`, naming the stores not reached.
- **Remembered for an hour.** A store's answer is reused for an hour; `fresh: true` (the "Refresh prices" button) asks again, and a watch's "Check now" always asks the store.
- **Progress.** While a search runs, `GET /v1/jobs/{id}` carries `progress` (`text`, `done`, `total`, for example "Searching City Gross Höör"). It is written to its own table (`job_progress`) on a separate connection, only on PostgreSQL, best effort, and forgotten after two hours.
- **Names and pictures.** Results carry the other names a product goes by (`also_called`), probable duplicates without a barcode (`likely_same`), offers, availability and stock where the store gives it, and a picture (from Open Food Facts, by barcode, fetched in the background; a person can add their own).

## Barcodes and comparing prices

- **Linking a barcode** (`POST /v1/monitors/{id}/barcode`): a product that a store publishes no barcode for can be given the one a person scanned, so it can be found by barcode and compared across stores. The link is shared by everyone, so its source is recorded (`retailer`, `user` or `admin`), a retailer-published barcode cannot be overridden, a wrong scan can be removed (`DELETE`), and a barcode that already belongs to another product is a `409` naming it: only `merge: true` merges the two (listings move; price history is kept). Store-internal codes (EAN-13 prefix 2 or 02, used for loose items) are refused.
- **Comparing in a shop** (`POST /v1/compare/barcode`): scan in shop A and find the same item in the stores you chose (`/v1/me/stores`, at most 15; at most 8 checked per scan). The response carries what the catalogue already knows, with its age, so a client can draw at once; a worker job then checks each store live (one request per store, spaced, cached, and counted against the per-user lookup budget shared with searches). Each row says `found`, `not_found`, `unknown`, `unknown_mapping` or a connector state; the result names the cheapest store and the saving versus where you stand. A store whose connector cannot search by barcode is asked about the SKUs already linked to that barcode.

## Using City Gross

City Gross prices and assortment are per store, and the API's `store` parameter is the store's `storeNumber`. All retailer requests are made by the worker, never by the API.

1. As an admin, queue a directory refresh: `POST /v1/admin/retailers/se.citygross/stores/discover` (one request). It upserts the stores (38 at the time of writing); list them with `GET /v1/stores`. Only registered stores can be used, because an unknown store number silently returns no products.
2. Search with `POST /v1/search/jobs` (`{"query": "havregryn", "store_ids": [...]}` or `{"gtin": "7310130321085", ...}`). City Gross publishes the GTIN, so barcode search works and creates a verified GTIN-to-product mapping. The same item at another City Gross store resolves to the same product.
3. **Add a product by its page link:** `POST /v1/monitors/from-url` with `{"url": "https://www.citygross.se/.../äpple-ingrid-marie-p100142800_KG", "store_id": "..."}` (optional `name`, `maximum_price`, `interval_minutes`). It returns a job; the worker looks that one product up and creates the product and its monitor together (the result holds `monitor_id` and the product). If you already monitor it you get the existing monitor back immediately (200). The link is parsed for the product id and never fetched. Prices differ by store, so send `store_id` unless the retailer has exactly one registered. Or create an `exact_product` monitor from a listing returned by a search, or a `generic_product` monitor with a text query. Store-wide (`store`) monitors are refused for live retailers. The minimum interval is 60 minutes.
4. **Remove a product:** `DELETE /v1/monitors/{id}` stops the monitor and takes it off your list but **keeps its events** (read it by id or with `GET /v1/monitors?include_removed=true`). `DELETE /v1/monitors/{id}?permanent=true` deletes the monitor and its events for good. The product's price history stays either way, and removing frees a slot under the monitor limit.
5. Monitors report price changes, thresholds, stock changes, and promotions attached to the products they match (`offer.detected` / `offer.changed`, with validity dates). Multi-buys such as "2 för 32 kr" are separate offers; a public single-item promotion becomes the effective price. Member-only promotions are recorded as `member` offers and never change the public price.

## Using Coop

Coop's data API requires a subscription key. Coop publishes no developer programme; the key is the one coop.se's own pages send to every visitor, so this is an **undocumented interface that may change or be withdrawn**, and it is the owner's decision to use it (2026-11-26). The key is configuration only.

1. Provide the key: enter it under the Coop connector's settings in the admin page (stored encrypted), or set `COOP_API_KEY` in `.env`. To find it, open coop.se in your browser, open the developer tools, and look at the `Ocp-Apim-Subscription-Key` request header on any request to `external.api.coop.se`. Never commit it. Without a key the connector shows "Needs configuration", Coop jobs finish as `auth_required`, and nothing is requested. If Coop rotates the key, requests return 401, the connector pauses (`access_blocked`), and you update the setting and resume.
2. Coop resolves **a postcode to the store that serves it**; there is no global store list. Register a store with `POST /v1/admin/retailers/se.coop/stores/discover` and `{"postcode": "11120"}`; it adds the store (for example `255700`, "Stora Coop Häggvik") for `GET /v1/stores`.
3. Search, barcode lookup and `exact_product` / `generic_product` monitors work as for City Gross. The EAN is both the SKU and the GTIN, and one request can look up many products, so an exact monitor costs one request per check. Prices are online prices for that store; the minimum monitor interval is 60 minutes.
4. **No offers.** Coop's anonymous catalogue marks products with deal *types* (`MULTI_BUY_FIXED_PRICE`, ...) but no amounts, and the site shows no deal on them. Deals are applied by the cart service. So Coop monitors report price and availability only, and nothing is invented.

## Using Willys

Willys' own pages load public JSON (`/search?q=` and `/axfood/rest/p/<code>`), so there is no key or login. All of Willys is **one national online store** ("Willys (online)", created automatically from the manifest; there is nothing to discover).

- **Add by name or by link.** Search `POST /v1/search/jobs`, or paste a product page link (`https://www.willys.se/produkt/...-101233933_ST`) into `POST /v1/monitors/from-url`. Items sold by weight (codes ending `_KG`) are priced per kilogram.
- **Barcodes.** Willys' search cannot look up a barcode, but a product's detail carries its EAN, so a product added by link or search has its barcode recorded. Comparing a barcode across stores asks Willys about the SKU already linked to it by id (a barcode never seen at Willys is `unknown_mapping` until someone adds the product there).
- **Offers.** "Klubbpris" (member price, e.g. "2 för 99 kr") is stored as an offer with `eligibility: member` and is never taken as the shelf price. A public single-item campaign price lowers the current price and records the regular one.
- **robots.txt.** Willys asks automated visitors for a 10 s delay (the request spacing is 10 s) and a 04:00-08:45 UTC visit window. Scheduled checks honour the window (a due monitor waits for it); a person's own search, add-by-link, comparison or "check now" is not scheduled work and is never delayed. The disallowed `/sok` page is never requested. At most 300 requests a day.
- **Not modelled.** Willys can price some items per store; this shows what an anonymous visitor sees.

## Using Lidl

Lidl is deliberately narrow, and the limits are Lidl's: lidl.se publishes no search or product API for this to use, and **its grocery prices are not on lidl.se at all** (they are in the Lidl Plus app and the weekly leaflet). What exists is an online shop, mostly non-food, whose product pages embed schema.org data.

- **Add by link only.** `POST /v1/monitors/from-url` with a page link such as `https://www.lidl.se/p/favorina-mandlar/p11000943` (one national store, "Lidl (online shop)"). Searching by name or barcode is declared unsupported, so the service never asks and says why; a search monitor is refused with a message to add products by link.
- **No online price is not a price.** Some pages state none (in store only, some out-of-stock items). Adding one reports "lists '…' without an online price (in store only or not for sale)" and creates nothing. A priced out-of-stock page is recorded with its price and `out_of_stock`.
- **Redirects.** The bare id redirects to the canonical address, so the manifest opts in to following a redirect to its own host (never elsewhere, at most 2 hops, each counted as a request: a lookup costs 2).
- **Barcodes.** A page's `gtin13` is recorded when the page has a price; unit prices are unknown (no size is published in a form we trust).

## Deploying on Unraid (Docker)

[deploy/unraid/](../deploy/unraid/) is one `docker-compose.yml` and one `.env.example` plus a guide: PostgreSQL, API, worker and a one-shot init that prepares `/mnt/user/appdata/retail-monitor` for Unraid's `nobody:users`. Four values are required (database password, `APP_SECRET`, admin email and password); the database URL is derived so it cannot disagree. It listens on the LAN (`BIND_ADDRESS`, `HTTP_PORT`), rotates logs, carries Unraid WebUI labels, and pulls `desertlee/price-watch:latest` from Docker Hub. Tested by building the image and starting the stack from this file, signing in, and making live Willys and Lidl lookups through it. `tests/test_deploy_files.py` keeps the files in step with the application's settings.

## Connectors

Connectors are self-contained packages (a folder with `manifest.yaml` and a Python module) that are installed, enabled, configured and removed from the admin page without touching the core. City Gross, Coop, Willys, Lidl, the fixture and the demo shop are bundled in the [`connectors/`](../connectors/README.md) folder at the repository root (add a folder to add one, delete it to remove one); others can be uploaded as zip packages and are stored in `INSTALLED_CONNECTORS_DIR`. See [docs/connectors.md](connectors.md) for the standard approach, the manifest, the authoring kit, the contract tests and the trust model, and `connectors/_template/` for a working example to copy.

## Request politeness

The aim is that the service never behaves like a crawler or an abusive client.

| Layer | Limit |
|---|---|
| Spacing | At least 3 s between requests to City Gross, one at a time (`minimum_request_interval_seconds` in the manifest) |
| Per scan | At most 6 requests (`max_requests_per_scan`); a name search asks each store for its best 30 matches first (`SEARCH_PAGE_SIZE`) and a person can ask for the next page ("Show more"); a connector never fetches more than 80 for one scan; a result limit is reported as `truncated` with `next_skip` |
| Per day | At most 400 requests per retailer per rolling 24 hours (`max_requests_per_day`), kept in the database so it holds across processes and restarts |
| Duplicates | Identical store/search/SKU requests within an hour share one fetch (`cache_seconds`, 3600; kept in the worker's memory). A person's "Refresh prices" (`fresh: true`) or "Check now" asks the store itself; scheduled checks use the kept answer |
| Parallel | A search asks stores of different retailers at the same time (each at its own spacing); stores of one retailer go one after another. The words are asked in Swedish too only when a store's first answer has few matches (`SEARCH_TRANSLATE_BELOW`), and a search stops asking after `SEARCH_TIME_BUDGET_SECONDS` (45) and names the stores it did not reach (`timed_out`) |
| Scheduling | Monitor checks are at least 60 minutes apart for City Gross, and next-due times get up to 10% random jitter so checks do not synchronise |
| Refusals | A 401/403/redirect pauses the connector for review (`access_blocked`); it is never retried or worked around. A 429 starts a cooldown that honours `Retry-After` (minimum 60 s, default 15 min). Five consecutive extraction failures pause it too. Resume with `POST /v1/admin/connectors/{id}/resume` |
| Network errors | Retried by the job queue with exponential backoff, at most 5 attempts |
| Who calls | Only the worker contacts retailers. The API queues jobs; store discovery is a job too |
| Own API | 300 requests/minute per credential (`API_RATE_LIMIT_PER_MINUTE`), 50 enabled monitors per user, 3 searches in flight and 20 per hour per user, and 60 s between manual checks of one monitor |
| Outbound safety | Host allowlist (`allowed_hosts`), HTTPS only, redirects refused, 15 s timeout, 5 MB response cap; secrets such as `COOP_API_KEY` are never logged, stored, or returned |

The user-agent defaults to a generic browser string (`CONNECTOR_USER_AGENT`). It is a compatibility header only; if a site refuses the client the answer is to stop, never to change how the client presents itself.

## Testing

`uv run pytest` runs the suite against SQLite. Set `TEST_DATABASE_URL=postgresql+psycopg://user:password@host:5432/db` to run it against PostgreSQL; use a scratch database, because the tests drop and recreate every table. SQLite ignores `FOR UPDATE SKIP LOCKED`, so the row-locking tests only run on PostgreSQL. `scripts/check.sh` runs lint, the SQLite suite, and then (in a throwaway Docker PostgreSQL) the suite again plus the migrations applied to an empty database and checked against the models; use `--fast` to skip the PostgreSQL part. GitHub Actions is deliberately not used (the workflow is manual-only). `tests/test_live_citygross.py` and `tests/test_live_coop.py` talk to the real sites (a handful of spaced requests each) and are skipped unless `RUN_LIVE=1` (Coop also needs `COOP_API_KEY`); run them by hand, never in CI.

## Implemented

- PostgreSQL schema with Alembic migration; SQLite is supported for local tests.
- Invite-only users with Argon2 passwords, a database-backed login failure limit, opaque revocable credentials, and redacted audit records for administrative changes.
- Scoped client tokens and per-user grants, plus ownership checks for monitors, jobs, events, and webhooks.
- Pluggable connectors: one folder per connector (built-in and installed alike), admin enable/disable and settings, upload/review/install/remove with automatic rollback, quarantine of broken packages, an authoring base class, a contract test kit and a template. Admin overrides, installed packages and encrypted settings are stored in the database; installed files live on a volume shared by the API and worker.
- Connector protocol with targeted requests (text query, SKUs, GTINs), typed result statuses and errors, a polite HTTP client, and a shared retrieval gate (pause, cooldown, daily budget, cache). City Gross and Coop are the live connectors.
- Manifest-loaded synthetic connector, queued name/verified-barcode search, catalogue listing, price and offer observations, exact/generic/store monitor evaluation. A fourth fixture connector is tested without a core registry edit.
- Package quantities, normalized unit prices, generic brand/exclusion/weight/unit-price filters, and immutable offer revision details.
- Database-backed due scheduling, job leases and recovery, retries, event keys, and webhook delivery history with manual retry of failed deliveries.
- Pause-on-block and cooldown state in the database, connector health with budget use, admin pause/resume, and queued store discovery.
- Admin page for initial setup, a locked Docker build, and a local check script (`scripts/check.sh`) for lint, tests, and migrations.

## Remaining before the specified MVP

- Willys per-store prices, and following Lidl's weekly leaflet offers (a bounded `store` monitor), if either becomes available as data.
- Trying the ICA connector against the live shop: read its robots.txt, confirm that a bare product id address resolves and that the embedded-state field names match the fixtures, set the spacing, then enable it.
- Coop deals, if Coop ever exposes amounts anonymously, and a documented Coop API key if Coop offers one.
- Verified store context beyond the directory (City Gross search responses do not echo the store), physical-store prices (only online prices are supported), and richer completeness checks.
- Broaden catalogue matching and offer mechanics (only `ItemsTotal` promotions are interpreted; other types are stored raw).
- Extend monitoring with grouped store notifications, offer withdrawal and expiry confirmation, broader generic attributes, and stronger overlapping-subscription delivery deduplication. Basic availability transitions and same-owner offer delivery deduplication exist.
- Extend cursor pagination and idempotency beyond the implemented event feed and monitor create/check routes; complete the remaining API surface and account/session flows.
- Harden deployment with health-based startup ordering, backups and restore tests, key rotation, metrics, and security testing of outbound requests.
- Add email and ntfy providers and richer admin management. The README places private retailer accounts and AI after the public MVP.

## Known deviations and limitations

These are deliberate simplifications or gaps in the current slice, not spec changes:

- **API shape.** Monitors and search jobs take a single `store_id` / `store_ids` and a single `channel`, not the README's `scope` object with `sales_channels` lists. Lists use fixed limits (50-200 rows) without cursors, except `GET /v1/events`. `DELETE /v1/webhooks/{id}` disables the webhook rather than removing it. `DELETE /v1/monitors/{id}` removes a monitor but keeps its events; `?permanent=true` deletes it.
- **Offer lifecycle.** Offers carry validity dates when the retailer gives them (City Gross does), but `offers.status` is always `active`; nothing marks offers upcoming, expired or withdrawn yet, and `offer.expiring` / `offer.expired` events do not exist.
- **Event de-duplication.** Offer events are de-duplicated across a user's overlapping monitors. Price and availability events are not, because their transition key includes the observation time; two monitors on one listing deliver two webhooks.
- **Webhook SSRF.** The destination is validated at registration and again before each send, but the HTTP client resolves DNS itself, so a rebinding host could pass the check. Pin the validated address or use an egress proxy before exposing this beyond a trusted home network.
- **Abuse limits.** Login lockout is per email address, so an attacker can lock out a known account for 15 minutes. The API rate limiter and the retailer request spacing/cache are in-process, so they assume one API process and one worker (as in `docker-compose.yml`); the daily budget, pause and cooldown are in the database and hold regardless. Behind a reverse proxy all unauthenticated callers share the proxy's address unless uvicorn is configured to trust forwarded headers.
- **Retailer-asserted GTINs.** A GTIN the retailer publishes with a valid check digit is stored as a verified mapping without further review. A wrong retailer GTIN would link products incorrectly; correct it with the admin verified-GTIN route.
- **Stock status.** City Gross `stockStatus` code 1 is mapped to `in_stock`; other codes are unknown and recorded as no availability, so no stock transition is emitted for them.
- **Comparison limits.** A comparison reports a store's regular single-item price (a public single-item promotion is included); multi-buy deals, member prices and in-store prices are not factored in. A job returns its rows when it finishes, not store by store. Barcode links are shared and not versioned: an undo removes the barcode but does not split a merge.
- **Connector code is not sandboxed.** An installed connector runs in the API and worker processes with their privileges. The allowlist and politeness limits apply to the standard HTTP client only, and an upload is checked statically (not executed) before install; the administrator is trusted to install only packages they trust. See [docs/connectors.md](connectors.md#trust-and-security).
- **Reloading.** Each process notices installed, updated or removed connectors within about two seconds. Python cannot unload a module, so a replaced or removed connector's old code stays in memory until the process restarts (it is no longer used). The enabled state and settings are read from the database on use, so they take effect immediately.
- **Single worker.** Request spacing, the result cache and the API rate limiter are per process; run one worker and one API process as the compose file does.
- **Secrets.** Webhook signing secrets and connector settings are encrypted with a key derived from `APP_SECRET`; changing `APP_SECRET` makes existing webhooks undeliverable until they are recreated. There is no key rotation.
- **Scan atomicity.** A scan with invalid or duplicate items stores the valid ones and reports `partial`. It does not complete the baseline or advance `last_success_at`, so the next scan is still treated as a baseline; valid items are otherwise processed under the normal event rules.

## Retailer access findings

Policy: see the README's [access policy](../readme.md#access-policy). A terms review is not a gate; the connector must stay targeted and human-scale.

### ICA (investigated 2026-11-26)

- **Where the data is.** ICA's online shop is an Ocado-based storefront at `https://handlaprivatkund.ica.se/stores/<storeId>/`. Search is `.../search?q=<text>`; a product is `.../products/<slug>/<productId>`. Store `1003395` is "ICA Supermarket Höör".
- **What a page contains.** Pages are server-rendered with the data embedded as `window.__INITIAL_STATE__`: product records with `retailerProductId` (the ICA SKU), name, brand, availability, category path, `size` (e.g. `0.75kg`), current price and unit price, and promotions (e.g. "2 för 45 kr" with a stable `retailerPromotionId`). The store identity is `session.metadata.retailerRegionId` and `regionName`. Product pages also carry schema.org JSON-LD (`sku`, price, availability). No barcode/GTIN appears on either, so ICA barcode search is unsupported; GTIN stays a manual mapping to an ICA SKU.
- **Authorization.** ICA has approved use of its website and online food catalogs for price retrieval by Price Watch (approval reported by the owner, 2026-11-27). The connector (`connectors/se.ica`) is built from fixtures shaped like the findings above, supports search, lookup by id or pasted link, and multibuy offers ("2 för 45 kr") for one store (default `1003395`), and ships disabled. It has not been run live (this build environment cannot reach ICA), so the field names inside the embedded state and the bare-id product address are assumptions to confirm.
- **Approved method (2026-11-27).** ICA's IT team: no API; request the pages with browser-style headers (their example is in the manifest's `request_headers`) and read prices from the HTML. Their example URL is `www.ica.se/produkt/`.
- **Saved pages (2026-11-27).** (1) `www.ica.se/produkt/<slug>-<id>/` and its listing show only *campaign* prices when no store is chosen ("2 för 25 kr"; cards carry `data-id`, `data-price`, `data-quantity`, with price 0 meaning none); there is no regular price, JSON-LD or barcode, so the connector does not use that host. (2) The store shop (`handlaprivatkund.ica.se/stores/1003395`) renders products as tiles: `data-test="fop-title"`, `fop-price` (what the shopper pays), `fop-original-price` (`aria-label="Ordinarie Pris 14,90 kr"`), `fop-offer-text`, `fop-size`, `fop-price-per-unit`, and a link `/stores/<store>/products/<slug>/<sku>` (the SKU equals the `www.ica.se` article id). A real tile is the fixture `tests/fixtures/ica_tile.html`. A saved search page (`/stores/1003395/search?q=mjölk`, fixture `tests/fixtures/ica_search.html`) confirms the search URL and shows the same tiles (price like `20,93 kr`, size `1.5L`, unit price `(13,95 kr/l)`); the first save held only 4 products and a later save of the same search 17 (so a search shows what the page rendered, and more load as the page is scrolled). A multibuy tile (`offer-loyalty`) shows `fop-price` as the bundle, "3 för 50 kr", with the single-item price only in the "Ordinarie Pris 21,60 kr/st" label: the connector takes the ordinary price as the current price and the bundle as a member offer. A saved product page (`/stores/1003395/products/<name>/1407322`, fixture `tests/fixtures/ica_product.html`) shows the main product outside the tiles: `<h1>`, `data-test="size-container"` (spans like `1L`, `(14,42 kr/l)`) and `data-test="price-container"` (`14,42 kr`), under `data-test="bop-view"`; the carousels below it (`related-products-carousel`) are tiles of other products. The page does not repeat its own SKU (it is in the address), and the price agreed with the same product in the search. The bare-id address `/stores/<store>/products/<id>` (no name) redirects to the named page, as the owner confirmed in a browser (2026-11-27); the connector follows that redirect. Still unverified: a product page with a promotion, a non-member multibuy tile, and the raw HTML ICA returns to a non-browser client.
- **Not offered.** Member prices (`stammispris`) need a login and are out of scope for now.

### City Gross (investigated 2026-11-26; connector enabled)

- **API.** citygross.se's own first-party JSON API, answered to a plain HTTP client with no challenge: `GET /api/v1/sites?siteTypeId=3` (store directory: `id`, `name`, `storeNumber`, address) and `GET /api/v1/Loop54/search?SearchQuery=<text|id|gtin>&skip=&take=&store=<storeNumber>` (search, with `searchResults.products` and `totalCount`).
- **Data.** Each product has `id` (e.g. `100132321_ST`), `gtin`, `name`, `subtitle`, `brand`, `descriptiveSize`, and per-store `productStoreDetails.prices` with current and ordinary price, comparative (unit) price, promotions (`effectType`, `minQuantity`, `value`, `membersOnly`, `from`/`to`) and `lowestPriceLast30Days`. `stockStatus` is `1` for in stock.
- **Quirks.** `store` takes `storeNumber`, not `id`, and an unknown store yields zero results rather than an error. `netContent.unitOfMeasure` is 0 for weights and volumes alike, so package size comes from `descriptiveSize`. `currentPrice` excludes promotions. `totalCount` can exceed the number of products actually returned (25 vs 22), so a short page, not the count, marks the end.
- **Sold by weight.** Ids ending `_KG` (loose fruit and vegetables) are priced per kilogram (`currentPrice.unit` is `KGM`), and `descriptiveSize` (for example `CA140G`) is roughly one piece. They are recorded with a 1 kg basis so the price and unit price read as kr/kg.
- **Not used.** Category and "discount only" listing endpoints exist but would enumerate the catalogue, which the access policy rules out.

### Willys (investigated 2026-11-26; connector enabled)

- **API.** The pages call their own JSON, answered to a plain client with no challenge or key: `GET /search?q=&size=&page=` (30 per page; `pagination.numberOfPages`) and `GET /axfood/rest/p/<code>` (one product, with `ean`). `GET /axfood/rest/store` lists 261 stores but anonymous prices are not selected per store here.
- **robots.txt.** `Crawl-delay: 10`, `Visit-time: 0400-0845` (UTC), and disallows `/sok`, `/o/`, basket and account paths. All honoured (see "Using Willys").
- **Data.** `code` (e.g. `101233933_ST`), `name`, `manufacturer`, `displayVolume`, `priceValue` (shelf price), `comparePrice`, `outOfStock`, `online`, and `potentialPromotions` (`campaignType: LOYALTY` is the member price; `qualifyingCount`, `rewardLabel`, `validUntil` in epoch milliseconds). The EAN is only on the detail endpoint. A barcode as the search text returns nothing.

### Lidl (investigated 2026-11-26; connector enabled, link-only)

- **What exists.** An online shop whose product pages (`/p/<name>/p<id>`, listed in the product sitemap) embed JSON-LD `Product` data: `sku`, `gtin13` (list), `offers.price`, `offers.availability` (`InStock`, `OutOfStock`, `InStoreOnly`). The offers pages are marketing; the folder/leaflet is not published as data; `/q/api/search` rejected our parameters and robots.txt disallows `/q/search?id=*`, so no search is built.
- **Gaps.** `InStoreOnly` pages and some out-of-stock pages have no price. Grocery prices are not on lidl.se.

### Coop (investigated 2026-11-26; connector enabled, needs `COOP_API_KEY`)

- **API.** The web pages call `external.api.coop.se`, which needs an `Ocp-Apim-Subscription-Key` header (Azure API Management). The key is embedded in the site and sent to every visitor; no official developer programme exists. A plain HTTP client with that key and a generic user-agent is accepted. The API host serves no `robots.txt`; `www.coop.se/robots.txt` disallows crawling `/handla/sok/*` pages, which the connector never fetches.
- **Endpoints used.** `GET /ecommerce/coop/coopstore?postCode=` (postcode to serving store `code`/`name`; unknown postcode gives HTTP 400 `StoreNotFoundError`), `POST /personalization/search/products` (body `{query, resultsOptions{skip,take,...}}`, `store` query parameter) and `POST /personalization/search/entities/by-id` (body is a JSON array of EANs).
- **Data.** Items have `ean` (also the id), `name`, `manufacturerName`, `packageSize`/`packageSizeUnit`/`packageSizeInformation`, `salesPriceData.b2cPrice` (consumer price; `b2bPrice` is ignored), `comparativePriceData` and `availableOnline`. Size labels vary ("1500 g", "1 liter", "4x1.5L"); a multipack with an unreadable label has unknown size rather than a guessed unit price. EANs starting with 2 are store-internal codes and are not matched across stores.
- **Not available anonymously.** Deal amounts (only deal types), stock levels (`availableOnline` is orderability, so `false` is recorded as unknown), regular/reference prices, and member prices.
- **Not used.** Category tree and "current offers" listings would enumerate the catalogue, which the access policy rules out.

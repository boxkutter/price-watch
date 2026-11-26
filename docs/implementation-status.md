# Implementation status

The repository has an executable slice of Retail Monitor. It has a synthetic store (`test.fixture`) for tests and two live connectors, **City Gross** (`se.citygross`) and **Coop** (`se.coop`), which make targeted, rate-limited requests. ICA is blocked (see below). The API and admin page are suitable for local evaluation, not a production launch.

The current worker polls PostgreSQL directly for durable jobs and delivery records. Redis and Celery from the original specification are not used yet; this keeps the first slice smaller while preserving queued work across restarts. The API readiness check requires the current Alembic schema revision.

## Run locally

1. Copy `.env.example` to `.env`.
2. Set `POSTGRES_PASSWORD` and use the same password in `DATABASE_URL`. Set a random `APP_SECRET` of at least 32 characters, an `ADMIN_EMAIL`, and an `ADMIN_PASSWORD` of at least 16 characters.
3. Run `docker compose build` and `docker compose up -d`.
4. Open `http://localhost:8085/admin` and sign in. API documentation is at `/docs`.

The admin page creates invitations, client credentials, and monitors, and shows connector state and recent jobs with manual retry. The invitation and client secret appear once; share or save them securely. A user accepts an invitation through `POST /v1/auth/accept-invitation`, then logs in through `POST /v1/auth/login`.

For app-to-app access, an admin creates a client credential at `POST /v1/clients`. The user grants that client a subset of scopes at `POST /v1/me/client-grants`. Client requests send both `Authorization: Bearer <user token>` and `X-Client-Token: <client token>`. Effective access is the intersection of user, client, and grant scopes. Revoking the grant removes client access to that user.

Clients can send `Idempotency-Key` with monitor creation, immediate check, and live search requests. Repeating the same request with that key returns the same resource. `GET /v1/events` supports `limit` and an `after` cursor so clients can reconcile missed webhooks.

Webhook deliveries are JSON bodies of the form `{"id": "<event id>", "type": "offer.detected", "occurred_at": "<ISO timestamp>", "data": {...}}`; this is simpler than the README's sketch (no `monitor_ids`, `retailer_id` or `store_id` at the top level yet).

Webhooks are configured per user at `POST /v1/webhooks`. The returned signing secret appears once. The sender signs `timestamp + "." + raw_json_body` with HMAC-SHA256 and sends `X-Retail-Timestamp`, `X-Retail-Delivery`, and `X-Retail-Signature: v1=<hex>`. Consumers should reject stale timestamps and deduplicate delivery IDs. HTTPS public destinations are accepted by default. For trusted home network destinations, configure `WEBHOOK_PRIVATE_HOSTS` and, only if needed, `ALLOW_HTTP_WEBHOOKS=true`.

## Using City Gross

City Gross prices and assortment are per store, and the API's `store` parameter is the store's `storeNumber`. All retailer requests are made by the worker, never by the API.

1. As an admin, queue a directory refresh: `POST /v1/admin/retailers/se.citygross/stores/discover` (one request). It upserts the stores (38 at the time of writing); list them with `GET /v1/stores`. Only registered stores can be used, because an unknown store number silently returns no products.
2. Search with `POST /v1/search/jobs` (`{"query": "havregryn", "store_ids": [...]}` or `{"gtin": "7310130321085", ...}`). City Gross publishes the GTIN, so barcode search works and creates a verified GTIN-to-product mapping. The same item at another City Gross store resolves to the same product.
3. Create an `exact_product` monitor from a listing returned by the search, or a `generic_product` monitor with a text query. Store-wide (`store`) monitors are refused for live retailers. The minimum interval is 60 minutes.
4. Monitors report price changes, thresholds, stock changes, and promotions attached to the products they match (`offer.detected` / `offer.changed`, with validity dates). Multi-buys such as "2 för 32 kr" are separate offers; a public single-item promotion becomes the effective price. Member-only promotions are recorded as `member` offers and never change the public price.

## Using Coop

Coop's data API requires a subscription key. Coop publishes no developer programme; the key is the one coop.se's own pages send to every visitor, so this is an **undocumented interface that may change or be withdrawn**, and it is the owner's decision to use it (2026-11-26). The key is configuration only.

1. Set `COOP_API_KEY` in `.env`. To find it, open coop.se in your browser, open the developer tools, and look at the `Ocp-Apim-Subscription-Key` request header on any request to `external.api.coop.se`. Never commit it. Without a key, Coop jobs finish as `auth_required` and nothing is requested. If Coop rotates the key, requests return 401, the connector pauses (`access_blocked`), and you update `.env` and resume.
2. Coop resolves **a postcode to the store that serves it**; there is no global store list. Register a store with `POST /v1/admin/retailers/se.coop/stores/discover` and `{"postcode": "11120"}`; it adds the store (for example `255700`, "Stora Coop Häggvik") for `GET /v1/stores`.
3. Search, barcode lookup and `exact_product` / `generic_product` monitors work as for City Gross. The EAN is both the SKU and the GTIN, and one request can look up many products, so an exact monitor costs one request per check. Prices are online prices for that store; the minimum monitor interval is 60 minutes.
4. **No offers.** Coop's anonymous catalogue marks products with deal *types* (`MULTI_BUY_FIXED_PRICE`, ...) but no amounts, and the site shows no deal on them. Deals are applied by the cart service. So Coop monitors report price and availability only, and nothing is invented.

## Request politeness

The aim is that the service never behaves like a crawler or an abusive client.

| Layer | Limit |
|---|---|
| Spacing | At least 3 s between requests to City Gross, one at a time (`minimum_request_interval_seconds` in the manifest) |
| Per scan | At most 6 requests (`max_requests_per_scan`); a search fetches at most 80 results; a result limit is reported as `truncated` |
| Per day | At most 400 requests per retailer per rolling 24 hours (`max_requests_per_day`), kept in the database so it holds across processes and restarts |
| Duplicates | Identical store/search/SKU requests within 10 minutes share one fetch (`cache_seconds`) |
| Scheduling | Monitor checks are at least 60 minutes apart for City Gross, and next-due times get up to 10% random jitter so checks do not synchronise |
| Refusals | A 401/403/redirect pauses the connector for review (`access_blocked`); it is never retried or worked around. A 429 starts a cooldown that honours `Retry-After` (minimum 60 s, default 15 min). Five consecutive extraction failures pause it too. Resume with `POST /v1/admin/connectors/{id}/resume` |
| Network errors | Retried by the job queue with exponential backoff, at most 5 attempts |
| Who calls | Only the worker contacts retailers. The API queues jobs; store discovery is a job too |
| Own API | 300 requests/minute per credential (`API_RATE_LIMIT_PER_MINUTE`), 50 enabled monitors per user, 3 searches in flight and 20 per hour per user, and 60 s between manual checks of one monitor |
| Outbound safety | Host allowlist (`allowed_hosts`), HTTPS only, redirects refused, 15 s timeout, 5 MB response cap; secrets such as `COOP_API_KEY` are never logged, stored, or returned |

The user-agent defaults to a generic browser string (`CONNECTOR_USER_AGENT`). It is a compatibility header only; if a site refuses the client the answer is to stop, never to change how the client presents itself.

## Testing

`uv run pytest` runs the suite against SQLite. Set `TEST_DATABASE_URL=postgresql+psycopg://user:password@host:5432/db` to run it against PostgreSQL; use a scratch database, because the tests drop and recreate every table. SQLite ignores `FOR UPDATE SKIP LOCKED`, so the row-locking tests only run on PostgreSQL. CI runs both, and also applies the migrations to an empty PostgreSQL database and checks they match the models. `tests/test_live_citygross.py` and `tests/test_live_coop.py` talk to the real sites (a handful of spaced requests each) and are skipped unless `RUN_LIVE=1` (Coop also needs `COOP_API_KEY`); run them by hand, never in CI.

## Implemented

- PostgreSQL schema with Alembic migration; SQLite is supported for local tests.
- Invite-only users with Argon2 passwords, a database-backed login failure limit, opaque revocable credentials, and redacted audit records for administrative changes.
- Scoped client tokens and per-user grants, plus ownership checks for monitors, jobs, events, and webhooks.
- Connector protocol with targeted requests (text query, SKUs, GTINs), typed result statuses and errors, a polite HTTP client, and a shared retrieval gate (pause, cooldown, daily budget, cache). City Gross and Coop are the live connectors.
- Manifest-loaded synthetic connector, queued name/verified-barcode search, catalogue listing, price and offer observations, exact/generic/store monitor evaluation. A fourth fixture connector is tested without a core registry edit.
- Package quantities, normalized unit prices, generic brand/exclusion/weight/unit-price filters, and immutable offer revision details.
- Database-backed due scheduling, job leases and recovery, retries, event keys, and webhook delivery history with manual retry of failed deliveries.
- Pause-on-block and cooldown state in the database, connector health with budget use, admin pause/resume, and queued store discovery.
- Admin page for initial setup, a locked Docker build, and CI checks for lint, tests, and migration.

## Remaining before the specified MVP

- ICA, if its block is ever lifted, within the README's [access policy](../readme.md#access-policy): targeted product search and monitoring only. ICA is blocked; see "Retailer access findings".
- Coop deals, if Coop ever exposes amounts anonymously, and a documented Coop API key if Coop offers one.
- Verified store context beyond the directory (City Gross search responses do not echo the store), physical-store prices (only online prices are supported), and richer completeness checks.
- Broaden catalogue matching and offer mechanics (only `ItemsTotal` promotions are interpreted; other types are stored raw).
- Extend monitoring with grouped store notifications, offer withdrawal and expiry confirmation, broader generic attributes, and stronger overlapping-subscription delivery deduplication. Basic availability transitions and same-owner offer delivery deduplication exist.
- Extend cursor pagination and idempotency beyond the implemented event feed and monitor create/check routes; complete the remaining API surface and account/session flows.
- Harden deployment with health-based startup ordering, backups and restore tests, key rotation, metrics, and security testing of outbound requests.
- Add email and ntfy providers and richer admin management. The README places private retailer accounts and AI after the public MVP.

## Known deviations and limitations

These are deliberate simplifications or gaps in the current slice, not spec changes:

- **API shape.** Monitors and search jobs take a single `store_id` / `store_ids` and a single `channel`, not the README's `scope` object with `sales_channels` lists. Lists use fixed limits (50-200 rows) without cursors, except `GET /v1/events`. `DELETE /v1/monitors/{id}` and `DELETE /v1/webhooks/{id}` disable the resource rather than removing it.
- **Offer lifecycle.** Offers carry validity dates when the retailer gives them (City Gross does), but `offers.status` is always `active`; nothing marks offers upcoming, expired or withdrawn yet, and `offer.expiring` / `offer.expired` events do not exist.
- **Event de-duplication.** Offer events are de-duplicated across a user's overlapping monitors. Price and availability events are not, because their transition key includes the observation time; two monitors on one listing deliver two webhooks.
- **Webhook SSRF.** The destination is validated at registration and again before each send, but the HTTP client resolves DNS itself, so a rebinding host could pass the check. Pin the validated address or use an egress proxy before exposing this beyond a trusted home network.
- **Abuse limits.** Login lockout is per email address, so an attacker can lock out a known account for 15 minutes. The API rate limiter and the retailer request spacing/cache are in-process, so they assume one API process and one worker (as in `docker-compose.yml`); the daily budget, pause and cooldown are in the database and hold regardless. Behind a reverse proxy all unauthenticated callers share the proxy's address unless uvicorn is configured to trust forwarded headers.
- **Retailer-asserted GTINs.** A GTIN the retailer publishes with a valid check digit is stored as a verified mapping without further review. A wrong retailer GTIN would link products incorrectly; correct it with the admin verified-GTIN route.
- **Stock status.** City Gross `stockStatus` code 1 is mapped to `in_stock`; other codes are unknown and recorded as no availability, so no stock transition is emitted for them.
- **Secrets.** Webhook signing secrets are encrypted with a key derived from `APP_SECRET`; changing `APP_SECRET` makes existing webhooks undeliverable until they are recreated. There is no key rotation.
- **Scan atomicity.** A scan with invalid or duplicate items stores the valid ones and reports `partial`. It does not complete the baseline or advance `last_success_at`, so the next scan is still treated as a baseline; valid items are otherwise processed under the normal event rules.

## Retailer access findings

Policy: see the README's [access policy](../readme.md#access-policy). A terms review is not a gate; the connector must stay targeted and must not circumvent blocks.

### ICA (investigated 2026-11-26)

- **Where the data is.** ICA's online shop is an Ocado-based storefront at `https://handlaprivatkund.ica.se/stores/<storeId>/`. Search is `.../search?q=<text>`; a product is `.../products/<slug>/<productId>`. Store `1003395` is "ICA Supermarket Höör".
- **What a page contains.** Pages are server-rendered with the data embedded as `window.__INITIAL_STATE__`: product records with `retailerProductId` (the ICA SKU), name, brand, availability, category path, `size` (e.g. `0.75kg`), current price and unit price, and promotions (e.g. "2 för 45 kr" with a stable `retailerPromotionId`). The store identity is `session.metadata.retailerRegionId` and `regionName`. Product pages also carry schema.org JSON-LD (`sku`, price, availability). No barcode/GTIN appears on either, so ICA barcode search is unsupported; GTIN stays a manual mapping to an ICA SKU.
- **Access.** A normal Chrome session loads all of this anonymously. Automated access is only partly possible. A plain HTTP client, or a browser opening the shop's URLs directly, gets a `403` from the CloudFront/AWS WAF edge. A default headless Chromium that enters through `ica.se/handla`, declines cookies and chooses a store via the picker (postcode, "Hämta i butik") reaches the shop, verifies the store (`session.metadata.regionName`) and gets search suggestions. However, the product-data request itself (`/stores/<id>/api/webproductpagews/v6/product-pages/search`) returns `403` and the page shows its own error, even when search is driven through the shop's own search box. The same request succeeds in a normal Chrome session. This is a bot-control rule on the product data for automated sessions, and getting past it would mean disguising the automation, which the access policy rules out. **ICA live retrieval is therefore blocked and not enabled.**
- **Not offered.** Member prices (`stammispris`) need a login and are out of scope for now.

### City Gross (investigated 2026-11-26; connector enabled)

- **API.** citygross.se's own first-party JSON API, answered to a plain HTTP client with no challenge: `GET /api/v1/sites?siteTypeId=3` (store directory: `id`, `name`, `storeNumber`, address) and `GET /api/v1/Loop54/search?SearchQuery=<text|id|gtin>&skip=&take=&store=<storeNumber>` (search, with `searchResults.products` and `totalCount`).
- **Data.** Each product has `id` (e.g. `100132321_ST`), `gtin`, `name`, `subtitle`, `brand`, `descriptiveSize`, and per-store `productStoreDetails.prices` with current and ordinary price, comparative (unit) price, promotions (`effectType`, `minQuantity`, `value`, `membersOnly`, `from`/`to`) and `lowestPriceLast30Days`. `stockStatus` is `1` for in stock.
- **Quirks.** `store` takes `storeNumber`, not `id`, and an unknown store yields zero results rather than an error. `netContent.unitOfMeasure` is 0 for weights and volumes alike, so package size comes from `descriptiveSize`. `currentPrice` excludes promotions. `totalCount` can exceed the number of products actually returned (25 vs 22), so a short page, not the count, marks the end.
- **Not used.** Category and "discount only" listing endpoints exist but would enumerate the catalogue, which the access policy rules out.

### Coop (investigated 2026-11-26; connector enabled, needs `COOP_API_KEY`)

- **API.** The web pages call `external.api.coop.se`, which needs an `Ocp-Apim-Subscription-Key` header (Azure API Management). The key is embedded in the site and sent to every visitor; no official developer programme exists. A plain HTTP client with that key and a generic user-agent is accepted. The API host serves no `robots.txt`; `www.coop.se/robots.txt` disallows crawling `/handla/sok/*` pages, which the connector never fetches.
- **Endpoints used.** `GET /ecommerce/coop/coopstore?postCode=` (postcode to serving store `code`/`name`; unknown postcode gives HTTP 400 `StoreNotFoundError`), `POST /personalization/search/products` (body `{query, resultsOptions{skip,take,...}}`, `store` query parameter) and `POST /personalization/search/entities/by-id` (body is a JSON array of EANs).
- **Data.** Items have `ean` (also the id), `name`, `manufacturerName`, `packageSize`/`packageSizeUnit`/`packageSizeInformation`, `salesPriceData.b2cPrice` (consumer price; `b2bPrice` is ignored), `comparativePriceData` and `availableOnline`. Size labels vary ("1500 g", "1 liter", "4x1.5L"); a multipack with an unreadable label has unknown size rather than a guessed unit price. EANs starting with 2 are store-internal codes and are not matched across stores.
- **Not available anonymously.** Deal amounts (only deal types), stock levels (`availableOnline` is orderability, so `false` is recorded as unknown), regular/reference prices, and member prices.
- **Not used.** Category tree and "current offers" listings would enumerate the catalogue, which the access policy rules out.

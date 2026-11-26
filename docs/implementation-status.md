# Implementation status

The repository now has an executable first slice of Retail Monitor. It uses a synthetic store (`test.fixture`) and does not contact any retailer. Its API and admin page are suitable for local evaluation, not a production launch.

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

## Testing

`uv run pytest` runs the suite against SQLite. Set `TEST_DATABASE_URL=postgresql+psycopg://user:password@host:5432/db` to run it against PostgreSQL; use a scratch database, because the tests drop and recreate every table. SQLite ignores `FOR UPDATE SKIP LOCKED`, so the row-locking tests only run on PostgreSQL. CI runs both, and also applies the migrations to an empty PostgreSQL database and checks they match the models.

## Implemented

- PostgreSQL schema with Alembic migration; SQLite is supported for local tests.
- Invite-only users with Argon2 passwords, a database-backed login failure limit, opaque revocable credentials, and redacted audit records for administrative changes.
- Scoped client tokens and per-user grants, plus ownership checks for monitors, jobs, events, and webhooks.
- Manifest-loaded synthetic connector, queued name/verified-barcode search, catalogue listing, price and offer observations, exact/generic/store monitor evaluation. A fourth fixture connector is tested without a core registry edit.
- Package quantities, normalized unit prices, generic brand/exclusion/weight/unit-price filters, and immutable offer revision details.
- Database-backed due scheduling, job leases and recovery, retries, event keys, and webhook delivery history with manual retry of failed deliveries.
- Admin page for initial setup, a locked Docker build, and CI checks for lint, tests, and migration.

## Remaining before the specified MVP

- Verify permitted access and actual data coverage for ICA, City Gross, and Coop. Their connectors stay disabled until this is done. Public pages show offers, but that alone does not establish permission to automate retrieval or completeness of an offer feed.
- Implement the full connector protocol, store discovery, authorized live retrieval, request rate limits, and per-source completeness checks.
- Extend fixture-backed queued search to verified live connectors and barcode lookup; broaden catalogue matching and offer mechanics. Manual verified GTIN mapping and basic unit normalization exist.
- Extend monitoring with grouped store notifications, offer withdrawal and expiry confirmation, broader generic attributes, and stronger overlapping-subscription delivery deduplication. Basic availability transitions and same-owner offer delivery deduplication exist.
- Extend cursor pagination and idempotency beyond the implemented event feed and monitor create/check routes; complete the remaining API surface and account/session flows.
- Harden deployment with health-based startup ordering, backups and restore tests, key rotation, metrics, and security testing of outbound requests.
- Add email and ntfy providers and richer admin management. The README places private retailer accounts and AI after the public MVP.

## Known deviations and limitations

These are deliberate simplifications or gaps in the current slice, not spec changes:

- **API shape.** Monitors and search jobs take a single `store_id` / `store_ids` and a single `channel`, not the README's `scope` object with `sales_channels` lists. Lists use fixed limits (50-200 rows) without cursors, except `GET /v1/events`. `DELETE /v1/monitors/{id}` and `DELETE /v1/webhooks/{id}` disable the resource rather than removing it.
- **Offer lifecycle.** `offers.status` is always `active`; nothing marks offers upcoming, expired or withdrawn yet, and offers have no validity dates.
- **Event de-duplication.** Offer events are de-duplicated across a user's overlapping monitors. Price and availability events are not, because their transition key includes the observation time; two monitors on one listing deliver two webhooks.
- **Webhook SSRF.** The destination is validated at registration and again before each send, but the HTTP client resolves DNS itself, so a rebinding host could pass the check. Pin the validated address or use an egress proxy before exposing this beyond a trusted home network.
- **Abuse limits.** Login lockout is per email address, so an attacker can lock out a known account for 15 minutes. There are no per-user quotas on search jobs or monitors, and no per-retailer request rate limiting (the manifest field is parsed but not enforced).
- **Secrets.** Webhook signing secrets are encrypted with a key derived from `APP_SECRET`; changing `APP_SECRET` makes existing webhooks undeliverable until they are recreated. There is no key rotation.
- **Scan atomicity.** A scan with invalid or duplicate items stores the valid ones and reports `partial`. It does not complete the baseline or advance `last_success_at`, so the next scan is still treated as a baseline; valid items are otherwise processed under the normal event rules.

## Retailer access review

As of 2026-11-26, no authorization or verified automation endpoint was supplied. The following public pages demonstrate that offers are published, but do not grant automated access: [ICA offers](https://www.ica.se/butiker/erbjudanden/), [City Gross offers](https://www.citygross.se/), and [Coop user terms](https://www.coop.se/coop-kundservice/anvandarvillkor/). An [ICA store offer page](https://www.ica.se/erbjudanden/maxi-ica-stormarknad-alingsas-1003647/) reports 272 offers while its initial rendered content exposes only a subset, so a connector must prove pagination completeness before treating absence as meaningful. [Coop explains](https://kundservice.coop.se/hc/sv/articles/360016692059-Aktuella-erbjudanden-kampanjpriser-och-medlemspriser) that store and online offers can differ. Do not enable a live connector based solely on these pages.

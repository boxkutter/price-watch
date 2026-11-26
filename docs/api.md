# Retail Monitor API guide

For developers and AI agents building an app on top of the service (a phone app, a shopping
script, a dashboard). It covers everything a client needs: getting access, the conventions, the
limits, the workflows with examples, events and webhooks, and a full endpoint reference.

- **Interactive reference:** `/docs` (Swagger UI) on the running service
- **Machine-readable spec:** `/openapi.json` (also committed as [`openapi.json`](openapi.json))
- **This guide, served by the app:** `/api-guide`; a short index for agents: `/llms.txt`
- **A working client you can copy:** [`examples/shopper_client.py`](../examples/shopper_client.py)
- **See it in action:** the example client page at `/demo` (sign in on `/admin` first)

The project is in development. There is no compatibility promise yet: paths, fields and behaviour
may change.

## At a glance (read this first, AI agents)

1. **Authenticate every `/v1` call** with `Authorization: Bearer <user token>`. An app also sends
   `X-Client-Token: <client token>`. Never use an administrator's token in a client app.
2. **Anything that contacts a store is asynchronous.** The call returns `202` with `job_id` and
   `status_url`. Poll `GET /v1/jobs/{job_id}` every 1 to 2 seconds until `state` is no longer
   `queued`, `running` or `retry_wait`. The answer is in `result`.
3. **Money is a decimal string** (`"24.90"`), never a float. Times are ISO 8601 UTC. **Unknown is
   `null`**, never `0`: a missing price, barcode or stock level is not zero or "out of stock".
4. **Prices are per store.** Always name the store (`store_id`). Never guess one.
5. **Send an `Idempotency-Key`** on creating calls so a retry cannot duplicate anything.
6. **Respect limits.** On `429`, wait the `Retry-After` seconds. Do not poll faster than 1 second
   or fan out searches: lookups reach real stores and are rate limited on purpose.
7. **Errors have one shape:** `{"code", "message", "details", "request_id"}`. `message` is safe to
   show a person. A `409` on linking a barcode is a question for the human, not an error to retry.
8. **Never call `/v1/admin/*`.** Those are for the administrator and are refused to client tokens.
9. **To try things without real stores,** use the two stores of the synthetic *Demo shop*
   (`Demo Shop North`, `Demo Shop South`): their prices move every minute and nothing leaves the
   machine. Demo product page: `https://demo.example.org/p/oats-1kg`; demo barcodes
   `9999990000019` (oats) and `9999990000026` (coffee).

## 1. Basics

| | |
|---|---|
| Base URL | where the service runs, for example `http://localhost:8085` (all paths below start with it) |
| Format | JSON in and out (`Content-Type: application/json`) |
| IDs | opaque strings (UUIDs). A store's product id (SKU) is a string with leading zeros kept |
| Money | decimal strings with a currency code (`"24.90"`, `"SEK"`) |
| Time | ISO 8601, UTC |
| Unknown | `null`, never zero, `false` or "unavailable" unless the store said so |
| Channel | `online` today. In-store prices are not published, so none are guessed |

## 2. Getting access

Access is invitation-only, and there are two layers: a **user** and, for apps, an **API client**.

### A user signs in

An administrator creates an invitation (`POST /v1/admin/invitations`) and shares its one-time
token. The person accepts it and signs in:

```bash
curl -X POST $BASE/v1/auth/accept-invitation -H 'Content-Type: application/json' \
  -d '{"token": "<invitation token>", "password": "a password of at least 16 characters"}'
# 201 {"user_id": "..."}

curl -X POST $BASE/v1/auth/login -H 'Content-Type: application/json' \
  -d '{"email": "you@example.org", "password": "..."}'
# 200 {"access_token": "<user token>", "token_type": "bearer", "expires_in": 2592000, "user_id": "..."}
```

The user token lasts 30 days. Five failed logins for one email lock further attempts for 15
minutes (`429`). `POST /v1/auth/logout` revokes the token. `GET /v1/me` shows who you are and
your effective scopes.

### An app is an API client

A client is a credential an administrator creates for an app, which each user then **grants**
scopes to. The app calls with both tokens, and its permissions are the intersection of the user's
scopes, the client's scopes and the grant's scopes, so a user can always see and revoke what an
app may do.

```bash
# Administrator creates the client (the token is shown once):
curl -X POST $BASE/v1/clients -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name": "My shopping app", "scopes": ["catalogue:read", "monitors:read", "monitors:write", "events:read"]}'
# 201 {"client_id": "...", "client_token": "<client token>", "scopes": [...]}

# The user grants it a subset of those scopes (revoke with DELETE /v1/me/client-grants/{client_id}):
curl -X POST $BASE/v1/me/client-grants -H "Authorization: Bearer $USER_TOKEN" -H 'Content-Type: application/json' \
  -d '{"client_id": "<client_id>", "scopes": ["catalogue:read", "monitors:read", "monitors:write"]}'

# The app then calls with both:
curl $BASE/v1/monitors -H "Authorization: Bearer $USER_TOKEN" -H "X-Client-Token: $CLIENT_TOKEN"
```

A client token on its own is useless (`401`), and a user without a grant for that client gets
`403`. Clients can never hold `clients:manage` or administrator rights.

### Scopes

| Scope | Lets the caller |
|---|---|
| `catalogue:read` | list retailers, stores, products, prices, offers; run searches and barcode comparisons |
| `monitors:read` | list and read monitors; read your stores |
| `monitors:write` | add, check, remove monitors; link barcodes; choose your stores |
| `events:read` | read the event feed and a monitor's events |
| `webhooks:manage` | create, list, disable webhooks; read and retry deliveries |
| `clients:manage` | grant and revoke API clients (users only, never clients) |

A `403` means the token lacks the scope; the endpoint reference below says which each call needs.

## 3. Conventions

### Errors

Every error uses one envelope, with the HTTP status:

```json
{"code": "not_found", "message": "Monitor not found", "details": [], "request_id": "9f1c..."}
```

| Status | `code` | Meaning |
|---|---|---|
| 400 / 422 | `invalid_request` | The request is invalid; `details` lists the fields (`field`, `type`) |
| 401 | `unauthorized` | Missing, invalid, expired or revoked token |
| 403 | `forbidden` | The token lacks the scope, or the action is administrator-only |
| 404 | `not_found` | Missing, or not yours (the service does not reveal other people's resources) |
| 409 | `conflict` | State conflict: an idempotency key reused with a different body, a barcode that belongs to another product, a retailer-published barcode |
| 429 | `rate_limited` | Too many requests or a quota; wait `Retry-After` seconds |
| 503 | `unavailable` | The service is not ready (for example a pending migration) |

Quote `request_id` (also the `X-Request-ID` response header) when reporting a problem.

### Idempotency

Send `Idempotency-Key: <any unique string up to 200 chars>` on `POST /v1/monitors`,
`POST /v1/monitors/from-url`, `POST /v1/monitors/{id}/check`, `POST /v1/search/jobs` and
`POST /v1/compare/barcode`. Repeating the same request with the same key returns the same
resource instead of creating another. The same key with a different body is `409`.

### Asynchronous jobs

Looking something up in a store is slow and rate limited, so it never blocks. The call returns:

```json
{"job_id": "…", "status": "queued", "status_url": "/v1/jobs/…"}
```

Poll `GET /v1/jobs/{job_id}`:

```json
{"id": "…", "state": "succeeded", "result": {…}, "attempts": 1, "created_at": "…"}
```

`state` is `queued`, `running` or `retry_wait` while work continues. When it stops it is
`succeeded`, or a status that says why not: `partial`, `not_found`, `paused`, `disabled`,
`rate_limited`, `access_blocked`, `extraction_failed`, `invalid_data`, `auth_required`,
`unsupported`, `rejected` or `failed`. Only `succeeded` means the whole request was answered.
A `paused` or `disabled` store's connector is under the administrator's control, so retrying
immediately is pointless. Poll every 1 to 2 seconds and give up after about a minute.

### Pagination

`GET /v1/events?limit=100&after=<event id>` returns `{"items": [...], "next_cursor": "<id>"|null}`
oldest first. Pass `next_cursor` as `after` to continue, and keep the last id you processed to
catch up after downtime. Other lists are capped (monitors at 100).

## 4. Limits

These exist to keep traffic to real stores polite, and apply per person unless stated.

| Limit | Default |
|---|---|
| API requests | 300 per minute per credential |
| Active monitors | 50 |
| Live lookups in flight (search or compare) | 3 |
| Live lookups per hour | 20 |
| Manual `check` of one monitor | one per 60 seconds |
| Stores in "my stores" | 15 |
| Stores in one barcode comparison | 8 |
| Monitor check interval | at least 60 minutes for City Gross, Coop, Willys and Lidl (15 for the demo shop) |

The service also rate limits each store, caches identical lookups for 10 minutes, and pauses a
connector that a store refuses. You see that as job statuses, not as errors.

## 5. Concepts

- **Retailer / connector.** A store chain, and the plug-in that knows how to ask it. `GET
  /v1/connectors` shows which are available and their status.
- **Store.** One shop or delivery area of a retailer. Prices and range differ per store. Willys and Lidl are each one national online store.
- **Product.** A specific trade item, matched across stores by its barcode (GTIN). Items without a
  published barcode stay separate until someone links one (see below).
- **Listing.** A product in one store, with its price history.
- **Monitor.** What you watch. `exact_product` follows one listing; `generic_product` follows a
  text search with filters. (A `store` monitor exists only for connectors with a bounded offers feed.)
- **Event.** Something that happened to a monitor: a price move, an alert reached, an offer.
- **Barcode.** A product's GTIN, with its source: `retailer` (published by the store, cannot be
  changed), `user` (a person scanned and linked it) or `admin`. It is shared by everyone, so a wrong
  link affects everyone, which is why merging is explicit.

## 6. Workflows

All examples assume `AUTH='-H "Authorization: Bearer $USER_TOKEN" -H "X-Client-Token: $CLIENT_TOKEN"'`.

### Choose your stores

```bash
curl $BASE/v1/stores $AUTH                                  # every registered store
curl -X PUT $BASE/v1/me/stores/<store_id> $AUTH             # 204: compare and search here
curl $BASE/v1/me/stores $AUTH                               # {"items": [{"store_id", "name", "retailer_id"}]}
curl -X DELETE $BASE/v1/me/stores/<store_id> $AUTH          # 204
```

### Add a product by its page link

Paste the product page address from a supported store (City Gross, Coop, Willys, Lidl, the demo shop). Lidl can only be added this way (it cannot be searched). The page
is **not fetched**: only the product id is read from the link, then the store is asked about that
one product.

```bash
curl -X POST $BASE/v1/monitors/from-url $AUTH -H 'Content-Type: application/json' -H 'Idempotency-Key: add-apple-1' \
  -d '{"url": "https://www.citygross.se/matvaror/frukt-och-gront/frukt/%C3%A4pple-ingrid-marie-p100142800_KG",
       "store_id": "<store_id>", "maximum_price": "30"}'
# 202 {"job_id": "…", "status": "queued", "status_url": "/v1/jobs/…"}
```

Optional: `name`, `maximum_price` (alert at or below, in kr), `interval_minutes` (15 to 10080, and at
least the store's minimum), `notify_existing_offers`. `store_id` is required unless the retailer has
exactly one registered store. If you already watch the product you get `200` with
`{"already_monitored": true, "monitor": {...}}` and no job. Otherwise poll the job:

```json
{"state": "succeeded", "result": {"status": "success", "already_monitored": false, "monitor_id": "…",
  "product": {"name": "Äpple Ingrid Marie CA140G KLASS 1", "sku": "100142800_KG", "listing_id": "…",
              "current_price": "34.95", "currency": "SEK", "unit_price": {"amount": "34.95", "unit": "kg"},
              "availability": "in_stock", "source_url": "https://…"}}}
```

If the store does not list it the job ends `not_found` and nothing is created. A link that is not
a product page of a supported store is `422`, naming the supported stores.

### Add a product by name

```bash
curl -X POST $BASE/v1/search/jobs $AUTH -H 'Content-Type: application/json' \
  -d '{"query": "havregryn", "store_ids": ["<store_id>"], "channel": "online"}'
```

The job result lists `items` (`product_id`, `listing_id`, `store_id`, `name`, `current_price`,
`currency`) and `coverage` per store (`complete`, `not_found`, `paused`, …). Search by barcode with
`"gtin"` instead of `"query"`. Watch one by creating a monitor from its `listing_id`:

```bash
curl -X POST $BASE/v1/monitors $AUTH -H 'Content-Type: application/json' \
  -d '{"name": "Oats", "type": "exact_product", "store_id": "<store_id>",
       "conditions": {"listing_id": "<listing_id>", "maximum_price": "20.00"}, "interval_minutes": 60}'
# 201 the monitor
```

A `generic_product` monitor follows a search: `"conditions": {"query": "råris", "brand": "…",
"excluded_terms": ["ekologisk"], "maximum_price": "30", "minimum_package_weight_kg": "1",
"maximum_unit_price": {"amount": "25.00", "unit": "kg"}}`. It reports new matches and qualifying prices.

### Compare in a shop (scan a barcode)

You are in shop A and scan a barcode. Find the same item in your other stores:

```bash
curl -X POST $BASE/v1/compare/barcode $AUTH -H 'Content-Type: application/json' \
  -d '{"gtin": "7310130321085", "here_store_id": "<the store you are in>"}'
```

Stores come from `store_ids` or, by default, your stores (`/v1/me/stores`), plus `here_store_id`
listed first (at most 8). The `202` response already carries what the catalogue knows, so you can
draw something immediately, and the job then checks each store live:

```json
{"job_id": "…", "status_url": "/v1/jobs/…", "gtin14": "07310130321085", "here_store_id": "…",
 "product": {"id": "…", "name": "Havregryn 1,5KG AXA", "retailers": ["se.citygross", "se.coop"], "listings": 2},
 "known": [
   {"store_id": "…", "store_name": "City Gross Borås", "retailer_id": "se.citygross", "status": "found",
    "current_price": "19.95", "currency": "SEK", "unit_price": {"amount": "13.30", "unit": "kg"},
    "source": "catalogue", "age_seconds": 5400, "is_cheapest": false, "vs_cheapest": {"amount": "2.45", "percent": "14.0"}},
   {"store_id": "…", "store_name": "Coop Häggvik", "status": "unknown", "detail": "Not checked in this store yet"}],
 "cheapest": {"store_name": "Coop Häggvik", "current_price": "17.50"},
 "saving_vs_here": {"amount": "-2.45", "percent": "-12.3"}}
```

The finished job `result` has the same `rows` (as `rows`), now with `source: "live"`, plus `complete`
(false if some store could not be checked) and `requests`. A row's `status` is `found`, `not_found`
(the store does not sell it), `unknown` (never checked), `unknown_mapping` (that store cannot
search by barcode and no listing of this product is linked to it yet), or a store-state status
such as `paused`. With `"refresh": false` you get the known prices only, immediately, with `200`
and no job. A store-internal code (a barcode starting with 2, used for loose items) is `422`.

### Link a barcode you scanned to a product

A product that the store publishes no barcode for (loose or some packaged goods) can be given
one, so it can be found by barcode and compared across stores:

```bash
curl -X POST $BASE/v1/monitors/<monitor_id>/barcode $AUTH -H 'Content-Type: application/json' \
  -d '{"gtin": "4006381333931"}'
# 200 {"outcome": "set", "merged_listings": 0, "product": {"id": "…", "barcode": {"gtin14": "…", "source": "user"}, …}}
```

`outcome` is `set`, `replaced` (you corrected a barcode a person had set) or `unchanged`. If the
barcode already belongs to another product you get `409` naming it in `details[0].existing_product`
(`id`, `name`, `retailers`). **Ask the person** whether they are the same item; if yes, resend with
`"merge": true`, which moves your product's listings to that one (`outcome: "merged"`, price
history is kept). A barcode the store itself publishes cannot be changed (`409`). Undo a wrong scan
with `DELETE /v1/monitors/{id}/barcode`. Only `exact_product` monitors have a barcode to link.

### Your watchlist

`GET /v1/monitors` returns everything needed to draw it:

```json
{"id": "…", "name": "Demo oats 1 kg", "type": "exact_product", "store_id": "…", "store_name": "Demo Shop North",
 "product_id": "…", "product_name": "Demo oats 1 kg", "barcode": {"gtin14": "09999990000019", "source": "retailer"},
 "latest": {"current_price": "27.05", "previous_price": "27.60", "currency": "SEK",
            "unit_price": {"amount": "27.05", "unit": "kg"}, "availability": "in_stock", "observed_at": "…"},
 "conditions": {"listing_id": "…", "maximum_price": "25.00"}, "interval_minutes": 60,
 "last_success_at": "…", "next_due_at": "…", "enabled": true, "removed_at": null}
```

`latest` is `null` until the first check. Price history: `GET /v1/listings/{listing_id}/prices` (newest
first). A monitor's events: `GET /v1/monitors/{id}/events`. Check now (one per 60 seconds):

```bash
curl -X POST $BASE/v1/monitors/<monitor_id>/check $AUTH     # 202 {"job_id": "…", "status": "queued"}
```

### Remove

```bash
curl -X DELETE $BASE/v1/monitors/<id> $AUTH                    # 204: stops, leaves your list, keeps its events
curl "$BASE/v1/monitors?include_removed=true" $AUTH            # still listed, with "removed_at" set
curl -X DELETE "$BASE/v1/monitors/<id>?permanent=true" $AUTH   # 204: deletes the monitor and its events
```

The product's price history is shared and stays either way. Removing frees a slot under the
monitor limit.

### Events

`GET /v1/events` is your feed across all monitors (see Pagination). Each event:

```json
{"id": "…", "type": "price.decreased", "monitor_id": "…", "payload": {"listing_id": "…", "old": "27.60", "new": "27.05", "store_id": "…", "channel": "online"}, "created_at": "…"}
```

| Event `type` | When | `payload` |
|---|---|---|
| `price.decreased`, `price.increased` | The price changed between checks | `listing_id`, `old`, `new`, `store_id`, `channel` |
| `price.threshold_reached` | The price crossed down to your `maximum_price` | `listing_id`, `price`, `threshold` |
| `offer.detected` | A promotion on a watched product appeared | `offer_id`, `title`, `terms`, `eligibility`, `valid_to`, `store_id` |
| `offer.changed` | A known promotion changed | as above |
| `product.discovered` | A new product matched a `generic_product` monitor | `listing_id`, `name` |
| `product.restocked`, `product.unavailable` | Stock changed (only when the store states it) | `listing_id`, `old`, `new`, `store_id`, `channel` |

The first check of a monitor sets a baseline and raises no events (unless `notify_existing_offers`).
A failed, refused or partial check never creates a price, stock or offer event.

## 7. Webhooks

Have events pushed to your server instead of polling.

```bash
curl -X POST $BASE/v1/webhooks $AUTH -H 'Content-Type: application/json' -d '{"url": "https://example.org/hook"}'
# 201 {"id": "…", "url": "…", "signing_secret": "<shown once>"}
```

The URL must be a public `https` address (private addresses are refused unless the administrator
allows them). Disable with `PATCH /v1/webhooks/{id}` `{"enabled": false}` or `DELETE`.

Each delivery is a `POST` with this body and headers:

```json
{"id": "<event id>", "type": "price.decreased", "occurred_at": "2026-11-26T09:00:00+00:00", "data": {…payload…}}
```

```
X-Retail-Timestamp: 1795693430
X-Retail-Delivery: <delivery id>
X-Retail-Signature: v1=<hex>
```

**Verify every delivery:** the signature is HMAC-SHA256, keyed with your `signing_secret`, over
`timestamp + "." + the exact raw body`, hex encoded. Reject deliveries whose timestamp is more than
about five minutes old, and deduplicate on `X-Retail-Delivery` (delivery is at least once).

```python
import hashlib, hmac, time

def verify(secret: str, headers: dict, raw_body: bytes, tolerance: int = 300) -> bool:
    timestamp = headers["X-Retail-Timestamp"]
    if abs(time.time() - int(timestamp)) > tolerance:
        return False
    expected = "v1=" + hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, headers["X-Retail-Signature"])
```

Answer `2xx` quickly. `408`, `429` and `5xx` are retried with backoff (up to 8 attempts); other
`4xx` fail permanently. `GET /v1/webhooks/{id}/deliveries` shows each delivery's `state`
(`queued`, `retry_wait`, `delivered`, `failed`) and `POST /v1/webhooks/{id}/deliveries/{delivery_id}/retry`
requeues a failed one. Use the event feed with `after` to reconcile anything missed.

## 8. Job results by kind

| Started by | `result` |
|---|---|
| `POST /v1/search/jobs` | `{"status", "complete", "items": [{"product_id", "listing_id", "store_id", "channel", "name", "current_price", "currency"}], "coverage": [{"store_id", "status", "detail", "truncated"}], "barcode_mapping": "verified"\|"unknown"\|null}` |
| `POST /v1/monitors/from-url` | `{"status", "already_monitored", "monitor_id", "product": {…}}`, or `{"status": "not_found"\|"paused"\|…, "detail"}` |
| `POST /v1/compare/barcode` | `{"status", "complete", "gtin14", "here_store_id", "product", "rows": […], "cheapest", "saving_vs_here", "requests"}` |
| `POST /v1/monitors/{id}/check` | `{"status", "complete", "prices", "offers", "invalid_items", "truncated", "detail", "requests"}` |

`requests` is how many requests actually went to stores (0 when answered from the cache).

## 9. Endpoint reference

Scope is what the token needs. All paths are under the base URL.

| Method | Path | Scope | Purpose |
|---|---|---|---|
| POST | `/v1/auth/accept-invitation` | none | Accept an invitation and set a password |
| POST | `/v1/auth/login` | none | Sign in; returns the user token |
| POST | `/v1/auth/logout` | any | Revoke the user token |
| GET | `/v1/me` | any | Who you are and your effective scopes |
| POST | `/v1/clients` | admin | Create an API client credential (token shown once) |
| DELETE | `/v1/clients/{client_id}` | admin | Revoke an API client |
| POST | `/v1/me/client-grants` | `clients:manage` | Grant a client a subset of scopes |
| DELETE | `/v1/me/client-grants/{client_id}` | `clients:manage` | Revoke a grant |
| GET | `/v1/retailers` | `catalogue:read` | Retailers and their status |
| GET | `/v1/connectors` | `catalogue:read` | Connectors, capabilities, status |
| GET | `/v1/connectors/{retailer_id}/health` | `catalogue:read` | One connector's health and budget use |
| GET | `/v1/retailers/{retailer_id}/stores` | `catalogue:read` | Stores of one retailer |
| GET | `/v1/stores` | `catalogue:read` | All registered stores |
| GET | `/v1/stores/{store_id}` | `catalogue:read` | One store |
| GET | `/v1/stores/{store_id}/offers` | `catalogue:read` | Current promotions observed at a store |
| GET | `/v1/stores/{store_id}/offers/history` | `catalogue:read` | Promotion revisions at a store |
| GET | `/v1/offers` | `catalogue:read` | Promotions, optionally by `store_id` |
| GET | `/v1/offers/{offer_id}` | `catalogue:read` | One promotion |
| GET | `/v1/products/search` | `catalogue:read` | Search the products already known (no store contact) |
| GET | `/v1/products/barcode/{gtin}` | `catalogue:read` | The product linked to a barcode, or `404` |
| GET | `/v1/products/{product_id}` | `catalogue:read` | A product, its barcode and where it is listed |
| GET | `/v1/products/{product_id}/prices` | `catalogue:read` | Latest price per store |
| GET | `/v1/products/{product_id}/history` | `catalogue:read` | Price history across stores |
| GET | `/v1/listings/{listing_id}/prices` | `catalogue:read` | Price history of one listing |
| POST | `/v1/search/jobs` | `catalogue:read` | Live search by name or barcode (asynchronous) |
| POST | `/v1/compare/barcode` | `catalogue:read` | Compare one barcode across your stores |
| GET | `/v1/me/stores` | `monitors:read` | Your comparison stores |
| PUT | `/v1/me/stores/{store_id}` | `monitors:write` | Add a store to yours |
| DELETE | `/v1/me/stores/{store_id}` | `monitors:write` | Remove a store from yours |
| POST | `/v1/monitors` | `monitors:write` | Create a monitor |
| POST | `/v1/monitors/from-url` | `monitors:write` | Add a product from its page link (asynchronous) |
| GET | `/v1/monitors` | `monitors:read` | Your monitors (`include_removed`) |
| GET | `/v1/monitors/{monitor_id}` | `monitors:read` | One monitor |
| GET | `/v1/monitors/{monitor_id}/events` | `events:read` | One monitor's events |
| DELETE | `/v1/monitors/{monitor_id}` | `monitors:write` | Remove (`permanent=true` to delete) |
| POST | `/v1/monitors/{monitor_id}/check` | `monitors:write` | Check now (asynchronous) |
| POST | `/v1/monitors/{monitor_id}/barcode` | `monitors:write` | Link a barcode to the monitor's product |
| DELETE | `/v1/monitors/{monitor_id}/barcode` | `monitors:write` | Remove a barcode a person linked |
| GET | `/v1/events` | `events:read` | Your event feed (cursor) |
| GET | `/v1/jobs/{job_id}` | `catalogue:read` or `monitors:read` (by job kind) | Poll a job |
| POST | `/v1/webhooks` | `webhooks:manage` | Register a webhook (secret shown once) |
| GET | `/v1/webhooks` | `webhooks:manage` | List webhooks |
| PATCH | `/v1/webhooks/{hook_id}` | `webhooks:manage` | Enable or disable |
| DELETE | `/v1/webhooks/{hook_id}` | `webhooks:manage` | Disable a webhook |
| GET | `/v1/webhooks/{hook_id}/deliveries` | `webhooks:manage` | Delivery history |
| POST | `/v1/webhooks/{hook_id}/deliveries/{delivery_id}/retry` | `webhooks:manage` | Requeue a failed delivery |
| GET | `/health/live` | none | Liveness |
| GET | `/health/ready` | none | Readiness and schema version |

### Administrator-only endpoints

Not available to client tokens; listed so the full surface is documented.

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/admin/invitations` | Invite a user |
| GET | `/v1/admin/audit` | Recent audit log |
| GET | `/v1/admin/jobs` | Recent jobs of all users |
| POST | `/v1/admin/jobs/{job_id}/retry` | Retry a finished job that did not succeed |
| POST | `/v1/admin/example-user` | Open an example-user session for the `/demo` page |
| DELETE | `/v1/admin/example-user/data` | Reset the example user's data |
| GET | `/v1/admin/connectors` | Connectors with status, settings, usage |
| POST | `/v1/admin/connectors/{retailer_id}/enable` | Enable a connector |
| POST | `/v1/admin/connectors/{retailer_id}/disable` | Disable a connector |
| POST | `/v1/admin/connectors/{retailer_id}/pause` | Pause a connector |
| POST | `/v1/admin/connectors/{retailer_id}/resume` | Resume a connector |
| PUT | `/v1/admin/connectors/{retailer_id}/settings/{name}` | Set a connector setting (write-only) |
| DELETE | `/v1/admin/connectors/{retailer_id}/settings/{name}` | Clear a connector setting |
| POST | `/v1/admin/connector-packages/inspect` | Upload and preview a connector package |
| POST | `/v1/admin/connector-packages/{upload_id}/install` | Install a previewed package |
| DELETE | `/v1/admin/connector-packages/{retailer_id}` | Remove an installed connector |
| POST | `/v1/admin/retailers/{retailer_id}/stores/discover` | Register stores (for example from a postcode) |
| POST | `/v1/admin/products/{product_id}/verified-gtin` | Link a barcode as an administrator |

## 10. Troubleshooting

| You see | Likely cause |
|---|---|
| `401` on everything | The user token expired (30 days) or was revoked; sign in again. Or a client token without the bearer token |
| `401` "Invalid client token" | The client credential was revoked or replaced |
| `403` "Client is not authorized by this user" | The user has not granted this client any scopes |
| `403` "Insufficient scope" | The grant or client lacks the scope in the endpoint reference |
| `422` "Prices differ by store: send store_id" | The retailer has several stores; name one |
| `422` "…not a product page on a supported store" | The link is from a store that has no connector, or not a product page |
| Job ends `paused` or `disabled` | The administrator paused or disabled that store's connector (often because the store refused automated access) |
| Job ends `auth_required` | The connector is missing a setting (for example an API key) |
| Job ends `rate_limited` | The store asked the service to slow down; try later |
| `429` on `check` | That monitor was checked less than a minute ago |
| Compare row `unknown_mapping` | That store cannot search by barcode; link the barcode to a product listed there first |
| Search coverage `unsupported` for a store | That store cannot be searched by name (Lidl): add its products by page link |
| Compare row `not_found` | That store does not sell the barcode (this is an answer, not an error) |

## 11. Example client

[`examples/shopper_client.py`](../examples/shopper_client.py) is a small, dependency-light Python
client (it needs only `httpx`) that wraps this API: authentication with both tokens, the job
polling pattern, idempotency keys, adding by link, comparing by barcode, the watchlist, removal,
and webhook signature verification. It runs end to end against the demo shop:

```bash
python examples/shopper_client.py --base-url http://localhost:8085 \
    --user-token "$USER_TOKEN" --client-token "$CLIENT_TOKEN"
```

The `/demo` page in a running service is the same thing as a web app, and its "API calls" tab shows
every request it makes.

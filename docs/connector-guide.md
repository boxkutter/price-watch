# Building a connector, step by step

For people and for AI coding agents. It assumes nothing about the store you are adding. The
reference for every field and status is [connectors.md](connectors.md); this guide is the order to
do things in, how to investigate a site, and the mistakes already made on three real stores.

**You will produce:** a folder `<id>/` with `manifest.yaml`, `connector.py`, `test_connector.py`
and fixtures, which `python -m app.connectors.build` turns into a zip you upload in the admin
page. Start from a copy of [connector-template/](connector-template/).

## 0. Ground rules (read once)

These come from the [access policy](../readme.md#access-policy) and are enforced by the contract
tests.

1. **Targeted only.** Fetch what was asked: a text query, specific product ids, specific barcodes.
   Never walk categories, "all offers", sitemaps or the catalogue.
2. **Behave like a polite person.** One request at a time, spaced, few per scan, cached. Use the
   standard client (`self.client`); never your own.
3. **Never get around a refusal.** A 401/403, a challenge page or a redirect to a login means the
   site said no. Report `access_blocked`, record it in `access_note`, and stop. Do not change
   headers, rotate identities, impersonate a browser fingerprint, or solve challenges. (ICA is
   documented this way; see section 10.)
4. **Secrets are settings.** Anything that works like a credential is declared under `settings:` and
   read with `self.require("NAME")`. It never appears in code, fixtures, URLs, logs or results.
5. **Unknown is not zero.** A missing price is skipped; unknown stock is `None`; a product that was
   asked for but not returned is `missing`, never "discontinued".

## 1. Decide what the connector will do

Pick the capabilities you can actually verify, and mark the rest `unsupported`:

| Capability | Question to answer |
|---|---|
| `product_search` | Can I search by text and get products with prices for one store? |
| `barcode_search` | Does the site expose the barcode (GTIN/EAN), so I can look one up? |
| `online_prices` | Are the prices the online-shop prices for a specific store or area? |
| `store_discovery` | Is there a way to list stores, or resolve one from a postcode? |
| `physical_store_prices`, `store_offers` | Almost always `unsupported`: in-store prices are rarely published, and whole-store offer lists are enumeration |

## 2. Investigate the site

Do this by hand first, with the browser's developer tools (Network tab, filter Fetch/XHR).

1. **Read `robots.txt`** of the site *and* of any API host it calls. Note what is disallowed. Never
   fetch a disallowed page; if the data comes from an API host, use the API the site itself calls.
2. **Watch the site work.** Search for a product, change the store or postcode, open a product page.
   Note every request that returns product data: method, URL, query parameters, request body,
   headers that look like keys, and the response shape.
3. **Try the request from a plain HTTP client** (`curl` or `httpx`) with a generic user-agent:
   - It answers → good; no browser is needed (City Gross).
   - It answers only with a key the page sends to every visitor → see section 7 (Coop).
   - It is refused (403, a challenge, "access denied") → **stop** (rule 3). Write down what you
     saw in `access_note` and tell the human; the honest options are an official data route or
     choosing another store.
4. **Answer these, on real responses** (they decide the design):

| Question | Why it matters |
|---|---|
| How is the *store* identified, and does the price depend on it? What does an **unknown** store return? | Some APIs return nothing instead of an error, which looks like "product not found" |
| Is the product id also the barcode? Is the barcode present and valid? | Decides barcode lookup and cross-store matching |
| Which field is the *consumer* price? Is there a regular/reference price, a member price, a business price? | Only the consumer price is `current_price` |
| Does the price already include promotions? | Otherwise you must apply a single-item promotion yourself |
| What are the units and how is the package size written? | Unit prices depend on a correct size |
| What does "available" mean: in stock, or merely orderable? | `False` must not become "out of stock" unless it truly means that |
| Do promotions carry amounts and dates anonymously? | If only a flag exists, produce **no offers** rather than invent them |
| How does paging work, and can you trust the total? | Decides how you detect the last page |
| Does the rate of requests matter (limits, `Retry-After`)? | Sets `minimum_request_interval_seconds` and the daily cap |

5. **Keep it small.** A dozen requests are enough to learn all of this. Space them out.

## 3. Map the data

Fill `PriceData` (one per product, `channel="online"`):

| Field | Rule |
|---|---|
| `sku` | The store's own product id, as a string, leading zeros kept |
| `name` | Enough to tell sizes and variants apart ("Oat flakes ICA 1.5 kg"); the generic monitors match on this |
| `current_price`, `regular_price` | `Decimal` via `to_decimal()`. Never a float. `regular_price` only if the site states it |
| `currency` | ISO code, e.g. `SEK` |
| `gtin` | Only through `valid_gtin()` (valid check digit). Not for store-internal codes |
| `availability` | `"in_stock"`, `"out_of_stock"`, or `None` when unknown |
| `package_value`, `package_unit` | Use `parse_size()` from `app.pricing` on the label (`1,5KG`, `450 g`, `33CL`, `6x33cl`), else `None`. Units: `g kg ml L st` |
| `source_url` | A link a person can open |

Offers (`OfferData`), only when amounts exist: `identity_key` must be stable for the same campaign
**and include the product** (`f"{sku}:{promo_id}"`) so shared campaign ids do not collide; `terms`
is the retailer's wording or a faithful rendering of its fields; `price` is the bundle total or
`None`; `eligibility` is `public` or `member` (a member price never becomes `current_price`);
`valid_from`/`valid_to` are timezone-aware; list the products in `skus`.

## 4. Capture fixtures

Save a few **real** responses and trim them to the fields you use. Remove descriptions, images,
nutrition and anything personal. Never keep a key or token. Pick the varied cases: a plain
product, one with a promotion, a multipack, an unusual size label, one without a price, a code that
looks like a barcode but is not. Put them in `fixtures/` and write tests against them first.

## 5. Write the manifest

Copy the template manifest and edit it. Set `allowed_hosts` to exactly the hosts you contact, every
declared capability honestly, `settings` for any key, and conservative limits (`3` seconds between
requests, `60` minutes between checks, `6` requests per scan, a few hundred per day). Add
`access_note`: why automating this site is acceptable, and anything fragile. Validate it in your
editor with `manifest.schema.json` (the template already points at it) or at the command line with
`python -m app.connectors.build --schema`.

## 6. Write the connector

Subclass `HttpJsonConnector`. Implement `collect(store, request)` and, if the store has a
directory, `discover(**inputs)`. The template is a complete, commented example; the built-in
`builtin/citygross/connector.py` and `builtin/coop/connector.py` are real ones.

- `request.query` → one or two search requests, stopping at a **short page**.
- `request.skus` / `request.gtins` → direct lookups; batch them if the API allows. Put what was
  not found in `missing`.
- Raise nothing for normal outcomes. The kit turns refusals, rate limits and bad responses into
  statuses; raise `TransientError` only for network faults (the client already does).

## 7. When the site needs a key

If the site's own pages send a key that any visitor receives, and the plain client works only with
it, declare it as a setting (`secret: true, required: true`), read it with `self.require()`, and
send it in a **header**. Say in `access_note` where it comes from and that it is undocumented. A
rejected key (401/403) pauses the connector, and the administrator updates the setting and resumes.
Do not extract the key automatically at run time. Coop is the worked example.

## 8. Test

```bash
cd your-connector-folder && pytest -q          # unit tests from fixtures + the contract
```

- **Unit tests** on `parse_*` functions, using your fixtures: prices as exact `Decimal`s, sizes,
  promotions, unknown values skipped.
- **`check_connector(...)`** (see the template's `test_connector.py`) runs the full contract against
  a fake site.
- **Live smoke test (optional, by hand):** a test skipped unless `RUN_LIVE=1`, making about six
  spaced requests: directory, a search, a lookup by id, a lookup by barcode. Never in CI; never
  more traffic than that. It is how you find the surprises section 10 lists.

## 9. Package and install

```bash
python -m app.connectors.build path/to/your-folder      # validates like the upload, writes <id>-<version>.zip
```

In the admin page: **Install a connector** → pick the zip → **Review package** (hosts, settings,
files, warnings) → **Install**. It starts **disabled**. Enter its settings, then **Enable**. Use
**Find stores** to add a store, search for a product, and create a monitor. If it fails to load,
the install is rolled back and the reason is shown.

## 10. What went wrong on real stores

| Store | Surprise | What the connector does |
|---|---|---|
| City Gross | The `store` parameter is the store number (`storeNumber`), not its id, and an **unknown store silently returns no products** | Validates stores against the directory at registration; never treats "no results" as "not found" for a bad store |
| City Gross | `totalCount` said 25 while only 22 products existed | Ends paging on a **short page**, not on the count |
| City Gross | `currentPrice` excludes promotions; shared promotion ids across products | Applies only a public single-item promotion to the price; offer identity includes the SKU |
| City Gross | `netContent.unitOfMeasure` is `0` for weights and volumes alike | Parses the Swedish label (`1,5KG`) instead |
| Coop | Needs a key the pages send to every visitor; no developer programme | A declared, write-only setting; a rejected key pauses it |
| Coop | The store comes from a postcode, not a list | `store_discovery_inputs: [postcode]` |
| Coop | Deals appear only as type flags, with no amounts | Produces no offers rather than inventing them |
| Coop | `availableOnline` means orderable, not in stock | `False` is recorded as unknown |
| Coop | Business price next to the consumer price; multipack sizes in names or labels | Uses only the consumer price; a multipack with an unreadable label has unknown size |
| Both | EANs beginning with `2` are store-internal codes | Never matched across stores as a global barcode |
| ICA | Product data endpoint refuses automated clients (HTTP 403) while a person's browser works | **Not built.** Documented as a placeholder; getting past it would mean disguising the client |

## 11. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Upload says "manifest.yaml is invalid" | An unknown key (typo) or a bad value; the message names it. Check against `manifest.schema.json` |
| "implementation names … but connector.py is not in the package" | The module file is outside the zip or the name differs; the zip may contain the folder itself, which is fine |
| Status "Error" in the admin page | The package raised on import or the class `id` differs from the manifest `id`; the reason is shown |
| Status "Needs configuration" | A required setting is missing: enter it on the connector card |
| Scans finish `access_blocked` and the connector pauses | The site refused the client. Do not retry or change headers; review, and resume only if the cause is understood |
| Scans finish `auth_required` | The setting is unset or empty |
| Contract test: "outside allowed_hosts" | You contacted a host not listed in the manifest (including a redirect target or CDN) |
| Everything works on a fake site, nothing on the real one | Re-check the section 2 questions on real responses; build fixtures from real data |

## 12. Definition of done

- [ ] Section 0 rules respected; `access_note` explains the access decision
- [ ] Manifest validates; `allowed_hosts` is exactly the hosts contacted; capabilities are honest
- [ ] Unit tests pass on trimmed **real** fixtures, with no keys or personal data in them
- [ ] `check_connector(...)` passes
- [ ] No request on untargeted scans; no secrets in code, URLs or results
- [ ] `python -m app.connectors.build` produces the zip with no warnings you cannot explain
- [ ] (Optional) the live smoke test passes with about six requests
- [ ] Installed through the admin page, configured, enabled, and one real search and one monitor work

# Instructions for AI agents building a connector

You are creating a **connector**: a self-contained package that teaches Price Watch about one
store or service. Humans can read this too; it is written as a checklist. Work in a copy of this
folder. Read in this order: this file, `README.md`, `manifest.yaml`, `connector.py`,
`test_connector.py`. The full guide is `docs/connector-guide.md` and the reference is
`docs/connectors.md` in the repository (they are not inside the package; ask the human for them if
you do not have the repository).

## Hard rules (never break these)

1. **Targeted only.** Fetch only what the request asks for (`request.query`, `request.skus`,
   `request.gtins`). Never crawl categories, sitemaps, "all offers" or the catalogue.
2. **Never get around a refusal.** On HTTP 401/403, a challenge page, or a redirect to a login,
   stop. Report it; do not change headers or user-agent, rotate identities, impersonate a browser,
   or solve challenges. Write what you saw in `access_note` and ask the human how to proceed.
3. **Use `self.client` for every request.** Never `requests`, `urllib`, `socket` or your own
   `httpx` client. It enforces the host allowlist, spacing, size and time limits.
4. **No secrets in the package.** Keys and tokens are declared under `settings:` and read with
   `self.require("NAME")`, sent in headers only. Never in code, fixtures, URLs, logs or results.
   Never ask the human to paste a key into a file you create.
5. **Unknown is not zero.** Skip a product whose price is unknown. Unknown stock is `None`. A
   product asked for but not returned goes in `Collected.missing`, never "discontinued".
6. **Prices are `Decimal`, never `float`.** Use `to_decimal()`. GTINs go through `valid_gtin()`.
7. **Do not edit files outside your connector folder.** The core, other connectors and the
   contract kit are not yours to change. If something seems missing, say so.

## Files

| File | Edit it? |
|---|---|
| `manifest.yaml` | Yes: id, hosts, capabilities, settings, limits, `access_note`. Unknown keys are rejected; validate against `manifest.schema.json` |
| `connector.py` | Yes: `parse_product()`, `collect()`, `discover()` |
| `test_connector.py` | Yes: replace the fake shop with fixtures from the real API |
| `fixtures/` | Create it: trimmed real responses (no keys, no personal data) |
| `manifest.schema.json` | No: generated; `python -m app.connectors.build --schema` |
| `README.md`, `AGENTS.md` | Update README for your connector; leave this file |

## Workflow

1. **Investigate** the site (guide section 2) before writing code: `robots.txt`, the requests the
   site itself makes, whether a plain client is answered, how stores are identified, which field is
   the consumer price, units, availability, promotions, paging. If a plain client is refused, stop
   and tell the human (rule 2). Keep it to about a dozen spaced requests.
2. **Capture fixtures** of real responses and trim them (guide section 4).
3. **Write tests first** from the fixtures: parsing, units, promotions, unknown values.
4. **Product links:** ask the human for two or three real product page links and add a
   `product_urls` rule with a real `example` (the link is parsed, never fetched).
5. **Edit the manifest**: a new `id` (`<2-10 letters>.<name>`), exactly the hosts you contact,
   honest capabilities, settings for any key, conservative limits.
6. **Write the connector**: subclass `HttpJsonConnector`; implement `collect()` (and `discover()`).
7. **Run the contract**: `check_connector(...)` is in `test_connector.py`.
8. **Package**: `python -m app.connectors.build <folder>`; read every warning.
9. Tell the human the zip is ready; installing and enabling happen in the admin page.

## Commands (run from the repository root)

```bash
cd <your-folder> && pytest -q                         # this connector's tests + contract
python -m app.connectors.build <your-folder>          # validate and zip, like the upload
python -m app.connectors.build --schema               # JSON Schema of manifest.yaml
scripts/check.sh --fast                               # the whole repository's lint + tests
```

## Definition of done

- [ ] Hard rules respected; `access_note` explains the access decision
- [ ] `pytest -q` passes in the connector folder, on trimmed real fixtures
- [ ] `check_connector(...)` passes, including `product_urls` reading real example links
- [ ] `allowed_hosts` lists exactly the hosts the connector contacts
- [ ] No key, token or personal data anywhere in the folder (search for them)
- [ ] The build command succeeds and every warning is explained
- [ ] README says what the connector covers and how to set it up

## Stop and ask the human when

- the site refuses a plain client, shows a challenge, or the terms look restrictive
- you need a credential, or the site requires login
- you are unsure whether a field is the consumer price, a business price, or a member price
- a capability cannot be verified (mark it `unverified` or `unsupported`; do not guess)
- anything would need more than a few dozen requests to learn

## Common mistakes

- Reporting "not found" for a bad store: some APIs return an empty list for an unknown store, so
  validate stores at registration instead.
- Trusting a response's total count: end paging on a short page.
- Treating a promotion flag as an offer: create offers only when amounts and dates exist.
- Putting a member price in `current_price`: member prices are offers with `eligibility="member"`.
- Matching products on store-internal barcodes (EAN-13 starting with `2`).
- Reading `availableOnline` / "orderable" as "in stock".
- Guessing the size of a multipack: leave it unknown unless the label is clear.

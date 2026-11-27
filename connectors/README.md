# Connectors

Everything the service knows about a particular store lives here, **one folder per connector**.
To add a store, add a folder. To remove one, delete its folder. The application code never changes.

| Folder | Connector | State |
|---|---|---|
| [`se.citygross/`](se.citygross/) | City Gross | Working. No key needed. "Find stores" takes an optional town or postcode |
| [`se.coop/`](se.coop/) | Coop | Working. Needs the `COOP_API_KEY` setting (enter it in the admin page) |
| [`se.willys/`](se.willys/) | Willys | Working. No key needed. Search and links; member offers; one national online store; background checks only 04:00-08:45 UTC as Willys asks |
| [`se.lidl/`](se.lidl/) | Lidl | Working, narrow. Add a product by its lidl.se page link (no search); covers only the online shop, not grocery prices, which Lidl does not publish |
| [`se.dollarstore/`](se.dollarstore/) | Dollarstore | Working. No key needed. Search, offers ("2 för 35") and per-store stock; stores come from the site's store list; a search is capped at the first 25 results |
| [`se.ica/`](se.ica/) | ICA | Search, product links and multibuy offers for one chosen store. Use is authorized by ICA; shipped disabled and untried against the live shop. See its `access_note` |
| [`test.fixture/`](test.fixture/) | Fixture retailer | Synthetic data for tests and the demo store |
| [`test.demoshop/`](test.demoshop/) | Demo shop (synthetic) | A made-up store with two shops (north, south) whose prices move over time and that sell the same barcodes. For trying the app and the example client without any real retailer; it never contacts anything |
| [`_template/`](_template/) | (not loaded) | A complete, commented starting point for a new connector, with tests |

Folders whose names start with `_` or `.` are ignored by the loader.

## Add a connector

**From this folder (the developer workflow)**

1. Copy `_template/` to a new folder named after the connector id, for example `xx.mystore/`.
2. Follow [`../docs/connector-guide.md`](../docs/connector-guide.md): investigate the site, edit
   `manifest.yaml` and `connector.py`, and make `pytest -q` pass inside your folder.
3. The running service notices the new folder within a couple of seconds. It appears in the admin
   page as **Disabled**. Enter its settings and enable it there.

**From the admin page (no access to the server needed)**

Build a zip with `python -m app.connectors.build connectors/xx.mystore`, then use **Install a
connector** in the admin page: upload, review the preview, install. Uploaded connectors are stored
in `INSTALLED_CONNECTORS_DIR` (a Docker volume), not in this folder. They use exactly the same
structure and the same loader.

## Remove a connector

- **A folder here:** delete it. Its stores, prices and history are kept; monitors that used it stop
  working, and the admin page lists them as belonging to a connector that is no longer installed.
  To turn one off without deleting it, press **Disable** in the admin page.
- **An uploaded one:** press **Remove** on its card in the admin page.

## What a connector folder contains

```text
xx.mystore/
├── manifest.yaml       what the service needs to know, as data (validate with manifest.schema.json)
├── connector.py        the code: collect() fetches and parses, discover() looks up stores
├── test_connector.py   your tests, including the shared contract (check_connector)
├── fixtures/           trimmed real responses used by the tests (no keys, no personal data)
├── README.md           what it covers and how to set it up
└── AGENTS.md           (from the template) rules and checklist for AI agents
```

Only `manifest.yaml` and the module it names are required.

## Where to read next

| You are | Read |
|---|---|
| A developer adding a store | [`../docs/connector-guide.md`](../docs/connector-guide.md): the steps, how to investigate a site, real pitfalls |
| An AI coding agent | [`_template/AGENTS.md`](_template/AGENTS.md): hard rules, workflow, commands, checklist |
| Looking up a manifest field or status | [`../docs/connectors.md`](../docs/connectors.md) and [`_template/manifest.schema.json`](_template/manifest.schema.json) |
| Wondering about trust and security | [`../docs/connectors.md#trust-and-security`](../docs/connectors.md#trust-and-security): connector code runs inside the service |

## The rules every connector follows

Fetch only what was asked (never crawl). Behave like a polite person (the service enforces spacing
and caps). Never get around a refusal or disguise the client. Secrets are declared settings, never
in code. Unknown is never zero. The contract tests check these.

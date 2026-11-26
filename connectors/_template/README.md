# Connector template

A complete, working example connector for a fictional shop, with tests. Copy this folder to start a
connector for a real store or service.

| Read | When |
|---|---|
| `AGENTS.md` | You are an AI agent, or you want the rules and the checklist in one page |
| `docs/connector-guide.md` (repository) | Step by step: how to investigate a site, map its data, test and ship |
| `docs/connectors.md` (repository) | Reference: every manifest field, the result statuses, the admin page |
| `manifest.yaml`, `connector.py`, `test_connector.py` | The three files you edit; every line is commented |
| `manifest.schema.json` | Validate `manifest.yaml` in your editor; regenerate with `python -m app.connectors.build --schema` |

## Quick path

1. Copy this folder, rename it after your connector id.
2. Investigate the site (guide section 2) and capture trimmed real fixtures.
3. Edit `manifest.yaml`, then `connector.py` (`collect()`, and `discover()` if there is a directory).
4. Replace the fake shop in `test_connector.py` with your fixtures; run `pytest -q` in this folder.
5. `python -m app.connectors.build path/to/your-folder` makes `<id>-<version>.zip`.
6. Admin page → **Install a connector** → review the preview → **Install** → set its settings →
   **Enable**.

Rules that apply to every connector: fetch only what was asked, never get around a refusal, secrets
are settings, and unknown is never zero. See `AGENTS.md`.

# Connector template

A working example connector for a fictional shop. To write a connector for a real store:

1. Copy this folder and rename it after your connector id.
2. Edit `manifest.yaml`: the id, the hosts it may contact, its settings, its limits.
3. Edit `connector.py`: `collect()` fetches and parses, `discover()` looks up stores.
4. Edit `test_connector.py`: describe the real API with a fake site and run `pytest -q`.
5. Build the package: `python -m app.connectors.build path/to/your-folder`
6. In the admin page, upload the zip, review the preview, install it, set its settings, enable it.

Read `docs/connectors.md` for the rules and the full manifest reference.

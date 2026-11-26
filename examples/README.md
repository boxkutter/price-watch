# Examples

- [`shopper_client.py`](shopper_client.py): a small Python client for the API (only needs `httpx`)
  and a worked demo against the built-in demo shop. Read it next to
  [`../docs/api.md`](../docs/api.md), the API guide.

```bash
python examples/shopper_client.py --base-url http://localhost:8085 \
    --user-token "$USER_TOKEN" --client-token "$CLIENT_TOKEN"
```

Get the two tokens by opening the **example client** page (`/demo`) after signing in on `/admin`
(its "API calls" tab shows how they are sent), or follow section 2 of the guide to create your own
user and API client.

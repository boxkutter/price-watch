"""A small client for the Retail Monitor API: copy it, or read it as the API guide in code.

It needs only `httpx`. It shows the patterns every client needs:
  - authenticating with a user token and a client token on every call
  - the error envelope, and honouring Retry-After
  - idempotency keys on creating calls
  - the asynchronous job pattern (a 202 with a job id, then polling)
  - adding a product by its page link, comparing a barcode across stores, linking a barcode
  - the event feed with its cursor
  - verifying a webhook signature

Run the demo against a service that has the demo shop (no real store is contacted):

    python examples/shopper_client.py --base-url http://localhost:8085 \
        --user-token "$USER_TOKEN" --client-token "$CLIENT_TOKEN"

The full guide is docs/api.md (served by the service at /api-guide).
"""
import argparse
import hashlib
import hmac
import os
import time
import uuid
from collections.abc import Callable, Iterator

import httpx

RUNNING = ("queued", "running", "retry_wait")


class ApiError(Exception):
    """An error response, in the service's one envelope: code, message, details, request_id."""

    def __init__(self, status: int, body: dict, retry_after: int | None = None):
        super().__init__(body.get("message") or f"HTTP {status}")
        self.status, self.body, self.retry_after = status, body, retry_after
        self.code, self.details = body.get("code"), body.get("details") or []
        self.request_id = body.get("request_id")


class BarcodeBelongsToAnotherProduct(Exception):
    """Linking a barcode found that it belongs to another product. This is a question for the
    person: if the two are the same item, call link_barcode again with merge=True."""

    def __init__(self, existing_product: dict):
        super().__init__(f"That barcode already belongs to '{existing_product['name']}'")
        self.existing_product = existing_product


class RetailMonitor:
    def __init__(self, http: httpx.Client, user_token: str, client_token: str | None = None,
                 base_url: str = "", on_wait: Callable[[], None] | None = None):
        """`http` is any httpx.Client. `on_wait` replaces the sleep between job polls (tests use
        it to run the worker)."""
        self.http, self.base_url, self.on_wait = http, base_url.rstrip("/"), on_wait
        self.headers = {"Authorization": f"Bearer {user_token}"}
        if client_token:
            self.headers["X-Client-Token"] = client_token  # the app's own credential

    @classmethod
    def connect(cls, base_url: str, user_token: str, client_token: str | None = None) -> "RetailMonitor":
        return cls(httpx.Client(timeout=30), user_token, client_token, base_url)

    # ---- plumbing ----------------------------------------------------------------------------
    def request(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None,
                idempotency_key: str | None = None, ok: tuple[int, ...] = (200, 201, 202, 204)):
        headers = dict(self.headers)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        for attempt in (1, 2):
            response = self.http.request(method, self.base_url + path, json=json, params=params,
                                         headers=headers)
            if response.status_code == 429 and attempt == 1:
                wait = int(response.headers.get("Retry-After", "1"))
                if wait <= 30:  # a short back-off is worth waiting for; a long one is the caller's call
                    time.sleep(wait)
                    continue
            break
        body = {} if response.status_code == 204 or not response.content else response.json()
        if response.status_code not in ok:
            raise ApiError(response.status_code, body, int(response.headers.get("Retry-After", 0)) or None)
        return {**body, "_status": response.status_code} if isinstance(body, dict) else body

    def wait_job(self, job_id: str, timeout: float = 60.0) -> dict:
        """Poll a job until it stops. Only state == 'succeeded' means the whole request was answered."""
        deadline = time.monotonic() + timeout
        while True:
            job = self.request("GET", f"/v1/jobs/{job_id}")
            if job["state"] not in RUNNING:
                return job
            if time.monotonic() > deadline:
                raise TimeoutError(f"job {job_id} did not finish in {timeout} seconds")
            (self.on_wait or (lambda: time.sleep(1.5)))()

    @staticmethod
    def _key() -> str:
        return str(uuid.uuid4())

    # ---- account and stores ------------------------------------------------------------------
    def me(self) -> dict:
        return self.request("GET", "/v1/me")

    def stores(self) -> list[dict]:
        return self.request("GET", "/v1/stores")["items"]

    def my_stores(self) -> list[dict]:
        return self.request("GET", "/v1/me/stores")["items"]

    def choose_store(self, store_id: str) -> None:
        self.request("PUT", f"/v1/me/stores/{store_id}")

    def forget_store(self, store_id: str) -> None:
        self.request("DELETE", f"/v1/me/stores/{store_id}")

    # ---- adding products ---------------------------------------------------------------------
    def add_by_link(self, url: str, store_id: str | None = None, maximum_price: str | None = None,
                    name: str | None = None) -> dict:
        """Add a product from its page link. Returns the job result, or for a product already
        watched {"already_monitored": True, "monitor_id": ...}. Raises if the store lacks it."""
        body = {"url": url, **({"store_id": store_id} if store_id else {}),
                **({"maximum_price": maximum_price} if maximum_price else {}),
                **({"name": name} if name else {})}
        first = self.request("POST", "/v1/monitors/from-url", json=body, idempotency_key=self._key())
        if first.get("already_monitored"):
            return {"already_monitored": True, "monitor_id": first["monitor"]["id"]}
        job = self.wait_job(first["job_id"])
        if job["state"] != "succeeded":
            raise RuntimeError(f"could not add the product: {job['state']} "
                               f"({(job.get('result') or {}).get('detail')})")
        return job["result"]

    def search(self, query: str, store_ids: list[str]) -> list[dict]:
        first = self.request("POST", "/v1/search/jobs", idempotency_key=self._key(),
                             json={"query": query, "store_ids": store_ids})
        return (self.wait_job(first["job_id"]).get("result") or {}).get("items", [])

    def watch(self, listing_id: str, store_id: str, name: str, maximum_price: str | None = None) -> dict:
        conditions = {"listing_id": listing_id, **({"maximum_price": maximum_price} if maximum_price else {})}
        return self.request("POST", "/v1/monitors", idempotency_key=self._key(),
                            json={"name": name, "type": "exact_product", "store_id": store_id,
                                  "conditions": conditions, "interval_minutes": 60})

    # ---- the watchlist -----------------------------------------------------------------------
    def watchlist(self, include_removed: bool = False) -> list[dict]:
        return self.request("GET", "/v1/monitors", params={"include_removed": include_removed})["items"]

    def history(self, listing_id: str) -> list[dict]:
        return self.request("GET", f"/v1/listings/{listing_id}/prices")["items"]

    def check_now(self, monitor_id: str) -> dict:
        first = self.request("POST", f"/v1/monitors/{monitor_id}/check", idempotency_key=self._key())
        return self.wait_job(first["job_id"])

    def events(self, after: str | None = None) -> Iterator[dict]:
        """Every event after a cursor, oldest first. Keep the last id to catch up later."""
        while True:
            page = self.request("GET", "/v1/events", params={"limit": 200, **({"after": after} if after else {})})
            yield from page["items"]
            after = page["next_cursor"]
            if not after:
                return

    def remove(self, monitor_id: str, permanent: bool = False) -> None:
        """Stop watching and leave the list; events are kept unless permanent=True."""
        self.request("DELETE", f"/v1/monitors/{monitor_id}", params={"permanent": permanent})

    # ---- barcodes ----------------------------------------------------------------------------
    def compare(self, gtin: str, here_store_id: str | None = None, store_ids: list[str] | None = None,
                refresh: bool = True) -> tuple[dict, dict | None]:
        """Standing in a store with a barcode. Returns (known, live): `known` is what the
        catalogue already knows, available at once; `live` is the finished job result."""
        body = {"gtin": gtin, "refresh": refresh,
                **({"here_store_id": here_store_id} if here_store_id else {}),
                **({"store_ids": store_ids} if store_ids else {})}
        known = self.request("POST", "/v1/compare/barcode", json=body, idempotency_key=self._key())
        if not refresh:
            return known, None
        return known, self.wait_job(known["job_id"]).get("result")

    def link_barcode(self, monitor_id: str, gtin: str, merge: bool = False) -> dict:
        """Link a scanned barcode to a monitor's product. If it belongs to another product this
        raises BarcodeBelongsToAnotherProduct: ask the person, then retry with merge=True."""
        try:
            return self.request("POST", f"/v1/monitors/{monitor_id}/barcode", json={"gtin": gtin, "merge": merge})
        except ApiError as error:
            if error.status == 409 and error.details and "existing_product" in error.details[0]:
                raise BarcodeBelongsToAnotherProduct(error.details[0]["existing_product"]) from error
            raise


def verify_webhook(secret: str, headers: dict, raw_body: bytes, tolerance: int = 300) -> bool:
    """Check a webhook delivery: HMAC-SHA256 over timestamp + "." + the exact raw body, with a
    freshness window. Also deduplicate on the X-Retail-Delivery header (delivery is at least once)."""
    try:
        timestamp = headers["X-Retail-Timestamp"]
        if abs(time.time() - int(timestamp)) > tolerance:
            return False
        expected = "v1=" + hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body,
                                    hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, headers["X-Retail-Signature"])
    except (KeyError, ValueError):
        return False


# ---- a worked example against the demo shop ---------------------------------------------------
DEMO_LINK = "https://demo.example.org/p/oats-1kg"
DEMO_BARCODE = "9999990000019"


def demo(api: RetailMonitor, say: Callable[[str], None] = print) -> dict:
    """Add a demo product by link, watch it move, compare it by barcode, then remove it."""
    me = api.me()
    say(f"Signed in as {me['email']} with scopes {', '.join(me['scopes'])}")
    shops = {s["name"]: s for s in api.stores() if s["retailer_id"] == "test.demoshop"}
    north, south = shops["Demo Shop North"], shops["Demo Shop South"]
    for store in (north, south):
        api.choose_store(store["id"])
    say(f"Comparing across: {', '.join(s['name'] for s in api.my_stores())}")

    added = api.add_by_link(DEMO_LINK, store_id=north["id"], maximum_price="25")
    product = added["product"]
    say(f"Added by link: {product['name']} at {product['current_price']} {product['currency']}"
        f" ({product['unit_price']['amount']} per {product['unit_price']['unit']})")
    monitor_id = added["monitor_id"]

    for _ in range(2):  # prices move every minute in the demo shop; each check records one
        job = api.check_now(monitor_id)
        say(f"Checked: {job['state']}")
        time.sleep(1)
    shown = next(m for m in api.watchlist() if m["id"] == monitor_id)
    say(f"Watchlist: {shown['name']} {shown['latest']['current_price']} (was {shown['latest']['previous_price']})")

    _known, live = api.compare(DEMO_BARCODE, here_store_id=north["id"])
    for row in live["rows"]:
        say(f"  {row['store_name']}: {row.get('current_price')} {row['status']}")
    if live.get("saving_vs_here"):
        say(f"Cheaper at {live['cheapest']['store_name']}: {live['saving_vs_here']['amount']} kr "
            f"({live['saving_vs_here']['percent']}%) versus where you are")

    events = list(api.events())
    say(f"{len(events)} event(s) so far, newest: {events[-1]['type'] if events else 'none'}")
    api.remove(monitor_id)
    say("Removed (its events are kept)")
    return {"monitor_id": monitor_id, "product": product, "compare": live, "events": events}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Retail Monitor demo client.")
    parser.add_argument("--base-url", default=os.environ.get("RETAIL_MONITOR_URL", "http://localhost:8085"))
    parser.add_argument("--user-token", default=os.environ.get("RETAIL_MONITOR_USER_TOKEN"))
    parser.add_argument("--client-token", default=os.environ.get("RETAIL_MONITOR_CLIENT_TOKEN"))
    args = parser.parse_args(argv)
    if not args.user_token:
        parser.error("a user token is required (--user-token or RETAIL_MONITOR_USER_TOKEN)")
    demo(RetailMonitor.connect(args.base_url, args.user_token, args.client_token))


if __name__ == "__main__":
    main()

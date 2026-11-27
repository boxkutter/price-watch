"""Polite HTTP client shared by live connectors.

Enforces the connector's host allowlist (including on redirects, which are refused),
a minimum interval between requests, bounded timeouts and response sizes, and an honest
user-agent. It never retries a refusal and never changes how it identifies itself.
"""
import json
import time
from collections.abc import Callable
from decimal import Decimal
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import settings
from app.connectors.base import (
    AccessBlocked,
    BudgetExceeded,
    ConnectorError,
    ExtractionError,
    HttpStatusError,
    RateLimited,
    TransientError,
)

MAX_RESPONSE_BYTES = 5_000_000
MAX_REDIRECTS = 2


def host_allowed(host: str, allowed: list[str]) -> bool:
    host = host.lower()
    return any(host == pattern or (pattern.startswith("*.") and host.endswith(pattern[1:]))
               for pattern in (item.lower() for item in allowed))


class RateLimiter:
    """Blocks until `interval` seconds have passed since the previous call."""

    def __init__(self, interval: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.interval, self.clock, self.sleep = interval, clock, sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self.interval - (self.clock() - self._last)
            if remaining > 0:
                self.sleep(remaining)
        self._last = self.clock()


class PoliteClient:
    def __init__(self, allowed_hosts: list[str], min_interval: float, timeout: float = 15.0,
                 transport: httpx.BaseTransport | None = None,
                 limiter: RateLimiter | None = None, user_agent: str | None = None,
                 follow_redirects: bool = False, headers: dict[str, str] | None = None):
        if not allowed_hosts:
            raise ValueError("A live connector needs at least one allowed host")
        self.allowed_hosts = list(allowed_hosts)  # a private copy: callers cannot widen it later
        # Only a redirect to another allowed host is ever followed (and only when the connector
        # opted in); anywhere else it is a refusal, as it may be a block or login page.
        self.follow_redirects = follow_redirects
        self.limiter = limiter or RateLimiter(min_interval)
        self._client = httpx.Client(timeout=timeout, follow_redirects=False, transport=transport,
                                    headers={"User-Agent": user_agent or settings.connector_user_agent,
                                             "Accept": "application/json", **(headers or {})})
        # Headers the manifest sets (a retailer-approved request profile) win over per-call defaults.
        self._fixed = {name.lower() for name in (headers or {})}
        self.requests_made = 0  # lifetime counter; connectors report per-scan deltas
        self._budget: int | None = None

    def begin_scan(self, max_requests: int) -> None:
        """Cap the number of requests until the next begin_scan() call."""
        self._budget = max_requests

    def budget_left(self) -> int:
        return 10**9 if self._budget is None else max(0, self._budget)

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None):
        return self.request_json("GET", url, params=params, headers=headers)

    def post_json(self, url: str, body, params: dict | None = None, headers: dict | None = None):
        return self.request_json("POST", url, params=params, headers=headers, body=body)

    def get_text(self, url: str, params: dict | None = None, headers: dict | None = None) -> str:
        """GET a page that is not JSON (HTML, XML). Same host allowlist, spacing, size and
        refusal handling as the JSON methods."""
        headers = {**({} if "accept" in self._fixed else
                      {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}),
                   **(headers or {})}
        return self.request_bytes("GET", url, params=params, headers=headers).decode("utf-8", "replace")

    def request_json(self, method: str, url: str, params: dict | None = None,
                     headers: dict | None = None, body=None):
        raw = self.request_bytes(method, url, params=params, headers=headers, body=body)
        try:
            return json.loads(raw, parse_float=Decimal)
        except ValueError as exc:
            raise ExtractionError("Response is not JSON") from exc

    def request_bytes(self, method: str, url: str, params: dict | None = None,
                      headers: dict | None = None, body=None) -> bytes:
        """The response body. Each hop of an allowed redirect is a request of its own: it is
        counted, spaced and checked against the host allowlist like any other."""
        for _hop in range(MAX_REDIRECTS + 1):
            content, location = self._once(method, url, params, headers, body)
            if location is None:
                return content
            url, params = location, None
        raise AccessBlocked("Too many redirects")

    def _once(self, method: str, url: str, params: dict | None, headers: dict | None,
              body) -> tuple[bytes, str | None]:
        """One request. Returns (body, None), or (b"", next_url) for a redirect we may follow."""
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or not host_allowed(
                parts.hostname, self.allowed_hosts):
            raise ConnectorError(f"Host not allowed: {parts.hostname}")
        if self._budget is not None:
            if self._budget <= 0:
                raise BudgetExceeded("Per-scan request budget exhausted")
            self._budget -= 1
        self.limiter.wait()
        self.requests_made += 1
        try:
            with self._client.stream(method, url, params=params, headers=headers,
                                     json=body) as response:
                status = response.status_code
                if status in (401, 403):
                    raise AccessBlocked(f"HTTP {status}")
                if status == 429:
                    retry_after = response.headers.get("Retry-After", "")
                    raise RateLimited(int(retry_after) if retry_after.isdigit() else None)
                if 300 <= status < 400:
                    target = urljoin(url, response.headers.get("Location", ""))
                    target_host = urlsplit(target).hostname
                    if (self.follow_redirects and method == "GET" and urlsplit(target).scheme == "https"
                            and target_host and host_allowed(target_host, self.allowed_hosts)):
                        return b"", target
                    raise AccessBlocked(f"Unexpected redirect (HTTP {status})")
                if status >= 500 or status in (408, 425):
                    raise TransientError(f"HTTP {status}")
                if status != 200:
                    raise HttpStatusError(status)
                received = bytearray()
                for chunk in response.iter_bytes():
                    received += chunk
                    if len(received) > MAX_RESPONSE_BYTES:
                        raise ExtractionError("Response too large")
        except httpx.TimeoutException as exc:
            raise TransientError("Timeout") from exc
        except httpx.HTTPError as exc:
            raise TransientError(type(exc).__name__) from exc
        return bytes(received), None

    def close(self) -> None:
        self._client.close()

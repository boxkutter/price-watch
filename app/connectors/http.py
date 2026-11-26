"""Polite HTTP client shared by live connectors.

Enforces the connector's host allowlist (including on redirects, which are refused),
a minimum interval between requests, bounded timeouts and response sizes, and an honest
user-agent. It never retries a refusal and never changes how it identifies itself.
"""
import json
import time
from collections.abc import Callable
from decimal import Decimal
from urllib.parse import urlsplit

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
                 limiter: RateLimiter | None = None, user_agent: str | None = None):
        if not allowed_hosts:
            raise ValueError("A live connector needs at least one allowed host")
        self.allowed_hosts = allowed_hosts
        self.limiter = limiter or RateLimiter(min_interval)
        self._client = httpx.Client(timeout=timeout, follow_redirects=False, transport=transport,
                                    headers={"User-Agent": user_agent or settings.connector_user_agent,
                                             "Accept": "application/json"})
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

    def request_json(self, method: str, url: str, params: dict | None = None,
                     headers: dict | None = None, body=None):
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
                    raise AccessBlocked(f"Unexpected redirect (HTTP {status})")
                if status >= 500 or status in (408, 425):
                    raise TransientError(f"HTTP {status}")
                if status != 200:
                    raise HttpStatusError(status)
                body = bytearray()
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ExtractionError("Response too large")
        except httpx.TimeoutException as exc:
            raise TransientError("Timeout") from exc
        except httpx.HTTPError as exc:
            raise TransientError(type(exc).__name__) from exc
        try:
            return json.loads(body, parse_float=Decimal)
        except ValueError as exc:
            raise ExtractionError("Response is not JSON") from exc

    def close(self) -> None:
        self._client.close()

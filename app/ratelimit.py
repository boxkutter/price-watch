"""Sliding-window limiter for our own API (in-process; the API runs as a single process)."""
import time
from collections import deque
from collections.abc import Callable


class SlidingWindow:
    def __init__(self, limit: int, window: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str) -> int | None:
        """Record a hit. Returns seconds to wait if the caller is over the limit, else None."""
        now = self.clock()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return max(1, int(self.window - (now - hits[0])) + 1)
        hits.append(now)
        if len(self._hits) > 10_000:  # drop idle keys so memory stays bounded
            for stale in [k for k, v in self._hits.items() if not v or now - v[-1] >= self.window]:
                self._hits.pop(stale, None)
        return None

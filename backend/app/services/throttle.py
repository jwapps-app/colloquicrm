"""In-process sliding-window rate limiter — single instance by design, shared
by the login throttle, the public lead forms and attachment uploads.

Each limiter keeps a dict of key -> recent event timestamps. The table is
bounded WITHOUT ever wiping live counters: when it grows past max_keys, keys
whose every entry has expired go first, then the keys whose most recent event
is oldest. A spray of one-off addresses is what gets evicted; an account (or
address) actually under attack keeps its lock. The previous `.clear()` on
overflow handed an attacker a reset button — flood 10k junk keys and every
lock in the table vanished.
"""

import time


class SlidingWindowLimiter:
    def __init__(self, window_seconds: int, limit: int, max_keys: int = 10_000):
        self.window = window_seconds
        self.limit = limit
        self.max_keys = max_keys
        self._events: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._events.get(key, ()) if now - t < self.window]
        if recent:
            self._events[key] = recent
        else:
            # Don't leave empty lists behind — every key we've ever looked at
            # would otherwise live in the table until eviction.
            self._events.pop(key, None)
        return recent

    def exceeded(self, keys: list[str], limit: int | None = None) -> bool:
        """True if ANY key has hit the limit inside the window. A caller can
        pass a tighter limit for a key that deserves less rope (a 6-digit
        TOTP code) while sharing the window and the table."""
        now = time.monotonic()
        cap = self.limit if limit is None else limit
        return any(len(self._recent(key, now)) >= cap for key in keys)

    def record(self, keys: list[str]) -> None:
        now = time.monotonic()
        for key in keys:
            self._events.setdefault(key, []).append(now)
        self._evict(now)

    def _evict(self, now: float) -> None:
        if len(self._events) <= self.max_keys:
            return
        # Expired keys first — their locks are already over.
        for key in [k for k, ts in self._events.items() if now - ts[-1] >= self.window]:
            del self._events[key]
        overflow = len(self._events) - self.max_keys
        if overflow <= 0:
            return
        # Still over: drop the keys that have been quiet the longest. The
        # ones being hammered right now (newest last event) survive.
        for key in sorted(self._events, key=lambda k: self._events[k][-1])[:overflow]:
            del self._events[key]

    def __len__(self) -> int:
        return len(self._events)

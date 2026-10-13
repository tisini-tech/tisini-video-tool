"""Small in-memory limits. One server process holds all the state.

``CallLimiter`` limits how many calls one key may make in a time window.
``KeyGuard`` blocks an address after repeated wrong API keys.

Both take a clock, so tests can move time without waiting.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable


class CallLimiter:
    """Allow ``limit`` calls per ``window`` seconds for each key."""

    def __init__(
        self,
        limit: int,
        window: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> int:
        """Record a call. Return 0 if allowed, else the seconds to wait.

        A refused call is not recorded, so waiting always works.
        """

        now = self._clock()

        with self._lock:
            hits = self._hits.get(key)

            if hits is None:
                self._prune(now)
                hits = self._hits[key] = deque()

            cutoff = now - self._window

            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= self._limit:
                return max(1, math.ceil(hits[0] + self._window - now))

            hits.append(now)

            return 0

    def _prune(self, now: float) -> None:
        if len(self._hits) < self._max_keys:
            return

        cutoff = now - self._window

        for key, hits in list(self._hits.items()):
            while hits and hits[0] <= cutoff:
                hits.popleft()

            if not hits:
                del self._hits[key]

        while len(self._hits) >= self._max_keys:
            del self._hits[next(iter(self._hits))]


class KeyGuard:
    """Block an address after ``max_fails`` wrong keys in ``window`` seconds."""

    def __init__(
        self,
        max_fails: int,
        window: float,
        block: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        self._max_fails = max_fails
        self._window = window
        self._block = block
        self._clock = clock
        self._max_keys = max_keys
        self._fails: dict[str, deque[float]] = {}
        self._blocked: dict[str, float] = {}
        self._lock = threading.Lock()

    def blocked_for(self, address: str) -> int:
        """Return the seconds left in a block, or 0 if there is none."""

        now = self._clock()

        with self._lock:
            until = self._blocked.get(address)

            if until is None:
                return 0

            if until <= now:
                del self._blocked[address]

                return 0

            return math.ceil(until - now)

    def fail(self, address: str) -> None:
        """Record one wrong key from ``address``."""

        now = self._clock()

        with self._lock:
            hits = self._fails.setdefault(address, deque())
            cutoff = now - self._window

            while hits and hits[0] <= cutoff:
                hits.popleft()

            hits.append(now)

            if len(hits) >= self._max_fails:
                self._blocked[address] = now + self._block
                del self._fails[address]

            self._prune(now)

    def _prune(self, now: float) -> None:
        if len(self._fails) + len(self._blocked) <= self._max_keys:
            return

        cutoff = now - self._window

        for address, hits in list(self._fails.items()):
            while hits and hits[0] <= cutoff:
                hits.popleft()

            if not hits:
                del self._fails[address]

        for address, until in list(self._blocked.items()):
            if until <= now:
                del self._blocked[address]

        while len(self._fails) + len(self._blocked) > self._max_keys:
            if self._fails:
                del self._fails[next(iter(self._fails))]
            else:
                del self._blocked[next(iter(self._blocked))]

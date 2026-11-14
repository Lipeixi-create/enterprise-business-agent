from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any


class ResultStore:
    """Small bounded in-memory store for synchronous query results.

    This is deliberately not a task queue. Results are scoped to the
    authenticated principal so knowing a request_id is not enough to read
    another caller's result.
    """

    def __init__(self, max_items: int = 500, ttl_seconds: float = 3600.0):
        self.max_items = max_items
        self.ttl_seconds = ttl_seconds
        self._items: OrderedDict[str, tuple[float, dict[str, Any], str]] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, request_id: str, payload: dict[str, Any], owner: str = "anonymous") -> None:
        now = time.monotonic()
        with self._lock:
            self._items[request_id] = (now, payload, owner)
            self._items.move_to_end(request_id)
            self._evict(now)

    def get(self, request_id: str, owner: str | None = None) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._lock:
            stored = self._items.get(request_id)
            if stored is None:
                return None
            created_at, payload, stored_owner = stored
            if now - created_at > self.ttl_seconds:
                self._items.pop(request_id, None)
                return None
            if owner is not None and stored_owner != owner:
                return None
            return payload

    def _evict(self, now: float) -> None:
        while self._items and (
            len(self._items) > self.max_items or now - next(iter(self._items.values()))[0] > self.ttl_seconds
        ):
            self._items.popitem(last=False)

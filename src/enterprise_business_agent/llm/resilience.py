"""Provider resilience primitives shared by the LLM adapters.

The module owns three concerns that must stay independent of a specific
provider protocol:

* the per-run cancellation event and hard deadline, propagated through a
  ``ContextVar`` so a client call cannot outlive its Agent Run;
* failure classification and bounded exponential backoff;
* a small consecutive-failure circuit breaker.

None of these helpers logs a prompt, response body or API key.
"""

from __future__ import annotations

import contextvars
import threading
import time
from dataclasses import dataclass, field
from typing import Any

# Failure kinds are reported on exceptions and trace events. They are plain
# strings so an archive can be read without importing this module.
FAULT_TIMEOUT = "timeout"
FAULT_CONNECTION = "connection"
FAULT_RATE_LIMIT = "rate_limit"
FAULT_SERVER = "server"
FAULT_AUTH = "auth"
FAULT_PROTOCOL = "protocol"
FAULT_CIRCUIT_OPEN = "circuit_open"
FAULT_CANCELLED = "cancelled"

TRANSIENT_FAULTS = frozenset(
    {FAULT_TIMEOUT, FAULT_CONNECTION, FAULT_RATE_LIMIT, FAULT_SERVER}
)


class CircuitOpenError(Exception):
    """Raised before a request when the breaker refuses to send it."""

    def __init__(self, retry_in_seconds: float):
        self.retry_in_seconds = max(0.0, float(retry_in_seconds))
        super().__init__(
            f"provider circuit is open; retry in {self.retry_in_seconds:.2f}s"
        )


_ACTIVE_CANCEL_EVENT: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "eba_llm_cancel_event", default=None
)
_ACTIVE_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "eba_llm_deadline", default=None
)


def bind_run_context(
    cancel_event: Any, deadline: float | None
) -> tuple[contextvars.Token, contextvars.Token]:
    """Bind the current Agent Run's cancellation event and monotonic deadline.

    The agent graph invokes the LLM client on the same thread that owns the
    run, so a context variable reaches every decision, report and narrative
    call without changing the public ``LLMClient`` protocol.
    """

    cancel_token = _ACTIVE_CANCEL_EVENT.set(cancel_event)
    deadline_token = _ACTIVE_DEADLINE.set(deadline)
    return cancel_token, deadline_token


def reset_run_context(
    tokens: tuple[contextvars.Token, contextvars.Token]
) -> None:
    cancel_token, deadline_token = tokens
    _ACTIVE_DEADLINE.reset(deadline_token)
    _ACTIVE_CANCEL_EVENT.reset(cancel_token)


def active_cancel_event() -> Any:
    return _ACTIVE_CANCEL_EVENT.get()


def active_deadline() -> float | None:
    return _ACTIVE_DEADLINE.get()


def is_cancelled(cancel_event: Any = None) -> bool:
    event = cancel_event if cancel_event is not None else active_cancel_event()
    return event is not None and bool(event.is_set())


def classify_status(status_code: int) -> str:
    if status_code in {401, 403}:
        return FAULT_AUTH
    if status_code == 429:
        return FAULT_RATE_LIMIT
    if status_code >= 500:
        return FAULT_SERVER
    return FAULT_PROTOCOL


def retry_after_seconds(headers: Any, *, maximum: float) -> float | None:
    """Parse an integer/float ``Retry-After``; HTTP dates are ignored."""

    try:
        raw = headers.get("Retry-After")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return min(value, max(0.0, maximum))


def next_retry_delay(
    attempt: int,
    *,
    base_seconds: float,
    max_seconds: float,
    retry_after: float | None = None,
    deadline: float | None = None,
    minimum_window: float,
) -> float | None:
    """Return a backoff delay, or ``None`` when the budget cannot fit it.

    ``attempt`` is zero-based. The caller must not send another request when
    the remaining budget is smaller than the minimum useful request window.
    """

    delay = (
        retry_after
        if retry_after is not None
        else min(max(0.0, base_seconds) * (2**attempt), max(0.0, max_seconds))
    )
    delay = max(0.0, delay)
    if deadline is None:
        return delay
    remaining = deadline - time.monotonic()
    if remaining - delay < minimum_window:
        return None
    return delay


@dataclass
class CircuitBreaker:
    """Consecutive-failure breaker with a single half-open probe.

    The breaker is per client instance. A 401 or a malformed response is a
    contract problem, not provider unavailability, so callers only record
    transient failures.
    """

    failure_threshold: int = 3
    reset_seconds: float = 30.0
    clock: Any = time.monotonic
    _failures: int = 0
    _opened_at: float | None = None
    _probe_in_flight: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def enabled(self) -> bool:
        return self.failure_threshold > 0

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            if self.clock() - self._opened_at >= self.reset_seconds:
                return "half_open"
            return "open"

    def before_call(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._opened_at is None:
                return
            elapsed = self.clock() - self._opened_at
            if elapsed < self.reset_seconds:
                raise CircuitOpenError(self.reset_seconds - elapsed)
            if self._probe_in_flight:
                raise CircuitOpenError(0.0)
            self._probe_in_flight = True

    def record_success(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._probe_in_flight = False

    def record_failure(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._failures += 1
            if self._opened_at is not None:
                # A failed half-open probe reopens immediately.
                self._opened_at = self.clock()
                self._probe_in_flight = False
                return
            if self._failures >= self.failure_threshold:
                self._opened_at = self.clock()

    def reset(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._probe_in_flight = False

"""Hard-bounded HTTP POST shared by the LLM adapters.

``httpx`` read timeouts reset whenever a byte arrives, so a provider that
trickles a body can hold a worker far past the configured limit. The transport
runs each exchange in a daemon thread, waits on a wall-clock deadline, and
closes the client when the deadline expires or the run is cancelled. Closing
the socket is what makes the abandoned read fail instead of lingering.

The module deliberately imports ``httpx`` as a module (not ``from httpx import
Client``) so existing test transports that monkeypatch the shared ``httpx``
module keep working.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import httpx


class TransportCancelled(Exception):
    """The caller cancelled the request while it was in flight."""


_live_threads = 0
_live_lock = threading.Lock()


def active_request_threads() -> int:
    """Number of request threads still alive; used by regression tests."""

    with _live_lock:
        return _live_threads


def _client_timeout(
    connect_timeout: float, read_timeout: float
) -> httpx.Timeout:
    return httpx.Timeout(
        connect=max(0.001, connect_timeout),
        read=max(0.001, read_timeout),
        write=max(0.001, read_timeout),
        pool=max(0.001, connect_timeout),
    )


def _close(client: Any) -> None:
    close = getattr(client, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        pass


def _interrupt(client: Any) -> None:
    """Unblock a reader parked in ``recv`` and then close the client.

    Closing an httpx client from another thread does not always interrupt a
    blocking read: on macOS the kernel keeps the file description alive for
    the duration of the read, so the worker can linger for its whole read
    timeout. ``shutdown`` wakes the reader immediately. The attribute walk is
    best-effort because these are httpx/httpcore internals; ``close`` is the
    fallback and the read timeout is the final bound.
    """

    pool = getattr(getattr(client, "_transport", None), "_pool", None)
    for connection in getattr(pool, "connections", None) or []:
        inner = getattr(connection, "_connection", None) or connection
        stream = getattr(inner, "_network_stream", None)
        sock = getattr(stream, "_sock", None)
        if sock is None:
            continue
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    if client is None:
        return
    # ``close`` waits on httpcore's connection locks.  A worker parked in a
    # TLS handshake can hold those locks for minutes, so closing here would
    # defeat the total timeout we just enforced.  Close off-thread and let the
    # caller surface the timeout immediately.
    threading.Thread(
        target=_close, args=(client,), name="eba-llm-close", daemon=True
    ).start()


def post_json(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    connect_timeout: float,
    read_timeout: float,
    total_timeout: float,
    cancel_event: Any = None,
) -> httpx.Response:
    """POST JSON with connect/read/total bounds and cooperative cancellation.

    ``total_timeout`` is a wall-clock promise for the whole exchange,
    independent of how many bytes the provider has already sent.
    """

    global _live_threads

    outcome: dict[str, Any] = {}
    holder: dict[str, Any] = {}
    timeout = _client_timeout(connect_timeout, read_timeout)

    def call() -> None:
        global _live_threads
        with _live_lock:
            _live_threads += 1
        client = None
        try:
            client = httpx.Client(timeout=timeout)
            holder["client"] = client
            if cancel_event is not None and cancel_event.is_set():
                return
            with client:
                outcome["response"] = client.post(
                    url, headers=headers, json=payload
                )
        except BaseException as exc:  # surfaced on the caller thread
            outcome["error"] = exc
        finally:
            _close(client)
            with _live_lock:
                _live_threads -= 1

    worker = threading.Thread(target=call, name="eba-llm-http", daemon=True)
    worker.start()
    hard_deadline = time.monotonic() + max(0.001, total_timeout)
    timed_out = False
    cancelled = False
    while worker.is_alive():
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        remaining = hard_deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        worker.join(min(0.05, remaining))

    if cancelled or timed_out:
        # Shutting the socket down is the only way to interrupt a blocking
        # read on a synchronous socket in Python.
        _interrupt(holder.get("client"))
        worker.join(0.25)
        if cancelled:
            raise TransportCancelled("LLM request was cancelled")
        raise httpx.ReadTimeout(
            f"LLM request did not complete within {total_timeout:.2f}s"
        )

    worker.join()
    error = outcome.get("error")
    if error is not None:
        raise error
    response = outcome.get("response")
    if response is None:
        raise httpx.HTTPError("LLM request produced no response")
    return response

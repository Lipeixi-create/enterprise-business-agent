"""Phase 9.2.1 reliability tests.

These tests use a real loopback HTTP server, not a mocked transport, so the
connect/read/total deadlines, socket close on cancellation and retry policy are
exercised against blocking sockets. No external provider is contacted.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from enterprise_business_agent.config import Settings
from enterprise_business_agent.llm import (
    GLMClient,
    LLMCancelled,
    LLMCircuitOpen,
    LLMTimeout,
    LLMUnavailable,
)
from enterprise_business_agent.llm.resilience import bind_run_context, reset_run_context
from enterprise_business_agent.llm.transport import _interrupt, active_request_threads


@pytest.fixture(autouse=True)
def _bypass_system_proxy(monkeypatch):
    # httpx honours the host's system proxy settings by default; without an
    # exclusion these loopback requests are answered by the proxy instead of
    # the local test server.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def _ok_body(text: str = '{"ok": true}') -> bytes:
    return json.dumps(
        {
            "model": "glm-5.3-flash",
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }
    ).encode()


class _Provider:
    def __init__(self) -> None:
        self.requests = 0
        self.lock = threading.Lock()
        self.responder = lambda count, handler: handler.write_json(200, _ok_body())
        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: D102 - silence test output
                return

            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                with provider.lock:
                    provider.requests += 1
                    count = provider.requests
                try:
                    provider.responder(count, self)
                except (BrokenPipeError, ConnectionResetError):
                    return

            def write_json(self, status: int, body: bytes, headers=None) -> None:
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)
                    self.wfile.flush()

            def write_trickle(self, body: bytes, delay: float) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                for byte in body:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(delay)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="test-provider", daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/api/paas/v4"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1.0)


@pytest.fixture()
def provider():
    instance = _Provider()
    try:
        yield instance
    finally:
        instance.close()


def _settings(provider: _Provider, **overrides) -> Settings:
    values = {
        "llm_provider": "glm",
        "llm_model": "glm-5.3-flash",
        "llm_base_url": provider.base_url,
        "llm_api_key": "test-key",
        "llm_timeout_seconds": 5.0,
        "llm_connect_timeout_seconds": 1.0,
        "llm_read_timeout_seconds": 5.0,
        "llm_max_retries": 0,
        "llm_retry_backoff_base_seconds": 0.01,
        "llm_retry_backoff_max_seconds": 0.05,
        "llm_retry_after_max_seconds": 2.0,
        "llm_circuit_failure_threshold": 0,
    }
    values.update(overrides)
    return Settings(**values)


def _wait_for(predicate, timeout: float = 1.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_total_deadline_bounds_a_trickling_response(provider):
    provider.responder = lambda count, handler: handler.write_trickle(_ok_body(), delay=0.15)
    client = GLMClient(
        _settings(
            provider,
            llm_timeout_seconds=1.0,
            llm_read_timeout_seconds=10.0,
            llm_max_retries=0,
        )
    )
    started = time.monotonic()
    with pytest.raises(LLMTimeout) as excinfo:
        client.complete_json(system="s", prompt="p")
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert excinfo.value.kind == "timeout"
    assert _wait_for(lambda: active_request_threads() == 0)


def test_interrupt_does_not_wait_for_a_blocked_client_close():
    class SlowClose:
        def close(self):
            time.sleep(3.0)

    started = time.monotonic()
    _interrupt(SlowClose())
    assert time.monotonic() - started < 0.5


def test_read_timeout_is_bounded_even_without_a_run_deadline(provider):
    def slow(count, handler):
        time.sleep(2.0)
        handler.write_json(200, _ok_body())

    provider.responder = slow
    client = GLMClient(
        _settings(
            provider,
            llm_timeout_seconds=0.6,
            llm_read_timeout_seconds=2.0,
            llm_max_retries=0,
        )
    )
    started = time.monotonic()
    with pytest.raises(LLMTimeout):
        client.complete_json(system="s", prompt="p")
    assert time.monotonic() - started < 1.8


def test_connection_refusal_is_retried_then_classified(provider):
    # A bound-then-closed port refuses immediately.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    client = GLMClient(
        _settings(
            provider,
            llm_base_url=f"http://127.0.0.1:{port}/api/paas/v4",
            llm_max_retries=2,
            llm_retry_backoff_base_seconds=0.01,
            llm_retry_backoff_max_seconds=0.01,
        )
    )
    with pytest.raises(LLMUnavailable) as excinfo:
        client.complete_json(system="s", prompt="p")
    assert excinfo.value.attempts == 3
    assert excinfo.value.kind == "connection"


def test_authentication_failure_is_not_retried(provider):
    provider.responder = lambda count, handler: handler.write_json(401, b'{"error":{"message":"bad key"}}')
    client = GLMClient(_settings(provider, llm_max_retries=3))
    with pytest.raises(LLMUnavailable) as excinfo:
        client.complete_json(system="s", prompt="p")
    assert excinfo.value.kind == "auth"
    assert provider.requests == 1


def test_retry_after_is_honoured_and_capped(provider):
    def responder(count, handler):
        if count == 1:
            handler.write_json(429, b"{}", headers={"Retry-After": "0.20"})
            return
        handler.write_json(200, _ok_body())

    provider.responder = responder
    client = GLMClient(
        _settings(
            provider,
            llm_max_retries=1,
            llm_retry_backoff_base_seconds=5.0,
            llm_retry_backoff_max_seconds=5.0,
            llm_retry_after_max_seconds=1.0,
        )
    )
    started = time.monotonic()
    result = client.complete_json(system="s", prompt="p")
    elapsed = time.monotonic() - started
    assert result.data == {"ok": True}
    assert provider.requests == 2
    assert 0.15 <= elapsed < 2.0


def test_circuit_opens_after_consecutive_transient_failures(provider):
    provider.responder = lambda count, handler: handler.write_json(500, b"{}")
    client = GLMClient(
        _settings(
            provider,
            llm_max_retries=0,
            llm_circuit_failure_threshold=2,
            llm_circuit_reset_seconds=30.0,
        )
    )
    for _ in range(2):
        with pytest.raises(LLMUnavailable) as excinfo:
            client.complete_json(system="s", prompt="p")
        assert excinfo.value.kind == "server"
    assert provider.requests == 2

    with pytest.raises(LLMCircuitOpen):
        client.complete_json(system="s", prompt="p")
    assert provider.requests == 2
    assert client._breaker.state == "open"


def test_circuit_half_open_probe_recovers(provider):
    provider.responder = lambda count, handler: handler.write_json(500, b"{}")
    client = GLMClient(
        _settings(
            provider,
            llm_max_retries=0,
            llm_circuit_failure_threshold=1,
            llm_circuit_reset_seconds=0.15,
        )
    )
    with pytest.raises(LLMUnavailable):
        client.complete_json(system="s", prompt="p")
    with pytest.raises(LLMCircuitOpen):
        client.complete_json(system="s", prompt="p")

    time.sleep(0.2)
    provider.responder = lambda count, handler: handler.write_json(200, _ok_body())
    assert client.complete_json(system="s", prompt="p").data == {"ok": True}
    assert client._breaker.state == "closed"


def test_cancellation_aborts_an_in_flight_request(provider):
    def slow(count, handler):
        time.sleep(2.0)
        handler.write_json(200, _ok_body())

    provider.responder = slow
    event = threading.Event()
    tokens = bind_run_context(event, None)
    client = GLMClient(_settings(provider, llm_timeout_seconds=5.0, llm_max_retries=2))
    timer = threading.Timer(0.15, event.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(LLMCancelled):
            client.complete_with_tools(system="s", messages=[{"role": "user", "content": "q"}], tools=[])
    finally:
        timer.cancel()
        reset_run_context(tokens)
    assert time.monotonic() - started < 1.5
    assert provider.requests == 1
    assert _wait_for(lambda: active_request_threads() == 0)


def test_run_hard_deadline_surfaces_as_a_task_deadline(provider):
    def slow(count, handler):
        time.sleep(3.0)
        handler.write_json(200, _ok_body())

    provider.responder = slow
    tokens = bind_run_context(None, time.monotonic() + 1.5)
    client = GLMClient(
        _settings(
            provider,
            llm_timeout_seconds=5.0,
            llm_read_timeout_seconds=5.0,
            llm_max_retries=0,
        )
    )
    started = time.monotonic()
    try:
        with pytest.raises(LLMTimeout) as excinfo:
            client.complete_json(system="s", prompt="p")
    finally:
        reset_run_context(tokens)
    assert time.monotonic() - started < 2.5
    assert getattr(excinfo.value, "task_deadline", False) is True


def test_provider_timeout_early_in_a_long_run_is_not_a_task_deadline(provider):
    # The agent loop passes its remaining run budget as the request window, so
    # the window equals the run deadline. A read timeout long before that
    # deadline is a provider failure and must stay retryable.
    def slow(count, handler):
        time.sleep(2.0)
        handler.write_json(200, _ok_body())

    provider.responder = slow
    tokens = bind_run_context(None, time.monotonic() + 30.0)
    client = GLMClient(
        _settings(
            provider,
            llm_timeout_seconds=30.0,
            llm_read_timeout_seconds=0.4,
            llm_max_retries=0,
        )
    )
    started = time.monotonic()
    try:
        with pytest.raises(LLMTimeout) as excinfo:
            client.complete_with_tools(
                system="s",
                messages=[{"role": "user", "content": "q"}],
                tools=[],
                timeout_seconds=30.0,
            )
    finally:
        reset_run_context(tokens)
    assert time.monotonic() - started < 2.0
    assert getattr(excinfo.value, "task_deadline", False) is False


def test_budget_is_checked_before_any_socket_is_opened(provider):
    client = GLMClient(_settings(provider))
    with pytest.raises(LLMTimeout) as excinfo:
        client.complete_with_tools(
            system="s",
            messages=[{"role": "user", "content": "q"}],
            tools=[],
            timeout_seconds=0.0,
        )
    assert provider.requests == 0
    assert excinfo.value.attempts == 0
    assert excinfo.value.budget_limited is True

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from ..config import Settings
from .resilience import (
    FAULT_CANCELLED,
    FAULT_CONNECTION,
    FAULT_PROTOCOL,
    FAULT_TIMEOUT,
    CircuitBreaker,
    CircuitOpenError,
    active_cancel_event,
    active_deadline,
    classify_status,
    is_cancelled,
    next_retry_delay,
    retry_after_seconds,
)
from .transport import TransportCancelled, post_json


class LLMError(Exception):
    code = "llm_error"
    retryable = False
    kind = "unknown"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        kind: str | None = None,
        retryable: bool | None = None,
    ):
        self.message = message
        self.status_code = status_code
        if kind is not None:
            self.kind = kind
        if retryable is not None:
            self.retryable = retryable
        super().__init__(message)


class LLMUnavailable(LLMError):
    code = "llm_unavailable"
    retryable = True
    kind = FAULT_CONNECTION


class ModelCallBudgetExceeded(LLMUnavailable):
    """A local hard cap refused the call before it was sent.

    Subclasses ``LLMUnavailable`` so the agent loop turns it into a structured
    ``llm_unavailable`` stop instead of crashing, and so it is never retried.
    The message is the only place the budget is mentioned; no request leaves the
    process after this is raised.
    """

    code = "model_call_budget_exceeded"


class LLMTimeout(LLMError):
    code = "llm_timeout"


class LLMInvalidResponse(LLMError):
    code = "llm_invalid_response"


class LLMTruncated(LLMInvalidResponse):
    """The provider stopped at the token cap before the payload was complete."""

    code = "llm_truncated"


class LLMCancelled(LLMError):
    """The owning run was cancelled while a request was in flight."""

    code = "llm_cancelled"
    retryable = False
    kind = FAULT_CANCELLED


class LLMCircuitOpen(LLMUnavailable):
    """The local breaker refused to send the request."""

    code = "llm_circuit_open"
    retryable = False
    kind = "circuit_open"


@dataclass(frozen=True)
class LLMCallResult:
    data: dict[str, Any]
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float = 0.0
    finish_reason: str | None = None
    truncated: bool = False


@dataclass(frozen=True)
class LLMToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMToolResponse:
    text: str | None
    tool_calls: list[LLMToolCall]
    stop_reason: str | None
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float = 0.0
    truncated: bool = False
    attempts: int = 1


class LLMClient(Protocol):
    model: str

    def complete_json(
        self, *, system: str, prompt: str, max_tokens: int | None = None
    ) -> LLMCallResult:
        ...

    def complete_with_tools(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        timeout_seconds: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMToolResponse:
        ...


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)

_TRUNCATION_REASONS = frozenset({"length", "max_tokens"})


def is_truncated(stop_reason: Any) -> bool:
    """Both Anthropic and GLM report a token cap, with different words."""

    return str(stop_reason or "").strip().lower() in _TRUNCATION_REASONS


MIN_REQUEST_WINDOW_SECONDS = 5.0


def _budgeted_attempt_timeout(
    deadline: float | None,
    configured_timeout: float,
    *,
    minimum: float = MIN_REQUEST_WINDOW_SECONDS,
) -> float | None:
    """Time an attempt may use, or ``None`` when a request is pointless.

    A request started with less than ``minimum`` seconds left cannot finish a
    provider round trip, and a retry with that little time only burns the case
    budget. The configured request timeout is also clamped to what remains.
    """

    if deadline is None:
        return configured_timeout
    remaining = deadline - time.monotonic()
    if remaining < minimum:
        return None
    return min(configured_timeout, remaining)


def _budgeted_retry_delay(
    attempt: int,
    deadline: float | None,
    *,
    base_seconds: float = 0.5,
    max_seconds: float = 2.0,
    retry_after: float | None = None,
    minimum: float = MIN_REQUEST_WINDOW_SECONDS,
) -> float | None:
    """Backoff delay, or ``None`` when the budget cannot fit another attempt."""

    return next_retry_delay(
        attempt,
        base_seconds=base_seconds,
        max_seconds=max_seconds,
        retry_after=retry_after,
        deadline=deadline,
        minimum_window=minimum,
    )


def _mark_error(
    exc: Exception,
    attempts: int,
    *,
    kind: str | None = None,
    budget_limited: bool = False,
    task_deadline: bool = False,
) -> Exception:
    # Attributes rather than fields keep the public exception constructor stable
    # while letting the evaluator report how many attempts were actually sent.
    setattr(exc, "attempts", attempts)
    if kind is not None:
        setattr(exc, "kind", kind)
    if budget_limited:
        setattr(exc, "budget_limited", True)
    if task_deadline:
        setattr(exc, "task_deadline", True)
    return exc


def _mark_budget_limited(exc: Exception, attempts: int) -> Exception:
    return _mark_error(exc, attempts, budget_limited=True)


def _deadline_for(timeout_seconds: float | None) -> float | None:
    """Combine an explicit request window with the owning run's hard deadline."""

    request_deadline = (
        time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    )
    run_deadline = active_deadline()
    if request_deadline is None:
        return run_deadline
    if run_deadline is None:
        return request_deadline
    return min(request_deadline, run_deadline)


# The transport unwinds a few polls after the cutoff, and a caller may compute
# its remaining window a moment after the run deadline was stamped.
_DEADLINE_SLACK_SECONDS = 0.25


def _run_deadline_reached(deadline: float | None) -> bool:
    """Whether the timeout happened because the owning run's budget ran out.

    ``_deadline_for`` returns the earlier of the request window and the run
    deadline. A shorter request window (connect/read/total) is the binding
    constraint, so a timeout there belongs to the provider, not the task
    budget. When the window is bounded by the run deadline, the clock decides:
    a connect timeout two seconds into a 300-second run must still surface as a
    provider timeout and be eligible for a retry.
    """

    run_deadline = active_deadline()
    if deadline is None or run_deadline is None:
        return False
    if deadline < run_deadline - _DEADLINE_SLACK_SECONDS:
        return False
    return time.monotonic() >= run_deadline - _DEADLINE_SLACK_SECONDS


def _run_deadline_leaves_no_room(deadline: float | None) -> bool:
    """Whether the run budget, not the caller's window, blocks a new attempt.

    ``_budgeted_attempt_timeout`` refuses a request with less than
    ``MIN_REQUEST_WINDOW_SECONDS`` left. When that remaining time comes from
    the run deadline the run is over; when it comes from a short explicit
    window the caller still owns time and the failure stays a provider/budget
    error the agent may retry.
    """

    run_deadline = active_deadline()
    if deadline is None or run_deadline is None:
        return False
    if deadline < run_deadline - _DEADLINE_SLACK_SECONDS:
        return False
    return run_deadline - time.monotonic() < MIN_REQUEST_WINDOW_SECONDS


def _extract_json(text: str) -> dict[str, Any]:
    candidate = text.strip()
    fence = _JSON_FENCE.search(candidate)
    if fence:
        candidate = fence.group(1).strip()
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end < start:
        raise LLMInvalidResponse("model response did not contain a JSON object")
    try:
        value = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMInvalidResponse(f"model response was not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise LLMInvalidResponse("model response must be a JSON object")
    return value


def _content_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return list(content)
    return [{"type": "text", "text": str(content or "")}]


def _append_message(
    converted: list[dict[str, Any]], role: str, content: Any
) -> None:
    # Anthropic-compatible providers may reject adjacent same-role messages.
    # Merge them while preserving block order (e.g. tool_result then prompt).
    if converted and converted[-1]["role"] == role:
        merged = _content_blocks(converted[-1]["content"]) + _content_blocks(content)
        converted[-1]["content"] = merged
        return
    converted.append({"role": role, "content": content})


def _to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            _append_message(
                converted,
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.get("tool_call_id"),
                        "content": str(message.get("content") or ""),
                    }
                ],
            )
            continue
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = message.get("content")
            if text:
                blocks.append({"type": "text", "text": str(text)})
            for call in message.get("tool_calls") or []:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id") or call.get("call_id"),
                        "name": call.get("name"),
                        "input": call.get("arguments") or call.get("input") or {},
                    }
                )
            _append_message(converted, "assistant", blocks or str(text or ""))
            continue
        _append_message(converted, "user", str(message.get("content") or ""))
    return converted


class AnthropicClient:
    """Small synchronous Anthropic Messages client with bounded retries.

    The client never logs the API key, prompt, or response body. The planner
    receives parsed JSON only; all database access remains outside this module.
    """

    provider = "anthropic"

    def __init__(self, settings: Settings):
        if not settings.llm_api_key:
            raise LLMUnavailable("ANTHROPIC_API_KEY is not configured")
        self.model = settings.llm_model
        self._api_key = settings.llm_api_key
        self._base_url = settings.llm_base_url
        self._timeout = settings.llm_timeout_seconds
        self._connect_timeout = settings.llm_connect_timeout_seconds
        self._read_timeout = settings.llm_read_timeout_seconds
        self._max_retries = settings.llm_max_retries
        self._max_tokens = settings.llm_max_tokens
        self._backoff_base = settings.llm_retry_backoff_base_seconds
        self._backoff_max = settings.llm_retry_backoff_max_seconds
        self._retry_after_max = settings.llm_retry_after_max_seconds
        self._breaker = CircuitBreaker(
            failure_threshold=settings.llm_circuit_failure_threshold,
            reset_seconds=settings.llm_circuit_reset_seconds,
        )

    def __repr__(self) -> str:
        return f"AnthropicClient(model={self.model!r}, key=<redacted>)"

    def close(self) -> None:
        pass

    def _post(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        total_timeout: float,
    ) -> httpx.Response:
        return post_json(
            url,
            headers=headers,
            payload=payload,
            connect_timeout=min(self._connect_timeout, total_timeout),
            read_timeout=min(self._read_timeout, total_timeout),
            total_timeout=total_timeout,
            cancel_event=active_cancel_event(),
        )

    def _circuit_guard(self) -> None:
        try:
            self._breaker.before_call()
        except CircuitOpenError as exc:
            raise LLMCircuitOpen(str(exc)) from exc

    def complete_json(
        self, *, system: str, prompt: str, max_tokens: int | None = None
    ) -> LLMCallResult:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens or self._max_tokens,
            "temperature": 0,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        started = time.monotonic()
        deadline = _deadline_for(None)
        attempts = 0
        last_error: Exception | None = None
        retry_after: float | None = None
        while True:
            if is_cancelled():
                raise LLMCancelled("LLM request was cancelled")
            self._circuit_guard()
            request_timeout = _budgeted_attempt_timeout(deadline, self._timeout)
            if request_timeout is None:
                raise _mark_error(
                    LLMTimeout(
                        "insufficient remaining budget for a model request",
                        kind=FAULT_TIMEOUT,
                    ),
                    attempts,
                    budget_limited=True,
                    task_deadline=_run_deadline_leaves_no_room(deadline),
                )
            attempts += 1
            try:
                response = self._post(
                    self._base_url, headers, payload, request_timeout
                )
                if response.status_code == 200:
                    self._breaker.record_success()
                    body = response.json()
                    content = body.get("content") or []
                    text = "".join(
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
                    stop_reason = body.get("stop_reason")
                    truncated = is_truncated(stop_reason)
                    try:
                        data = _extract_json(text)
                    except LLMInvalidResponse as exc:
                        if truncated:
                            raise LLMTruncated(
                                "model response reached the output token cap before "
                                "the JSON object was complete"
                            ) from exc
                        raise
                    usage = body.get("usage") or {}
                    return LLMCallResult(
                        data=data,
                        model=str(body.get("model") or self.model),
                        input_tokens=usage.get("input_tokens"),
                        output_tokens=usage.get("output_tokens"),
                        latency_ms=round((time.monotonic() - started) * 1000, 3),
                        finish_reason=stop_reason,
                        truncated=truncated,
                    )
                if response.status_code in {401, 403}:
                    raise LLMUnavailable(
                        "LLM authentication failed; check ANTHROPIC_API_KEY",
                        status_code=response.status_code,
                        kind=classify_status(response.status_code),
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    kind = classify_status(response.status_code)
                    last_error = _mark_error(
                        LLMUnavailable(
                            f"LLM provider returned HTTP {response.status_code}",
                            status_code=response.status_code,
                            kind=kind,
                        ),
                        attempts,
                    )
                    self._breaker.record_failure()
                    retry_after = retry_after_seconds(
                        response.headers, maximum=self._retry_after_max
                    )
                else:
                    raise LLMInvalidResponse(
                        f"LLM provider returned HTTP {response.status_code}",
                        status_code=response.status_code,
                        kind=FAULT_PROTOCOL,
                    )
            except TransportCancelled as exc:
                raise LLMCancelled("LLM request was cancelled") from exc
            except httpx.TimeoutException as exc:
                last_error = _mark_error(
                    LLMTimeout("LLM request timed out", kind=FAULT_TIMEOUT),
                    attempts,
                )
                self._breaker.record_failure()
            except httpx.HTTPError as exc:
                last_error = _mark_error(
                    LLMUnavailable(
                        "LLM provider is unreachable", kind=FAULT_CONNECTION
                    ),
                    attempts,
                )
                self._breaker.record_failure()
            if attempts > self._max_retries:
                raise _mark_error(
                    last_error,
                    attempts,
                    task_deadline=_run_deadline_reached(deadline),
                )
            delay = _budgeted_retry_delay(
                attempts - 1,
                deadline,
                base_seconds=self._backoff_base,
                max_seconds=self._backoff_max,
                retry_after=retry_after,
            )
            if delay is None:
                raise _mark_error(
                    last_error,
                    attempts,
                    budget_limited=True,
                    task_deadline=_run_deadline_leaves_no_room(deadline),
                )
            time.sleep(delay)

    def complete_with_tools(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        timeout_seconds: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMToolResponse:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens or self._max_tokens,
            "temperature": 0,
            "system": system,
            "messages": _to_anthropic_messages(messages),
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = {"type": "auto"}
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        started = time.monotonic()
        deadline = _deadline_for(timeout_seconds)
        attempts = 0
        last_error: Exception | None = None
        retry_after: float | None = None
        while True:
            if is_cancelled():
                raise LLMCancelled("LLM request was cancelled")
            self._circuit_guard()
            request_timeout = _budgeted_attempt_timeout(deadline, self._timeout)
            if request_timeout is None:
                raise _mark_error(
                    LLMTimeout(
                        "insufficient remaining budget for a model request",
                        kind=FAULT_TIMEOUT,
                    ),
                    attempts,
                    budget_limited=True,
                    task_deadline=_run_deadline_leaves_no_room(deadline),
                )
            attempts += 1
            try:
                response = self._post(
                    self._base_url, headers, payload, request_timeout
                )
                if response.status_code == 200:
                    self._breaker.record_success()
                    body = response.json()
                    blocks = body.get("content") or []
                    text_parts: list[str] = []
                    calls: list[LLMToolCall] = []
                    for block in blocks:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            text_parts.append(str(block.get("text") or ""))
                        elif block.get("type") == "tool_use":
                            arguments = block.get("input")
                            if not isinstance(arguments, dict):
                                raise LLMInvalidResponse("tool_use input must be a JSON object")
                            call_id = str(block.get("id") or "")
                            name = str(block.get("name") or "")
                            if not call_id or not name:
                                raise LLMInvalidResponse("tool_use is missing id or name")
                            calls.append(LLMToolCall(call_id, name, arguments))
                    text = "".join(text_parts).strip() or None
                    if text is None and not calls:
                        raise LLMInvalidResponse("model response contained no text or tool call")
                    usage = body.get("usage") or {}
                    stop_reason = body.get("stop_reason")
                    return LLMToolResponse(
                        text=text,
                        tool_calls=calls,
                        stop_reason=stop_reason,
                        model=str(body.get("model") or self.model),
                        input_tokens=usage.get("input_tokens"),
                        output_tokens=usage.get("output_tokens"),
                        latency_ms=round((time.monotonic() - started) * 1000, 3),
                        truncated=is_truncated(stop_reason),
                        attempts=attempts,
                    )
                if response.status_code in {401, 403}:
                    raise LLMUnavailable(
                        "LLM authentication failed; check ANTHROPIC_API_KEY",
                        status_code=response.status_code,
                        kind=classify_status(response.status_code),
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    kind = classify_status(response.status_code)
                    last_error = _mark_error(
                        LLMUnavailable(
                            f"LLM provider returned HTTP {response.status_code}",
                            status_code=response.status_code,
                            kind=kind,
                        ),
                        attempts,
                    )
                    self._breaker.record_failure()
                    retry_after = retry_after_seconds(
                        response.headers, maximum=self._retry_after_max
                    )
                else:
                    raise LLMInvalidResponse(
                        f"LLM provider returned HTTP {response.status_code}",
                        status_code=response.status_code,
                        kind=FAULT_PROTOCOL,
                    )
            except TransportCancelled as exc:
                raise LLMCancelled("LLM request was cancelled") from exc
            except httpx.TimeoutException as exc:
                last_error = _mark_error(
                    LLMTimeout("LLM request timed out", kind=FAULT_TIMEOUT),
                    attempts,
                )
                self._breaker.record_failure()
            except httpx.HTTPError as exc:
                last_error = _mark_error(
                    LLMUnavailable(
                        "LLM provider is unreachable", kind=FAULT_CONNECTION
                    ),
                    attempts,
                )
                self._breaker.record_failure()
            if attempts > self._max_retries:
                raise _mark_error(
                    last_error,
                    attempts,
                    task_deadline=_run_deadline_reached(deadline),
                )
            delay = _budgeted_retry_delay(
                attempts - 1,
                deadline,
                base_seconds=self._backoff_base,
                max_seconds=self._backoff_max,
                retry_after=retry_after,
            )
            if delay is None:
                raise _mark_error(
                    last_error,
                    attempts,
                    budget_limited=True,
                    task_deadline=_run_deadline_leaves_no_room(deadline),
                )
            time.sleep(delay)


def build_llm_client(settings: Settings) -> LLMClient | None:
    if not settings.llm_enabled:
        return None
    provider = settings.llm_provider.strip().lower()
    if provider == "anthropic":
        return AnthropicClient(settings)
    if provider == "glm":
        from .glm import GLMClient

        return GLMClient(settings)
    raise LLMUnavailable(
        f"LLM provider {settings.llm_provider!r} is not supported; "
        "use 'anthropic' or 'glm'"
    )

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from ..config import Settings
from .client import (
    LLMCallResult,
    LLMCancelled,
    LLMCircuitOpen,
    LLMInvalidResponse,
    LLMTimeout,
    LLMToolCall,
    LLMToolResponse,
    LLMTruncated,
    LLMUnavailable,
    _budgeted_attempt_timeout,
    _budgeted_retry_delay,
    _deadline_for,
    _extract_json,
    _mark_error,
    _run_deadline_leaves_no_room,
    _run_deadline_reached,
    is_truncated,
)
from .resilience import (
    FAULT_CONNECTION,
    FAULT_PROTOCOL,
    FAULT_TIMEOUT,
    CircuitBreaker,
    CircuitOpenError,
    active_cancel_event,
    classify_status,
    is_cancelled,
    retry_after_seconds,
)
from .transport import TransportCancelled, post_json


def _glm_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools:
        schema = tool.get("input_schema") or tool.get("parameters") or {}
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": schema,
                },
            }
        )
    return converted


def _glm_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    if system:
        converted.append({"role": "system", "content": system})
    for message in messages:
        role = message.get("role")
        if role == "tool":
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": str(message.get("tool_call_id") or ""),
                    "content": str(message.get("content") or ""),
                }
            )
            continue
        if role == "assistant":
            calls: list[dict[str, Any]] = []
            for call in message.get("tool_calls") or []:
                arguments = call.get("arguments")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments or {}, ensure_ascii=False)
                calls.append(
                    {
                        "id": call.get("id") or call.get("call_id"),
                        "type": "function",
                        "function": {
                            "name": call.get("name"),
                            "arguments": arguments,
                        },
                    }
                )
            converted.append(
                {
                    "role": "assistant",
                    "content": str(message.get("content") or ""),
                    "tool_calls": calls,
                }
            )
            continue
        converted.append({"role": role or "user", "content": str(message.get("content") or "")})
    return converted


def _usage(body: dict[str, Any]) -> tuple[int | None, int | None]:
    usage = body.get("usage") or {}
    return usage.get("prompt_tokens"), usage.get("completion_tokens")


def _provider_error(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("code")
        return str(message)[:300] if message else None
    if isinstance(error, str):
        return error[:300]
    return None


class GLMClient:
    """GLM-5.3-Flash OpenAI-compatible Chat Completions client.

    The request/response shape was measured against the real provider:
    tool calls arrive as ``choices[0].message.tool_calls[*]`` with a JSON
    string in ``function.arguments`` and a ``call_...`` id. The adapter keeps
    that protocol detail out of the Agent Runtime. It never logs the key,
    prompt, response body or tool arguments.
    """

    provider = "glm"

    def __init__(self, settings: Settings):
        if not settings.llm_api_key:
            raise LLMUnavailable("GLM_API_KEY is not configured")
        self.model = settings.llm_model
        self._api_key = settings.llm_api_key
        self._base_url = settings.llm_base_url.rstrip("/")
        self._timeout = settings.llm_timeout_seconds
        self._connect_timeout = settings.llm_connect_timeout_seconds
        self._read_timeout = settings.llm_read_timeout_seconds
        self._max_retries = settings.llm_max_retries
        self._max_tokens = settings.llm_max_tokens
        self._temperature = settings.llm_temperature
        self._seed = settings.llm_seed
        self._backoff_base = settings.llm_retry_backoff_base_seconds
        self._backoff_max = settings.llm_retry_backoff_max_seconds
        self._retry_after_max = settings.llm_retry_after_max_seconds
        self._breaker = CircuitBreaker(
            failure_threshold=settings.llm_circuit_failure_threshold,
            reset_seconds=settings.llm_circuit_reset_seconds,
        )

    def __repr__(self) -> str:
        return f"GLMClient(model={self.model!r}, key=<redacted>)"

    def close(self) -> None:
        pass

    @property
    def endpoint(self) -> str:
        return f"{self._base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _post(
        self,
        payload: dict[str, Any],
        timeout: float,
        *,
        read_timeout: float | None = None,
        connect_timeout: float | None = None,
    ) -> httpx.Response:
        """Bound connect, read and the whole HTTP exchange.

        ``httpx`` resets its read timeout whenever a byte arrives, so the total
        wall-clock promise is enforced by the transport. Cancelling the owning
        run closes the socket instead of leaving a reader behind.
        """

        return post_json(
            self.endpoint,
            headers=self._headers(),
            payload=payload,
            connect_timeout=min(
                connect_timeout if connect_timeout is not None else self._connect_timeout,
                timeout,
            ),
            read_timeout=min(
                read_timeout if read_timeout is not None else self._read_timeout,
                timeout,
            ),
            total_timeout=timeout,
            cancel_event=active_cancel_event(),
        )

    def _circuit_guard(self) -> None:
        try:
            self._breaker.before_call()
        except CircuitOpenError as exc:
            raise LLMCircuitOpen(str(exc)) from exc

    def _request(
        self,
        payload: dict[str, Any],
        *,
        timeout_seconds: float | None,
    ) -> tuple[dict[str, Any], float, int]:
        started = time.monotonic()
        deadline = _deadline_for(timeout_seconds)
        attempts = 0
        last_error: Exception | None = None
        retry_after: float | None = None
        while True:
            if is_cancelled():
                raise LLMCancelled("GLM request was cancelled")
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
                response = self._post(payload, request_timeout)
                if response.status_code == 200:
                    self._breaker.record_success()
                    body = response.json()
                    if not isinstance(body, dict):
                        raise LLMInvalidResponse("GLM response must be a JSON object")
                    return (
                        body,
                        round((time.monotonic() - started) * 1000, 3),
                        attempts,
                    )
                if response.status_code in {401, 403}:
                    raise LLMUnavailable(
                        "LLM authentication failed; check GLM_API_KEY",
                        status_code=response.status_code,
                        kind=classify_status(response.status_code),
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    kind = classify_status(response.status_code)
                    last_error = _mark_error(
                        LLMUnavailable(
                            f"GLM provider returned HTTP {response.status_code}",
                            status_code=response.status_code,
                            kind=kind,
                        ),
                        attempts,
                    )
                    self._breaker.record_failure()
                    retry_after = retry_after_seconds(response.headers, maximum=self._retry_after_max)
                else:
                    detail = _provider_error(_safe_json(response))
                    message = f"GLM provider returned HTTP {response.status_code}"
                    if detail:
                        message += f": {detail}"
                    raise LLMInvalidResponse(
                        message,
                        status_code=response.status_code,
                        kind=FAULT_PROTOCOL,
                    )
            except TransportCancelled as exc:
                raise LLMCancelled("GLM request was cancelled") from exc
            except httpx.TimeoutException:
                last_error = _mark_error(
                    LLMTimeout("GLM request timed out", kind=FAULT_TIMEOUT),
                    attempts,
                )
                self._breaker.record_failure()
            except httpx.HTTPError:
                last_error = _mark_error(
                    LLMUnavailable("GLM provider is unreachable", kind=FAULT_CONNECTION),
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
                    task_deadline=_run_deadline_reached(deadline),
                )
            time.sleep(delay)

    def complete_json(self, *, system: str, prompt: str, max_tokens: int | None = None) -> LLMCallResult:
        payload = {
            "model": self.model,
            "messages": _glm_messages(system, [{"role": "user", "content": prompt}]),
            "temperature": self._temperature,
            "max_tokens": max_tokens or self._max_tokens,
        }
        if self._seed is not None:
            payload["seed"] = self._seed
        body, latency, _attempts = self._request(payload, timeout_seconds=None)
        content = _message_content(body)
        choice = _first_choice(body)
        stop_reason = choice.get("finish_reason")
        truncated = is_truncated(stop_reason)
        try:
            data = _extract_json(content)
        except LLMInvalidResponse as exc:
            if truncated:
                raise LLMTruncated(
                    "GLM response reached the output token cap before the JSON object was complete"
                ) from exc
            raise
        input_tokens, output_tokens = _usage(body)
        return LLMCallResult(
            data=data,
            model=str(body.get("model") or self.model),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency,
            finish_reason=stop_reason,
            truncated=truncated,
        )

    def complete_with_tools(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        timeout_seconds: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMToolResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _glm_messages(system, messages),
            "temperature": self._temperature,
            "max_tokens": max_tokens or self._max_tokens,
        }
        if self._seed is not None:
            payload["seed"] = self._seed
        if tools:
            payload["tools"] = _glm_tools(tools)
            payload["tool_choice"] = "auto"
        body, latency, attempts = self._request(payload, timeout_seconds=timeout_seconds)
        choice = _first_choice(body)
        message = choice.get("message") or {}
        text = message.get("content")
        if text is not None and not isinstance(text, str):
            text = str(text)
        text = (text or "").strip() or None
        calls: list[LLMToolCall] = []
        for item in message.get("tool_calls") or []:
            if not isinstance(item, dict):
                raise LLMInvalidResponse("GLM tool_call must be an object")
            function = item.get("function") or {}
            name = function.get("name")
            call_id = item.get("id")
            if not name or not call_id:
                raise LLMInvalidResponse("GLM tool_call is missing id or function.name")
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise LLMInvalidResponse("GLM tool_call arguments were not valid JSON") from exc
            if not isinstance(arguments, dict):
                raise LLMInvalidResponse("GLM tool_call arguments must decode to a JSON object")
            calls.append(LLMToolCall(str(call_id), str(name), arguments))
        if text is None and not calls:
            raise LLMInvalidResponse("GLM response contained no text or tool call")
        input_tokens, output_tokens = _usage(body)
        stop_reason = choice.get("finish_reason")
        return LLMToolResponse(
            text=text,
            tool_calls=calls,
            stop_reason=stop_reason,
            model=str(body.get("model") or self.model),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency,
            truncated=is_truncated(stop_reason),
            attempts=attempts,
        )


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _first_choice(body: dict[str, Any]) -> dict[str, Any]:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise LLMInvalidResponse("GLM response contained no choices")
    return choices[0]


def _message_content(body: dict[str, Any]) -> str:
    message = _first_choice(body).get("message") or {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise LLMInvalidResponse("GLM response contained no text")
    return content

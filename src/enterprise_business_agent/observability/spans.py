from __future__ import annotations

import json
from typing import Any, Mapping

SERVICE_NAME = "enterprise-business-agent"


def _load_trace_module() -> Any:
    """Return ``opentelemetry.trace`` or ``None`` when the API is absent.

    Only the OpenTelemetry API is used. Installing an SDK and exporter is an
    operational choice: without one the global tracer is a no-op and spans go
    nowhere, which keeps tracing optional.
    """

    try:
        from opentelemetry import trace
    except ImportError:
        return None
    return trace


def _coerce(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _attributes(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _coerce(value) for key, value in values.items() if value is not None}


class SpanSink:
    """Turn the runtime's existing trace events into OpenTelemetry spans.

    One instance belongs to one agent run, so parent/child relations do not
    depend on thread-local context. Every operation is guarded: a broken tracer
    or exporter must never affect the analysis itself.
    """

    def __init__(
        self,
        *,
        run_id: str,
        provider: str | None = None,
        prompt_version: str | None = None,
        service_name: str = SERVICE_NAME,
        tracer: Any = None,
        trace_module: Any = None,
        parent_context: Any = None,
    ):
        self._trace_module: Any = trace_module if trace_module is not None else _load_trace_module()
        if tracer is None:
            from .setup import current_tracer

            tracer = current_tracer()
        if tracer is None and self._trace_module is not None:
            try:
                tracer = self._trace_module.get_tracer(service_name)
            except Exception:
                tracer = None
        self._tracer = tracer
        self._run_id = run_id
        self._decision: Any = None
        self._tools: dict[str, Any] = {}
        self._tokens: dict[str, Any] = {}
        self._root: Any = None
        self._root_token: Any = None
        if self._tracer is None:
            return
        try:
            self._root = self._start(
                "agent.run",
                None,
                {"agent.run_id": run_id, "agent.provider": provider, "agent.prompt_version": prompt_version},
                context=parent_context,
            )
            # Keep the run span current so SQL, evidence and report spans
            # emitted without an explicit parent join the same trace.
            self._root_token = self._attach(self._root)
            from .setup import set_run_context

            set_run_context(run_id)
        except Exception:
            self._disable()

    def __call__(self, event: Mapping[str, Any]) -> None:
        if self._tracer is None:
            return
        try:
            self._handle(event)
        except Exception:
            self._disable()

    def _handle(self, event: Mapping[str, Any]) -> None:
        event_type = str(event.get("event_type") or "agent_event")
        if event_type == "decision_started":
            self._end(self._decision)
            self._decision = self._start(
                "agent.decision",
                self._root,
                {
                    "agent.round": event.get("round"),
                    "agent.budget_left": event.get("budget_left"),
                    "agent.tokens_used": event.get("tokens_used"),
                },
            )
        elif event_type == "model_response":
            calls = event.get("tool_calls") or []
            span = self._start(
                "llm.complete",
                self._decision,
                {
                    "llm.model": event.get("model"),
                    "llm.latency_ms": event.get("latency_ms"),
                    "llm.input_tokens": event.get("input_tokens"),
                    "llm.output_tokens": event.get("output_tokens"),
                    "llm.tool_call_count": len(calls),
                },
            )
            if span is not None and not calls and not event.get("has_text"):
                self._mark_error(span, "model_response_without_text_or_tool_call")
            self._end(span)
        elif event_type == "tool_call_started":
            call_id = str(event.get("call_id") or "")
            self._close_tool(call_id)
            span = self._start(
                "tool.call",
                self._decision,
                {
                    "tool.name": event.get("tool"),
                    "tool.call_id": call_id,
                    "tool.arguments": event.get("arguments"),
                },
            )
            self._tools[call_id] = span
            token = self._attach(span)
            if token is not None:
                self._tokens[call_id] = token
            from .setup import set_run_context

            set_run_context(self._run_id, call_id)
        elif event_type == "tool_call_finished":
            call_id = str(event.get("call_id") or "")
            span = self._tools.pop(call_id, None)
            self._detach(call_id)
            from .setup import set_run_context

            set_run_context(self._run_id)
            if span is None:
                span = self._start(
                    "tool.call",
                    self._decision,
                    {"tool.name": event.get("tool"), "tool.call_id": call_id},
                )
            status = str(event.get("status") or "")
            if span is not None:
                try:
                    span.set_attribute("tool.status", status)
                    duration = _coerce(event.get("duration_ms"))
                    if duration is not None:
                        span.set_attribute("tool.duration_ms", duration)
                    span.set_attribute("tool.evidence_count", len(event.get("evidence_ids") or []))
                    if event.get("error_code") is not None:
                        span.set_attribute("tool.error_code", str(event.get("error_code")))
                finally:
                    if status and status != "ok":
                        self._mark_error(span, status)
                    self._end(span)
        elif event_type == "checkpoint_restored":
            span = self._start(
                "checkpoint.restore",
                self._root,
                {
                    "checkpoint.thread_id": event.get("thread_id"),
                    "checkpoint.found": event.get("found"),
                    "checkpoint.id": event.get("checkpoint_id"),
                    "checkpoint.pending_writes": event.get("pending_writes"),
                    "checkpoint.restore_ms": event.get("restore_ms"),
                },
            )
            if span is not None and not event.get("found"):
                self._mark_error(span, "checkpoint_not_found")
            self._end(span)
        elif event_type in {"llm_error", "llm_retry"}:
            self._add_event(
                self._decision,
                event_type,
                {
                    "llm.code": event.get("code"),
                    "llm.reason": event.get("reason"),
                    "llm.retried": event.get("retried"),
                },
            )
        elif event_type == "run_finished":
            for call_id in list(self._tools):
                self._close_tool(call_id)
            self._end(self._decision)
            self._decision = None
            if self._root is not None:
                try:
                    status = str(event.get("status") or "")
                    self._root.set_attribute("agent.status", status)
                    for key, value in {
                        "agent.stop_reason": event.get("stop_reason"),
                        "agent.tool_calls_used": event.get("tool_calls_used"),
                        "llm.input_tokens": event.get("input_tokens"),
                        "llm.output_tokens": event.get("output_tokens"),
                        "llm.latency_ms": event.get("latency_ms"),
                        "llm.provider": event.get("provider"),
                        "llm.model": event.get("model"),
                        "agent.prompt_version": event.get("prompt_version"),
                    }.items():
                        if value is not None:
                            self._root.set_attribute(key, _coerce(value))
                    if status and status not in {
                        "completed",
                        "completed_with_warnings",
                    }:
                        self._mark_error(self._root, status)
                finally:
                    self._end(self._root)
                    self._root = None
                    self._detach_root()
                    from .setup import clear_run_context

                    clear_run_context()

    def checkpoint_restored(
        self,
        *,
        thread_id: str,
        found: bool,
        checkpoint_id: str | None = None,
        pending_writes: int | None = None,
        restore_ms: float | None = None,
    ) -> None:
        """Record a checkpoint lookup on the run trace.

        A resumed run is a new process with a new root span; the checkpoint span
        ties that run to the durable state it continued from.
        """

        self(
            {
                "event_type": "checkpoint_restored",
                "thread_id": thread_id,
                "found": found,
                "checkpoint_id": checkpoint_id,
                "pending_writes": pending_writes,
                "restore_ms": restore_ms,
            }
        )

    def _attach(self, span: Any) -> Any:
        """Make ``span`` current so nested spans share its trace id."""

        if span is None or self._trace_module is None:
            return None
        try:
            from opentelemetry import context as otel_context

            return otel_context.attach(self._trace_module.set_span_in_context(span))
        except Exception:
            return None

    def _detach(self, call_id: str) -> None:
        token = self._tokens.pop(call_id, None)
        if token is None:
            return
        try:
            from opentelemetry import context as otel_context

            otel_context.detach(token)
        except Exception:
            return

    def _close_tool(self, call_id: str) -> None:
        self._detach(call_id)
        self._end(self._tools.pop(call_id, None))

    def _detach_root(self) -> None:
        token = self._root_token
        self._root_token = None
        if token is None:
            return
        try:
            from opentelemetry import context as otel_context

            otel_context.detach(token)
        except Exception:
            return

    def _start(
        self,
        name: str,
        parent: Any,
        attributes: Mapping[str, Any],
        *,
        context: Any = None,
    ) -> Any:
        if self._tracer is None:
            return None
        if parent is not None and self._trace_module is not None:
            context = self._trace_module.set_span_in_context(parent)
        if context is not None:
            return self._tracer.start_span(name, context=context, attributes=_attributes(attributes))
        return self._tracer.start_span(name, attributes=_attributes(attributes))

    @staticmethod
    def _end(span: Any) -> None:
        if span is None:
            return
        try:
            span.end()
        except Exception:
            return

    @staticmethod
    def _add_event(span: Any, name: str, attributes: Mapping[str, Any]) -> None:
        if span is None:
            return
        span.add_event(name, attributes=_attributes(attributes))

    @staticmethod
    def _mark_error(span: Any, description: str) -> None:
        try:
            from opentelemetry.trace import Status, StatusCode
        except ImportError:
            return
        try:
            span.set_status(Status(StatusCode.ERROR, description))
        except Exception:
            return

    def _disable(self) -> None:
        for call_id in list(self._tools):
            self._close_tool(call_id)
        self._tokens.clear()
        for span in [self._decision, self._root]:
            self._end(span)
        self._decision = None
        self._root = None
        self._detach_root()
        self._tracer = None
        from .setup import clear_run_context

        clear_run_context()

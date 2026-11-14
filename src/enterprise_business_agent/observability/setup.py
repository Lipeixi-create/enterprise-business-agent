"""Optional OpenTelemetry SDK and OTLP exporter setup.

Tracing is an operational capability, never a correctness dependency. When the
SDK or exporter is missing, when no endpoint is configured, or when the SDK
rejects initialisation, the application keeps running and the status object says
exactly why no span was exported. Only spans handed to a working exporter by a
real ``SpanExporter.export`` call are counted as exported.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

_SERVICE_DEFAULT = "enterprise-business-agent"

_run_id_var: ContextVar[str | None] = ContextVar("eba_otel_run_id", default=None)
_tool_call_id_var: ContextVar[str | None] = ContextVar("eba_otel_tool_call_id", default=None)


@dataclass
class TracingStatus:
    enabled: bool = False
    sdk_available: bool = False
    exporter_configured: bool = False
    exporter_kind: str | None = None
    exporter_target: str | None = None
    initialized: bool = False
    init_error: str | None = None
    exported_spans: int = 0
    dropped_spans: int = 0
    sample_ratio: float = 1.0
    propagation: str = "w3c-tracecontext"
    note: str = "tracing was never configured in this process"

    def as_dict(self) -> dict[str, Any]:
        return {
            "otel_enabled": self.enabled,
            "otel_sdk_available": self.sdk_available,
            "exporter_configured": self.exporter_configured,
            "exporter_kind": self.exporter_kind,
            "exporter_target": self.exporter_target,
            "initialized": self.initialized,
            "init_error": self.init_error,
            "exported_spans": self.exported_spans,
            "dropped_spans": self.dropped_spans,
            "exported_spans_scope": "process_local",
            "sample_ratio": self.sample_ratio,
            "propagation": self.propagation,
            "note": self.note,
        }


_lock = threading.RLock()
_status = TracingStatus()
_provider: Any = None
_tracer: Any = None


def tracing_status() -> TracingStatus:
    with _lock:
        return _status


def current_tracer() -> Any:
    """Return the tracer owned by this module, or ``None`` when uninitialised.

    The tracer is kept local rather than read from the global provider so a
    process (or a test) can reinitialise tracing without fighting OpenTelemetry's
    set-once rule for the global ``TracerProvider``.
    """

    with _lock:
        return _tracer


def set_run_context(run_id: str | None, tool_call_id: str | None = None) -> None:
    _run_id_var.set(run_id)
    _tool_call_id_var.set(tool_call_id)


def clear_run_context() -> None:
    _run_id_var.set(None)
    _tool_call_id_var.set(None)


def current_otel_context() -> Any:
    """Return the active OpenTelemetry context, or ``None`` without the API.

    Capturing it lets a caller hand the trace parent to another thread. The
    async run worker does not inherit context variables automatically, so the
    run would otherwise start a disconnected trace.
    """

    try:
        from opentelemetry import context as otel_context

        return otel_context.get_current()
    except Exception:  # noqa: BLE001 - tracing is optional
        return None


def extract_trace_context(carrier: Mapping[str, str] | None) -> Any:
    """Extract a W3C ``traceparent`` (and baggage) from request headers."""

    if not carrier:
        return None
    try:
        from opentelemetry.propagate import extract

        return extract(dict(carrier))
    except Exception:  # noqa: BLE001 - malformed headers must not fail a request
        return None


def inject_trace_context(carrier: dict[str, str] | None = None) -> dict[str, str]:
    """Inject the active trace context into an outbound/response carrier."""

    target: dict[str, str] = carrier if carrier is not None else {}
    try:
        from opentelemetry.propagate import inject

        inject(target)
    except Exception:  # noqa: BLE001 - propagation is best effort
        pass
    return target


def current_trace_ids() -> tuple[str | None, str | None]:
    """Return ``(trace_id, span_id)`` for logs, or ``(None, None)``."""

    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        if not context.is_valid:
            return None, None
        return format(context.trace_id, "032x"), format(context.span_id, "016x")
    except Exception:  # noqa: BLE001 - tracing is optional
        return None, None


class _JsonlFileSpanExporter:
    """Append finished spans as JSON lines for local inspection.

    This is a real ``SpanExporter``: only spans handed to ``export`` are
    written. The directory is created on first export, and a write failure is
    reported as ``FAILURE`` instead of raising into the application.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._write_lock = threading.Lock()

    def export(self, spans):
        from opentelemetry.sdk.trace.export import SpanExportResult

        lines: list[str] = []
        for item in spans:
            try:
                payload = json.loads(item.to_json())
            except Exception:  # noqa: BLE001 - one bad span must not lose the rest
                continue
            payload["_exported_at"] = time.time()
            lines.append(json.dumps(payload, ensure_ascii=False))
        if not lines:
            return SpanExportResult.SUCCESS
        try:
            with self._write_lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write("\n".join(lines) + "\n")
        except OSError:
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


def _build_exporter(kind: str, endpoint: str | None, trace_file: str) -> tuple[Any, str]:
    """Construct the configured exporter, raising only setup errors."""

    if kind == "console":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        return ConsoleSpanExporter(), "stdout"
    if kind == "file":
        return _JsonlFileSpanExporter(trace_file), str(trace_file)
    if kind == "otlp":
        if not endpoint:
            raise ValueError("no OTLP endpoint configured")
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ValueError("opentelemetry-exporter-otlp-proto-http is not installed") from exc
        target = endpoint.rstrip("/")
        if not target.endswith("/v1/traces"):
            target = f"{target}/v1/traces"
        return OTLPSpanExporter(endpoint=target), target
    raise ValueError(f"unknown exporter kind {kind!r}")


def force_flush_tracing(timeout_millis: int = 5_000) -> bool:
    """Flush the process tracer; used by scripts that exit after one run."""

    with _lock:
        provider = _provider
    if provider is None:
        return False
    try:
        return bool(provider.force_flush(timeout_millis))
    except Exception:  # noqa: BLE001 - flushing is best effort
        return False


def _counting_exporter(exporter: Any) -> Any:
    """Wrap an exporter so only successful exports are counted."""

    try:
        from opentelemetry.sdk.trace.export import SpanExportResult
    except ImportError:  # pragma: no cover - SDK guard
        return exporter

    class _CountingExporter:
        def __init__(self, inner: Any):
            self._inner = inner

        def export(self, spans):
            result = self._inner.export(spans)
            with _lock:
                if result == SpanExportResult.SUCCESS:
                    _status.exported_spans += len(spans)
                else:
                    _status.dropped_spans += len(spans)
            return result

        def shutdown(self):
            return self._inner.shutdown()

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            flush = getattr(self._inner, "force_flush", None)
            if flush is None:
                return True
            return bool(flush(timeout_millis))

    return _CountingExporter(exporter)


def configure_tracing(settings: Any, *, exporter: Any = None) -> TracingStatus:
    """Initialise the SDK at most once; never raise to the caller.

    ``exporter`` lets a deployment or test supply its own ``SpanExporter``. When
    omitted, ``settings.otel_exporter`` chooses OTLP HTTP, console output, a
    local JSONL file, or no exporter at all.
    """

    global _status, _provider, _tracer

    with _lock:
        if _status.initialized or _status.init_error is not None:
            return _status
        enabled = bool(getattr(settings, "otel_enabled", False))
        service_name = str(getattr(settings, "otel_service_name", _SERVICE_DEFAULT) or _SERVICE_DEFAULT)
        kind = str(getattr(settings, "otel_exporter", "otlp") or "otlp").strip().lower()
        endpoint = (
            getattr(settings, "otel_exporter_endpoint", None)
            or _env("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
            or _env("OTEL_EXPORTER_OTLP_ENDPOINT")
        )
        trace_file = str(
            getattr(settings, "otel_trace_file", "data/traces/eba-spans.jsonl") or "data/traces/eba-spans.jsonl"
        )
        sample_ratio = float(getattr(settings, "otel_sample_ratio", 1.0))
        if not enabled:
            _status = TracingStatus(
                enabled=False,
                note="EBA_OTEL_ENABLED is false; no span is created or exported.",
            )
            return _status

        try:
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError:
            _status = TracingStatus(
                enabled=True,
                sdk_available=False,
                note=(
                    "opentelemetry-sdk is not installed; spans are not created. "
                    "Install the SDK and an OTLP exporter to export traces."
                ),
            )
            return _status

        _status = TracingStatus(
            enabled=True,
            sdk_available=True,
            sample_ratio=sample_ratio,
            exporter_kind="injected" if exporter is not None else kind,
            exporter_target=(
                "supplied exporter"
                if exporter is not None
                else (
                    endpoint
                    if kind == "otlp"
                    else trace_file
                    if kind == "file"
                    else "stdout"
                    if kind == "console"
                    else None
                )
            ),
            note="",
        )
        if exporter is None:
            if kind == "none":
                _status.exporter_kind = "none"
                _status.init_error = "exporter kind is none"
                _status.note = "Tracing is enabled but no exporter was requested."
                return _status
            try:
                exporter, target = _build_exporter(kind, endpoint, trace_file)
            except Exception as exc:  # noqa: BLE001 - tracing must never break the app
                _status.init_error = str(exc)
                _status.note = "exporter construction failed; the application continues without tracing."
                return _status
            _status.exporter_configured = True
            _status.exporter_target = target
        else:
            _status.exporter_configured = True

        try:
            from opentelemetry.sdk.trace.sampling import (
                ALWAYS_ON,
                ParentBased,
                TraceIdRatioBased,
            )

            sampler = ParentBased(TraceIdRatioBased(sample_ratio)) if sample_ratio < 1.0 else ALWAYS_ON
            resource = Resource.create(
                {
                    "service.name": service_name,
                    "service.version": getattr(settings, "version", "unknown")
                    if isinstance(getattr(settings, "version", None), str)
                    else "unknown",
                    "deployment.environment": _env("EBA_ENV") or "local",
                }
            )
            provider = TracerProvider(resource=resource, sampler=sampler)
            provider.add_span_processor(
                BatchSpanProcessor(
                    _counting_exporter(exporter),
                    schedule_delay_millis=500,
                )
            )
            _provider = provider
            _tracer = provider.get_tracer(service_name)
            _status.initialized = True
            _status.note = (
                f"Tracing initialised with the {_status.exporter_kind} exporter; "
                "exported span counts are per process and only increase after a "
                "successful export call."
            )
        except Exception as exc:  # noqa: BLE001 - tracing must never break the app
            _status.init_error = f"{type(exc).__name__}: {exc}"
            _status.note = "exporter initialisation failed; the application continues without tracing."
        return _status


def reset_tracing() -> None:
    """Discard the process-wide tracing state.

    Used by tests and by an operator who reconfigures tracing in-process. It is
    never needed for correctness because tracing has no effect on business paths.
    """

    global _status, _provider, _tracer
    shutdown_tracing()
    with _lock:
        _status = TracingStatus()
        _provider = None
        _tracer = None


def shutdown_tracing() -> None:
    global _provider, _tracer
    with _lock:
        provider = _provider
        _provider = None
        _tracer = None
    if provider is None:
        return
    # Shutdown joins the exporter's worker thread, which acquires _lock while
    # counting an export, so the lock must not be held here.
    try:
        provider.shutdown()
    except Exception:  # noqa: BLE001 - shutdown is best effort
        pass


@contextmanager
def span(
    name: str,
    *,
    context: Any = None,
    kind: Any = None,
    **attributes: Any,
) -> Iterator[Any]:
    """Create a span when tracing is initialised; otherwise a no-op.

    The span is made current for the duration of the block, so nested spans
    such as a SQL query inside a tool call share one trace without callers
    having to pass parent objects around. ``context`` lets a worker thread
    continue a trace started on the request thread.
    """

    tracer = _tracer
    if tracer is None:
        yield None
        return
    payload = {key.replace("__", "."): value for key, value in attributes.items() if value is not None}
    run_id = _run_id_var.get()
    tool_call_id = _tool_call_id_var.get()
    if run_id:
        payload.setdefault("agent.run_id", run_id)
    if tool_call_id:
        payload.setdefault("tool.call_id", tool_call_id)
    try:
        options: dict[str, Any] = {"attributes": payload}
        if context is not None:
            options["context"] = context
        if kind is not None:
            options["kind"] = kind
        current = tracer.start_span(name, **options)
    except Exception:  # noqa: BLE001 - a broken tracer must not block work
        yield None
        return
    token = None
    try:
        from opentelemetry import context as otel_context
        from opentelemetry import trace

        token = otel_context.attach(trace.set_span_in_context(current))
    except Exception:  # noqa: BLE001 - nesting is best effort
        token = None
    try:
        yield current
    except Exception as exc:  # noqa: BLE001 - record, then re-raise unchanged
        try:
            from opentelemetry.trace import Status, StatusCode

            current.set_status(Status(StatusCode.ERROR, type(exc).__name__))
        except Exception:
            pass
        raise
    finally:
        if token is not None:
            try:
                from opentelemetry import context as otel_context

                otel_context.detach(token)
            except Exception:
                pass
        try:
            current.end()
        except Exception:
            pass


def _env(name: str) -> str | None:
    import os

    value = os.getenv(name)
    return value.strip() if value and value.strip() else None


def traced(name: str, **attribute_map: str):
    """Decorate a function with a span built from selected keyword arguments.

    ``attribute_map`` maps a span attribute to a keyword argument name; the
    decorator deliberately does not copy all arguments, so a caller cannot
    accidentally put a password or question text on a span.
    """

    import functools

    def decorator(function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            attributes = {
                attribute: kwargs.get(argument)
                for attribute, argument in attribute_map.items()
                if kwargs.get(argument) is not None
            }
            with span(name, **attributes):
                return function(*args, **kwargs)

        return wrapper

    return decorator


__all__ = [
    "TracingStatus",
    "clear_run_context",
    "configure_tracing",
    "current_otel_context",
    "current_trace_ids",
    "current_tracer",
    "extract_trace_context",
    "force_flush_tracing",
    "inject_trace_context",
    "reset_tracing",
    "set_run_context",
    "shutdown_tracing",
    "span",
    "traced",
    "tracing_status",
]

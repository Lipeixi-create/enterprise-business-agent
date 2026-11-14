from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from ..observability.setup import (
    current_trace_ids,
    extract_trace_context,
    inject_trace_context,
    span,
)
from ..security.audit import record_security_event

REQUEST_ID_HEADER = "X-Request-ID"
TRACE_ID_HEADER = "X-Trace-ID"

# Only credential-shaped keys are masked. Token *counts* ("input_tokens") are
# operational metrics and must stay readable.
_SENSITIVE_KEY = re.compile(
    r"(?i)(api[_-]?key|authorization|password|passwd|secret|credential|"
    r"connection[_-]?string|database[_-]?url|readonly[_-]?database[_-]?url|"
    r"access[_-]?token|refresh[_-]?token|bearer[_-]?token|private[_-]?key)"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|password|passwd|secret|access[_-]?token|"
    r"refresh[_-]?token|private[_-]?key)\b(\s*[=:]\s*)([^\s,;]+)"
)


def redact(value: Any, key: str | None = None) -> Any:
    """Mask credential-shaped values in log fields.

    The function never changes business numbers: it only reacts to key names
    and inline ``password=...`` style assignments. API keys and database
    passwords must not appear in logs even at DEBUG level.
    """

    if key is not None and _SENSITIVE_KEY.search(key):
        return "***"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}***", value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        trace_id, span_id = current_trace_ids()
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": trace_id,
            "span_id": span_id,
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(redact(extra))
        if record.exc_info:
            payload["error_type"] = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("enterprise_business_agent.api")
    logger.setLevel(level.upper())
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def log_event(logger: logging.Logger, message: str, **fields) -> None:
    logger.info(message, extra={"fields": fields})


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = incoming.strip() if incoming and len(incoming) <= 128 else str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()
        # Continue an upstream trace when the caller sends a W3C traceparent,
        # otherwise start a new one. The route path is recorded; the query
        # string is deliberately excluded because it can contain user input.
        trace_context = extract_trace_context(request.headers)
        with span(
            "http.request",
            context=trace_context,
            http__request__method=request.method,
            http__request__path=request.url.path,
            http__request__id=request_id,
        ) as current:
            trace_id, span_id = current_trace_ids()
            request.state.trace_id = trace_id
            request.state.span_id = span_id
            try:
                response = await call_next(request)
            except Exception as exc:
                if current is not None:
                    try:
                        from opentelemetry.trace import Status, StatusCode

                        current.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                    except Exception:
                        pass
                raise
            elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            if current is not None:
                try:
                    current.set_attribute("http.response.status_code", response.status_code)
                    current.set_attribute("http.duration_ms", elapsed_ms)
                    if response.status_code >= 500:
                        from opentelemetry.trace import Status, StatusCode

                        current.set_status(Status(StatusCode.ERROR, f"http_{response.status_code}"))
                except Exception:
                    pass
            response.headers[REQUEST_ID_HEADER] = request_id
            if trace_id:
                response.headers[TRACE_ID_HEADER] = trace_id
            carrier: dict[str, str] = {}
            inject_trace_context(carrier)
            if carrier.get("traceparent"):
                response.headers["traceparent"] = carrier["traceparent"]
            logger = getattr(request.app.state, "logger", None)
            if logger is not None:
                log_event(
                    logger,
                    "http_request",
                    request_id=request_id,
                    trace_id=trace_id,
                    method=request.method,
                    path=request.url.path,
                    status_code=response.status_code,
                    elapsed_ms=elapsed_ms,
                )
            if response.status_code in {401, 403}:
                principal = getattr(request.state, "principal", None)
                event_type = "auth_failed" if response.status_code == 401 else "authorization_denied"
                record_security_event(
                    getattr(request.app.state, "run_store", None),
                    event_type,
                    request_id=request_id,
                    principal_id=getattr(principal, "principal_id", None),
                    detail={
                        "method": request.method,
                        "path": request.url.path,
                        "status_code": response.status_code,
                        "role": getattr(principal, "role", None),
                    },
                    logger=logger,
                )
                if logger is not None:
                    log_event(
                        logger,
                        "security_denied",
                        request_id=request_id,
                        event_type=event_type,
                        method=request.method,
                        path=request.url.path,
                        status_code=response.status_code,
                        role=getattr(principal, "role", None),
                    )
            return response

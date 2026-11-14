"""Security audit events that are not tied to one agent run.

Run-scoped tool, model and approval events already live in
``agent_audit_events`` with a ``run_id``. Authentication failures,
authorization denials and MCP calls have no run, so they use the same table
with ``run_id=None`` and a bounded event type. The helper never lets an audit
write failure change the request outcome and never stores credentials.
"""

from __future__ import annotations

import logging
from typing import Any

SECURITY_EVENT_TYPES = frozenset(
    {
        "auth_failed",
        "authorization_denied",
        "mcp_auth_failed",
        "mcp_tool_call",
        "mcp_tool_denied",
        "mcp_protocol_error",
    }
)


def record_security_event(
    store: Any,
    event_type: str,
    *,
    request_id: str | None = None,
    principal_id: str | None = None,
    detail: dict[str, Any] | None = None,
    logger: logging.Logger | None = None,
) -> str | None:
    if store is None:
        return None
    if event_type not in SECURITY_EVENT_TYPES:
        raise ValueError(f"unknown security event type {event_type!r}")
    try:
        return store.append_event(
            event_type=event_type,
            run_id=None,
            request_id=request_id,
            principal_id=principal_id,
            detail=detail or {},
        )
    except Exception as exc:  # noqa: BLE001 - audit must not break the request
        if logger is not None:
            logger.warning(
                "security_audit_write_failed",
                extra={"fields": {"event_type": event_type, "error_type": type(exc).__name__}},
            )
        return None


__all__ = ["SECURITY_EVENT_TYPES", "record_security_event"]

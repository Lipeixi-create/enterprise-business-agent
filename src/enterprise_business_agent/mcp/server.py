"""Minimal MCP JSON-RPC server over an HTTP transport.

Supported protocol revisions are the MCP revisions this process implements.
Only ``tools`` capabilities are advertised: the server exposes the same eight
read-only tools as the LangGraph agent. A call is never routed directly to a
tool handler; it goes through ``ToolRegistry.execute`` so scope checks,
metric/region restrictions, argument validation, AnalysisGateway,
SemanticQueryGuard and SafeQueryExecutor all still apply.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from .. import __version__
from ..security import Principal
from ..security.audit import record_security_event

MCP_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
_LATEST_PROTOCOL = MCP_PROTOCOL_VERSIONS[0]
_NON_ERROR_STATUSES = frozenset({"ok", "no_data", "not_calculable", "insufficient_data"})
_DENIED_STATUSES = frozenset({"permission_denied", "unknown_tool"})


def _response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(
    request_id: Any,
    code: int,
    message: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "message": message}
    if data:
        payload["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": payload}


class McpToolServer:
    def __init__(
        self,
        *,
        registry: Any,
        settings: Any,
        run_store: Any = None,
        logger: logging.Logger | None = None,
    ):
        self.registry = registry
        self.settings = settings
        self.run_store = run_store
        self.logger = logger

    def handle(
        self,
        message: Any,
        principal: Principal,
        *,
        request_id: str | None = None,
    ) -> tuple[int, Any]:
        """Handle one JSON-RPC message or batch.

        Returns ``(http_status, body)``. Notifications have no body and use
        HTTP 202. The transport layer is responsible for authentication and
        for rejecting requests when MCP is disabled.
        """

        if isinstance(message, list):
            if not message:
                return 400, _error(None, -32600, "Invalid Request")
            responses = []
            for item in message:
                status, body = self._handle_one(item, principal, request_id=request_id)
                if status == 202 and body is None:
                    continue
                if body is not None:
                    responses.append(body)
            if not responses:
                return 202, None
            return 200, responses
        return self._handle_one(message, principal, request_id=request_id)

    def _handle_one(
        self,
        message: Any,
        principal: Principal,
        *,
        request_id: str | None,
    ) -> tuple[int, Any]:
        if not isinstance(message, dict):
            return 400, _error(None, -32600, "Invalid Request")
        if message.get("jsonrpc") != "2.0":
            return 400, _error(message.get("id"), -32600, "Invalid Request", {"reason": "jsonrpc must be 2.0"})
        method = message.get("method")
        if not isinstance(method, str) or not method:
            return 400, _error(message.get("id"), -32600, "Invalid Request")
        notification = "id" not in message
        request_id_value = message.get("id")

        if method.startswith("notifications/"):
            return 202, None

        if method == "initialize":
            params = message.get("params")
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            version = (
                requested if isinstance(requested, str) and requested in MCP_PROTOCOL_VERSIONS else _LATEST_PROTOCOL
            )
            result = {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": str(
                        getattr(
                            self.settings,
                            "mcp_server_name",
                            "enterprise-business-agent",
                        )
                    ),
                    "version": __version__,
                },
                "instructions": (
                    "Read-only business analysis tools. Every call is checked "
                    "against the API key's role, metric/domain/region policy "
                    "and the trusted semantic query gateway. These tools cannot "
                    "execute SQL or write data."
                ),
            }
            if isinstance(requested, str) and requested not in MCP_PROTOCOL_VERSIONS:
                result["_meta"] = {"requestedProtocolVersion": requested}
            return (202, None) if notification else (200, _response(request_id_value, result))

        if method == "ping":
            return (202, None) if notification else (200, _response(request_id_value, {}))

        if method == "tools/list":
            tools = self._list_tools(principal)
            result = {"tools": tools}
            return (202, None) if notification else (200, _response(request_id_value, result))

        if method == "tools/call":
            result = self._call_tool(
                message.get("params"),
                principal,
                request_id=request_id,
                request_id_value=request_id_value,
            )
            return (202, None) if notification else (200, result)

        self._audit_protocol_error(
            principal,
            request_id=request_id,
            method=method,
            code=-32601,
        )
        if notification:
            return 202, None
        return 200, _error(
            request_id_value,
            -32601,
            f"Method not found: {method}",
            {"supported": ["initialize", "ping", "tools/list", "tools/call"]},
        )

    def _list_tools(self, principal: Principal) -> list[dict[str, Any]]:
        if self.registry is None:
            return []
        tools = []
        for spec in self.registry.specs(principal):
            tools.append(
                {
                    "name": spec["name"],
                    "description": spec.get("description", ""),
                    "inputSchema": spec.get("input_schema") or {"type": "object"},
                    "annotations": {
                        "readOnlyHint": True,
                        "destructiveHint": False,
                        "idempotentHint": True,
                        "openWorldHint": False,
                    },
                }
            )
        return tools

    def _call_tool(
        self,
        params: Any,
        principal: Principal,
        *,
        request_id: str | None,
        request_id_value: Any,
    ) -> dict[str, Any]:
        if not isinstance(params, dict):
            self._audit_protocol_error(principal, request_id=request_id, method="tools/call", code=-32602)
            return _error(request_id_value, -32602, "Invalid params", {"reason": "params must be an object"})
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not name:
            self._audit_protocol_error(principal, request_id=request_id, method="tools/call", code=-32602)
            return _error(request_id_value, -32602, "Invalid params", {"reason": "name must be a string"})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            self._audit_protocol_error(principal, request_id=request_id, method="tools/call", code=-32602)
            return _error(
                request_id_value,
                -32602,
                "Invalid params",
                {"reason": "arguments must be an object"},
            )
        if self.registry is None:
            self._audit_protocol_error(principal, request_id=request_id, method="tools/call", code=-32603)
            return _error(request_id_value, -32603, "Internal error", {"reason": "tool registry unavailable"})

        call_id = f"mcp-{uuid.uuid4()}"
        try:
            result = self.registry.execute(
                name,
                arguments,
                principal,
                call_id=call_id,
            )
            payload = result.model_dump(mode="json")
            status = str(result.status)
        except Exception as exc:  # noqa: BLE001 - protocol errors must stay structured
            self._audit_protocol_error(principal, request_id=request_id, method="tools/call", code=-32603)
            if self.logger is not None:
                self.logger.error(
                    "mcp_tool_error",
                    extra={
                        "fields": {
                            "run_id": request_id,
                            "tool": name,
                            "error_type": type(exc).__name__,
                        }
                    },
                )
            return _error(
                request_id_value,
                -32603,
                "Internal error",
                {"reason": "tool execution failed", "error_type": type(exc).__name__},
            )

        event_type = "mcp_tool_denied" if status in _DENIED_STATUSES else "mcp_tool_call"
        record_security_event(
            self.run_store,
            event_type,
            request_id=request_id,
            principal_id=principal.principal_id,
            detail={
                "tool": name,
                "status": status,
                "call_id": call_id,
                "role": principal.role,
                "arguments": sorted(arguments) if isinstance(arguments, dict) else [],
            },
            logger=self.logger,
        )
        text = json.dumps(payload, ensure_ascii=False, default=str)
        return _response(
            request_id_value,
            {
                "content": [{"type": "text", "text": text}],
                "structuredContent": payload,
                "isError": status not in _NON_ERROR_STATUSES,
            },
        )

    def _audit_protocol_error(
        self,
        principal: Principal,
        *,
        request_id: str | None,
        method: str,
        code: int,
    ) -> None:
        record_security_event(
            self.run_store,
            "mcp_protocol_error",
            request_id=request_id,
            principal_id=principal.principal_id,
            detail={
                "method": method,
                "code": code,
                "role": principal.role,
            },
            logger=self.logger,
        )


__all__ = ["MCP_PROTOCOL_VERSIONS", "McpToolServer"]

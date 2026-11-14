"""Phase 8.2 API authorization, security audit and MCP transport tests."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from enterprise_business_agent.api import ResultStore, create_app
from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base, make_engine, make_readonly_engine

KEYS = {
    "admin-key": "admin",
    "approver-key": "approver",
    "analyst-key": "analyst",
    "restricted-key": "restricted_analyst",
}

RESTRICTED_POLICY = {
    "restricted_analyst": {
        "metrics": ["sales_amount"],
        "domains": ["Sales"],
        "resources": ["sales_fact"],
        "dimensions": ["region", "product"],
        "regions": ["EC"],
    }
}


@contextmanager
def _client(tmp_path, **overrides):
    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=24,
        api_timeout_seconds=5,
        api_keys=KEYS,
        allow_anonymous_api=True,
        authz_policy=RESTRICTED_POLICY,
        **overrides,
    )
    path = tmp_path / "phase8-security.db"
    write_engine = make_engine(f"sqlite:///{path}")
    Base.metadata.create_all(write_engine)
    with Session(write_engine) as seed:
        generate_data(seed, date(2024, 1, 1), 24, reset=False)
        seed.commit()
    write_engine.dispose()

    engine = make_readonly_engine(f"sqlite:///{path}", settings.query_timeout_ms)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    app = create_app(
        settings,
        session_factory=factory,
        engine=engine,
        llm_client=None,
        result_store=ResultStore(),
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        engine.dispose()


def _headers(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


def _mcp(client: TestClient, payload, key: str | None = "analyst-key"):
    headers = _headers(key) if key is not None else {}
    return client.post("/mcp", headers=headers, json=payload)


def _audit(client: TestClient, event_type: str):
    response = client.get(
        "/api/v1/security/audit",
        headers=_headers("admin-key"),
        params={"event_type": event_type},
    )
    assert response.status_code == 200
    return response.json()["events"]


# ---------------------------------------------------------------------------
# Endpoint authorization
# ---------------------------------------------------------------------------


def test_approver_cannot_read_business_metrics(tmp_path):
    with _client(tmp_path, mcp_enabled=False) as client:
        response = client.post(
            "/api/v1/metrics/query",
            headers=_headers("approver-key"),
            json={"metric": "sales_amount", "start": "2025-03-01", "end": "2025-03-31"},
        )
        assert response.status_code == 403
        body = response.json()
        assert body["error"]["code"] == "forbidden"
        assert body["error"]["detail"]["required_scope"] == "metrics:read"

        natural = client.post(
            "/api/v1/analysis/natural-language",
            headers=_headers("approver-key"),
            json={"question": "2025年3月销售额是多少", "as_of": "2025-04-01"},
        )
        assert natural.status_code == 403
        assert natural.json()["error"]["detail"]["required_scope"] == "metrics:read"


def test_restricted_role_cannot_use_sql_fallback(tmp_path):
    with _client(tmp_path, mcp_enabled=False) as client:
        response = client.post(
            "/api/v1/analysis/natural-language",
            headers=_headers("restricted-key"),
            json={
                "question": "2025年3月员工满意度是多少",
                "as_of": "2025-04-01",
                "allow_sql_fallback": True,
            },
        )
        body = response.json()
        assert response.status_code == 403
        assert body["status"] == "error"
        assert body["error"]["code"] == "forbidden"
        assert body["error"]["stage"] == "authorization"
        assert body["error"]["detail"]["reason"] == "sql_fallback_forbidden"


def test_empty_api_key_is_rejected(tmp_path):
    with _client(tmp_path, mcp_enabled=False) as client:
        response = client.get("/api/v1/security/audit", headers={"X-API-Key": ""})
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Security audit
# ---------------------------------------------------------------------------


def test_security_audit_is_admin_only_and_records_denials(tmp_path):
    with _client(tmp_path, mcp_enabled=False) as client:
        denied = client.post(
            "/api/v1/metrics/query",
            headers=_headers("approver-key"),
            json={"metric": "sales_amount", "start": "2025-03-01", "end": "2025-03-31"},
        )
        assert denied.status_code == 403

        events = _audit(client, "authorization_denied")
        assert any(event["detail"]["path"] == "/api/v1/metrics/query" for event in events)

        for event in events:
            assert event["event_type"] == "authorization_denied"
            assert event["detail"]["status_code"] == 403

        not_admin = client.get(
            "/api/v1/security/audit",
            headers=_headers("analyst-key"),
        )
        assert not_admin.status_code == 403

        unknown = client.get(
            "/api/v1/security/audit",
            headers=_headers("admin-key"),
            params={"event_type": "not_a_real_event"},
        )
        assert unknown.status_code == 422
        assert unknown.json()["error"]["code"] == "invalid_event_type"


def test_authentication_failure_is_audited(tmp_path):
    with _client(tmp_path, mcp_enabled=False) as client:
        response = client.get("/api/v1/security/audit")
        assert response.status_code == 401

        events = _audit(client, "auth_failed")
        assert events
        assert events[0]["principal_id"] is None


# ---------------------------------------------------------------------------
# MCP transport
# ---------------------------------------------------------------------------


def test_mcp_disabled_is_not_exposed(tmp_path):
    with _client(tmp_path, mcp_enabled=False) as client:
        response = _mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "mcp_disabled"


def test_mcp_requires_an_api_key_and_audits_failure(tmp_path):
    with _client(tmp_path, mcp_enabled=True) as client:
        response = _mcp(
            client,
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            key=None,
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"

        events = _audit(client, "mcp_auth_failed")
        assert events
        assert events[0]["detail"]["reason"] == "invalid_or_missing_api_key"


def test_mcp_initialize_and_tools_list(tmp_path):
    with _client(tmp_path, mcp_enabled=True) as client:
        response = _mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": "init-1",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"},
                },
            },
        )
        assert response.status_code == 200
        assert response.headers["Mcp-Protocol-Version"] == "2025-06-18"
        body = response.json()
        assert body["id"] == "init-1"
        assert body["result"]["protocolVersion"] == "2025-06-18"
        assert body["result"]["capabilities"]["tools"]["listChanged"] is False

        listed = _mcp(
            client,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert listed.status_code == 200
        tools = listed.json()["result"]["tools"]
        assert len(tools) == 8
        names = {tool["name"] for tool in tools}
        assert {"query_metric", "compare_periods", "top_n"} <= names
        for tool in tools:
            assert tool["annotations"]["readOnlyHint"] is True
            assert tool["annotations"]["destructiveHint"] is False
            assert tool["inputSchema"]["type"] == "object"


def test_mcp_tool_call_goes_through_registry_and_audits(tmp_path):
    with _client(tmp_path, mcp_enabled=True) as client:
        response = _mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "query_metric",
                    "arguments": {
                        "metric": "sales_amount",
                        "start": "2025-03-01",
                        "end": "2025-03-31",
                    },
                },
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == 3
        assert body["result"]["isError"] is False
        assert body["result"]["structuredContent"]["status"] == "ok"
        assert body["result"]["content"][0]["type"] == "text"

        events = _audit(client, "mcp_tool_call")
        assert events
        assert events[0]["detail"]["tool"] == "query_metric"
        assert events[0]["detail"]["status"] == "ok"


def test_mcp_tool_call_respects_metric_policy(tmp_path):
    with _client(tmp_path, mcp_enabled=True) as client:
        response = _mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "query_metric",
                    "arguments": {
                        "metric": "net_profit",
                        "start": "2025-03-01",
                        "end": "2025-03-31",
                    },
                },
            },
            key="restricted-key",
        )
        assert response.status_code == 200
        body = response.json()
        assert body["result"]["isError"] is True
        assert body["result"]["structuredContent"]["status"] == "permission_denied"

        events = _audit(client, "mcp_tool_denied")
        assert events
        assert events[0]["detail"]["status"] == "permission_denied"


def test_mcp_tools_list_is_filtered_by_role(tmp_path):
    with _client(tmp_path, mcp_enabled=True) as client:
        response = _mcp(
            client,
            {"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
            key="approver-key",
        )
        assert response.status_code == 200
        assert response.json()["result"]["tools"] == []


def test_mcp_protocol_errors_are_structured_and_audited(tmp_path):
    with _client(tmp_path, mcp_enabled=True) as client:
        unknown = _mcp(
            client,
            {"jsonrpc": "2.0", "id": 6, "method": "resources/list"},
        )
        assert unknown.status_code == 200
        assert unknown.json()["error"]["code"] == -32601

        invalid = _mcp(client, {"id": 7, "method": "ping"})
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == -32600

        malformed = client.post(
            "/mcp",
            content="{not-json",
            headers={
                "X-API-Key": "analyst-key",
                "Content-Type": "application/json",
            },
        )
        assert malformed.status_code == 400
        assert malformed.json()["error"]["code"] == -32700

        events = _audit(client, "mcp_protocol_error")
        assert events


def test_mcp_notification_returns_202_without_body(tmp_path):
    with _client(tmp_path, mcp_enabled=True) as client:
        response = _mcp(
            client,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert response.status_code == 202
        assert response.content == b""

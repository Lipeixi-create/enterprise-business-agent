"""Phase 6 backend permission matrix and resource-isolation tests."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from enterprise_business_agent.agent import ToolRegistry
from enterprise_business_agent.api import ResultStore, create_app
from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base, make_engine, make_readonly_engine
from enterprise_business_agent.security import AuthorizationPolicy, Principal

KEYS = {
    "sales-key": "sales_analyst",
    "marketing-key": "marketing_analyst",
    "finance-key": "finance_analyst",
    "supply-key": "supply_chain_analyst",
    "hr-key": "hr_analyst",
    "admin-key": "admin",
    "manager-key": "business_manager",
    "analyst-a": "analyst",
    "analyst-b": "analyst",
    "restricted-key": "restricted_analyst",
    "viewer-key": "viewer",
}


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=24,
        api_timeout_seconds=5,
        api_keys=KEYS,
        allow_anonymous_api=False,
        authz_policy={
            "restricted_analyst": {
                "metrics": ["sales_amount"],
                "dimensions": ["region", "product"],
                "regions": ["EC"],
            }
        },
    )
    path = tmp_path / "phase6-permissions.db"
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
        with TestClient(app) as test_client:
            yield test_client
    finally:
        engine.dispose()


def _query(client, key: str, metric: str, **overrides):
    payload = {
        "metric": metric,
        "start": "2025-03-01",
        "end": "2025-03-01",
        **overrides,
    }
    return client.post(
        "/api/v1/metrics/query",
        headers={"X-API-Key": key},
        json=payload,
    )


@pytest.mark.parametrize(
    ("key", "allowed_metric", "denied_metric"),
    [
        ("sales-key", "sales_amount", "net_profit"),
        ("marketing-key", "ROI", "sales_amount"),
        ("finance-key", "net_profit", "stockout_rate"),
        ("supply-key", "inventory_days", "labor_cost"),
        ("hr-key", "headcount", "revenue"),
    ],
)
def test_department_analysts_are_limited_to_their_domain(
    client, key, allowed_metric, denied_metric
):
    allowed = _query(client, key, allowed_metric)
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "ok"

    denied = _query(client, key, denied_metric)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "forbidden"


def test_domain_restriction_also_applies_to_natural_language(client):
    response = client.post(
        "/api/v1/analysis/natural-language",
        headers={"X-API-Key": "sales-key"},
        json={"question": "2025年第一季度净利润是多少", "as_of": "2025-04-01"},
    )
    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "forbidden"
    assert body["error"]["stage"] == "authorization"


def test_tool_allowlist_is_enforced_before_execution():
    class _Metric:
        name = "sales_amount"
        domain = "Sales"
        source_table = "sales_fact"

    class _Resolver:
        metrics = (_Metric(),)

        @classmethod
        def get(cls, name):
            return cls.metrics[0] if name == cls.metrics[0].name else None

    class _Dimension:
        name = "region"

    class _DimensionRegistry:
        dimensions = (_Dimension(),)

    class _Gateway:
        metric_resolver = _Resolver()
        dimension_registry = _DimensionRegistry()

    settings = Settings(
        authz_policy={
            "sales_analyst": {
                "domains": ["Sales"],
                "tools": ["query_metric"],
            }
        }
    )
    policy = AuthorizationPolicy(settings)
    principal = Principal(
        "key:sales",
        "sales_analyst",
        frozenset(
            {
                "metrics:read",
                "agent:run",
                "run:read",
                "evidence:read",
                "finding:read",
                "report:read",
            }
        ),
    )
    registry = ToolRegistry(gateway=_Gateway(), authorization=policy, settings=settings)

    allowed = registry.execute(
        "query_metric",
        {"metric": "sales_amount", "start": "2025-03-01", "end": "2025-03-01"},
        principal,
        call_id="allowed",
    )
    # The dummy gateway fails only after authorization, which proves the tool
    # itself was allowed. A denied tool never reaches argument validation.
    assert allowed.status == "execution_error"

    denied = registry.execute(
        "compare_periods",
        {
            "metric": "sales_amount",
            "kind": "mom",
            "year": 2025,
            "month": 3,
        },
        principal,
        call_id="denied",
    )
    assert denied.status == "permission_denied"
    assert denied.error.code == "permission_denied"

    names = {item["name"] for item in registry.specs(principal)}
    assert names == {"query_metric"}


def test_region_scope_cannot_be_bypassed_by_removing_or_widening_filters(client):
    omitted = _query(
        client,
        "restricted-key",
        "sales_amount",
        dimensions=["region"],
    )
    assert omitted.status_code == 403
    assert "must filter" in omitted.json()["error"]["message"]

    grouped_without_filter = _query(client, "restricted-key", "sales_amount")
    assert grouped_without_filter.status_code == 403

    other_region = _query(
        client,
        "restricted-key",
        "sales_amount",
        filters=[{"dimension": "region", "operator": "eq", "value": "NC"}],
    )
    assert other_region.status_code == 403

    widened_in = _query(
        client,
        "restricted-key",
        "sales_amount",
        filters=[
            {"dimension": "region", "operator": "in", "value": ["EC", "NC"]}
        ],
    )
    assert widened_in.status_code == 403

    allowed = _query(
        client,
        "restricted-key",
        "sales_amount",
        filters=[{"dimension": "region", "operator": "eq", "value": "EC"}],
    )
    assert allowed.status_code == 200


def test_unauthorized_principal_cannot_read_another_users_run(client):
    created = client.post(
        "/api/v1/agent/runs",
        headers={"X-API-Key": "analyst-a"},
        json={"question": "分析华东销售", "max_tool_calls": 1},
    )
    assert created.status_code == 200
    run_id = created.json()["run_id"]

    owner = client.get(
        f"/api/v1/agent/runs/{run_id}/evidence",
        headers={"X-API-Key": "analyst-a"},
    )
    assert owner.status_code == 200
    assert owner.json()["count"] == 0

    for path in (
        f"/api/v1/agent/runs/{run_id}",
        f"/api/v1/agent/runs/{run_id}/evidence",
        f"/api/v1/agent/runs/{run_id}/findings",
        f"/api/v1/agent/runs/{run_id}/report",
        f"/api/v1/agent/runs/{run_id}/report/versions",
        f"/api/v1/agent/runs/{run_id}/report/validation",
    ):
        denied = client.get(path, headers={"X-API-Key": "analyst-b"})
        assert denied.status_code == 404, path
        assert denied.json()["error"]["code"] == "agent_run_not_found"

    viewer = client.get(
        f"/api/v1/agent/runs/{run_id}/report",
        headers={"X-API-Key": "viewer-key"},
    )
    assert viewer.status_code == 403
    assert viewer.json()["error"]["code"] == "forbidden"


def test_admin_and_business_manager_can_read_cross_owner_resources(client):
    created = client.post(
        "/api/v1/agent/runs",
        headers={"X-API-Key": "analyst-a"},
        json={"question": "分析华东销售", "max_tool_calls": 1},
    )
    run_id = created.json()["run_id"]

    for key in ("admin-key", "manager-key"):
        run = client.get(
            f"/api/v1/agent/runs/{run_id}",
            headers={"X-API-Key": key},
        )
        assert run.status_code == 200
        evidence = client.get(
            f"/api/v1/agent/runs/{run_id}/evidence",
            headers={"X-API-Key": key},
        )
        assert evidence.status_code == 200
        findings = client.get(
            f"/api/v1/agent/runs/{run_id}/findings",
            headers={"X-API-Key": key},
        )
        assert findings.status_code == 200

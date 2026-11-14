"""Phase 7.6: evaluator trust fixes.

These are deterministic offline tests. They cover the two defects found in the
third-round A batch:

1. ``_unverified_numbers`` only read Evidence summaries, so a correct detail
   number shown in a trusted tool message was treated as fabricated.
2. No relation check existed, so P008=14,779 was accepted as the maximum even
   though P003=18,163 is larger.

No test here calls a real model.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session, sessionmaker

from enterprise_business_agent.agent import (
    AgentRuntime,
    AnalysisGateway,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
)
from enterprise_business_agent.agent.contracts import EvidenceDraft, ModelToolMessage
from enterprise_business_agent.agent.evaluation import (
    LiveEvalCase,
    PlannedCall,
    _archive_tool_messages,
    _number_matches,
    _redact_archive_text,
    _source_numbers,
    _unsourced_numbers,
    run_live_agent_case,
)
from enterprise_business_agent.agent.relations import relation_issues
from enterprise_business_agent.agent.tools import MetricQueryArgs
from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base, make_engine, make_readonly_engine
from enterprise_business_agent.security import AuthorizationPolicy, Principal
from tests.agent_fakes import ScriptedLLM, finish_response, tool_response

ANALYST = Principal("key:test", "analyst", frozenset({"metrics:read", "agent:run"}))


def _message(
    rows: list[dict],
    *,
    tool: str = "top_n",
    trusted: bool = True,
    reused: bool = False,
    arguments: dict | None = None,
    metric: str = "sales_amount",
    label: str = "销售额",
    row_count: int | None = None,
    truncated: bool | None = None,
    content: str = "{}",
) -> ModelToolMessage:
    rows = list(rows)
    view = {
        "tool": tool,
        "metric": metric,
        "label": label,
        "rows": rows,
        "row_count": len(rows) if row_count is None else row_count,
        "displayed_count": len(rows),
        "truncated": bool(truncated) if truncated is not None else False,
    }
    encoded = content.encode("utf-8")
    return ModelToolMessage(
        tool_call_id="call-1",
        tool=tool,
        status="duplicate_suppressed" if reused else "ok",
        reused=reused,
        trusted=trusted,
        evidence_ids=["ev-1"] if trusted else [],
        arguments=arguments or {"metric": metric, "dimension": "region", "n": len(rows)},
        content=content,
        content_sha256=hashlib.sha256(encoded).hexdigest(),
        content_bytes=len(encoded),
        visible_data=view,
        row_count=view["row_count"],
        displayed_count=view["displayed_count"],
        truncated=view["truncated"],
    )


def _state(
    messages: list[ModelToolMessage],
    *,
    evidence: str = "销售额查询完成。",
) -> dict:
    return {
        "evidence": [
            EvidenceDraft(
                evidence_id="ev-1",
                tool_call_id="call-1",
                result_summary=evidence,
                traceable=True,
            )
        ],
        "tool_messages": messages,
        "verified_findings": [],
        "observations": [],
    }


# --------------------------------------------------------------------------
# 7.6.1 number verification now uses trusted, model-visible detail data
# --------------------------------------------------------------------------


def test_detail_number_in_trusted_tool_message_is_verified():
    message = _message(
        [
            {"dimension_value": "P003", "value": 18163},
            {"dimension_value": "P008", "value": 14779},
        ]
    )
    state = _state([message], evidence="返回 2 行明细。")

    unverified = AgentRuntime._unverified_numbers("P008 的销售额为 14779，P003 为 18163。", state)

    assert unverified == []


def test_fabricated_number_is_still_flagged():
    message = _message([{"dimension_value": "P003", "value": 18163}])
    state = _state([message], evidence="返回 1 行明细。")

    unverified = AgentRuntime._unverified_numbers("P003 的销售额为 18163，另有 99999 来自未知来源。", state)

    assert unverified == ["99999"]


def test_untrusted_tool_message_cannot_verify_numbers():
    message = _message(
        [{"dimension_value": "P003", "value": 77777}],
        trusted=False,
    )
    state = _state([message], evidence="证据不可追溯。")

    unverified = AgentRuntime._unverified_numbers("P003 的销售额为 77777。", state)

    assert unverified == ["77777"]


def test_reused_dedupe_message_verifies_numbers_when_traceable():
    message = _message(
        [{"dimension_value": "P003", "value": 88888}],
        reused=True,
        content='{"reused": true, "summary": "复用上次结果"}',
    )
    state = _state([message], evidence="复用上次结果。")

    unverified = AgentRuntime._unverified_numbers("P003 的销售额为 88888。", state)

    assert unverified == []


def test_numbers_from_undisplayed_rows_of_a_truncated_result_do_not_verify():
    message = _message(
        [
            {"dimension_value": "P003", "value": 18163},
            {"dimension_value": "P008", "value": 14779},
        ],
        row_count=25,
        truncated=True,
    )
    state = _state([message], evidence="仅展示前 20 行，共 25 行。")

    displayed = AgentRuntime._unverified_numbers("P003 为 18163。", state)
    undisplayed = AgentRuntime._unverified_numbers("第 25 名为 55555。", state)

    assert displayed == []
    assert undisplayed == ["55555"]


# --------------------------------------------------------------------------
# 7.6.2 deterministic business-relation validation
# --------------------------------------------------------------------------


def _p026_rows() -> list[dict]:
    return [
        {"dimension_value": "P003", "denominator": 18163},
        {"dimension_value": "P008", "denominator": 14779},
        {"dimension_value": "P001", "denominator": 9000},
    ]


def test_correct_maximum_claim_passes():
    message = _message(_p026_rows(), label="销售额")

    issues = relation_issues("P003 的分母最大，为 18163。", [message])

    assert issues == []


def test_wrong_maximum_claim_is_rejected_even_though_the_number_exists():
    message = _message(_p026_rows(), label="销售额")

    issues = relation_issues("P008 的分母最大，为 14779。", [message])

    assert [item["kind"] for item in issues] == ["max_mismatch"]
    assert issues[0]["claimed"] == "14779"
    assert issues[0]["actual"] == "18163"
    assert issues[0]["actual_member"] == "P003"


def test_ranked_enumeration_after_maximum_only_claims_the_first_entry():
    message = _message(
        [
            {"dimension_value": "P003", "value": 18163},
            {"dimension_value": "P008", "value": 14779},
            {"dimension_value": "P001", "value": 9000},
        ]
    )

    issues = relation_issues("销售额最高的是 P003 18163、P008 14779、P001 9000。", [message])

    assert issues == []


def test_ranked_enumeration_still_rejects_a_wrong_first_entry():
    message = _message(
        [
            {"dimension_value": "P003", "value": 18163},
            {"dimension_value": "P008", "value": 14779},
            {"dimension_value": "P001", "value": 9000},
        ]
    )

    issues = relation_issues("销售额最高的是 P008 14779、P003 18163、P001 9000。", [message])

    assert {item["member"] for item in issues} == {"P008"}
    assert "max_mismatch" in {item["kind"] for item in issues}


def test_tie_wording_still_checks_every_named_member():
    message = _message(
        [
            {"dimension_value": "P003", "value": 18163},
            {"dimension_value": "P008", "value": 14779},
            {"dimension_value": "P001", "value": 9000},
        ]
    )

    issues = relation_issues("销售额最高的是 P003 和 P008。", [message])

    assert [item["member"] for item in issues] == ["P008"]


def test_attributive_maximum_applies_to_the_named_member_not_the_subject():
    message = _message(
        [
            {"dimension_value": "EC", "value": "0.287860"},
            {"dimension_value": "WC", "value": "0.294995"},
            {"dimension_value": "NC", "value": "0.295126"},
        ],
        arguments={"metric": "gross_margin", "dimension": "region", "n": 3, "ascending": True},
    )

    assert relation_issues("EC 与最高的 NC 相差 0.007266。", [message]) == []


def test_relation_check_ignores_untrusted_messages():
    message = _message(_p026_rows(), label="销售额", trusted=False)

    assert relation_issues("P008 的分母最大，为 14779。", [message]) == []


def test_truncated_result_scopes_the_maximum_to_displayed_rows():
    message = _message(
        [
            {"dimension_value": "P003", "denominator": 18163},
            {"dimension_value": "P008", "denominator": 14779},
        ],
        row_count=25,
        truncated=True,
        label="销售额",
    )

    issues = relation_issues("P008 的分母最大，为 14779。", [message])

    assert [item["kind"] for item in issues] == ["max_mismatch"]
    assert issues[0]["truncated"] is True
    assert "已展示的" in issues[0]["message"]


def test_unique_claim_is_rejected_when_values_tie():
    message = _message(
        [
            {"dimension_value": "P003", "denominator": 18163},
            {"dimension_value": "P009", "denominator": 18163},
        ],
        label="销售额",
    )

    issues = relation_issues("P003 的分母最大，且是唯一最大值。", [message])

    assert [item["kind"] for item in issues] == ["unique_claim_violated"]


def test_unique_claim_passes_with_a_single_winner():
    message = _message(_p026_rows(), label="销售额")

    issues = relation_issues("P003 的分母最大，且是唯一最大值。", [message])

    assert issues == []


def test_superlative_uses_the_named_metric_not_a_supporting_column():
    message = _message(
        [
            {
                "dimension_value": "C006",
                "value": 0.446114,
                "numerator": 229908.63,
                "denominator": 515358.42,
            },
            {
                "dimension_value": "C007",
                "value": 1.737348,
                "numerator": 381099.40,
                "denominator": 219356.98,
            },
        ],
        metric="ROI",
        label="营销 ROI",
        arguments={"metric": "ROI", "dimension": "campaign", "n": 2, "ascending": True},
    )

    answer = "C006 的 ROI 最低，ROI=0.446114（活动归因收入 229908.63 / 营销投入 515358.42）。"

    assert relation_issues(answer, [message]) == []


def test_rank_names_the_member_immediately_after_the_ordinal():
    message = _message(
        [
            {"dimension_value": "P004", "value": 87.345507, "rank": 1},
            {"dimension_value": "P005", "value": 79.203518, "rank": 2},
            {"dimension_value": "P007", "value": 30.0, "rank": 3},
        ],
        metric="inventory_days",
        label="库存天数",
    )

    answer = "P004 和 P005 分别比第三名 P007 高 57.345507 天和 49.203518 天。"

    assert relation_issues(answer, [message]) == []


def test_smallest_decline_compares_magnitude_of_signed_contributions():
    message = _message(
        [
            {"dimension_value": "NC", "contribution": -12159846.56},
            {"dimension_value": "SC", "contribution": -11706262.12},
            {"dimension_value": "EC", "contribution": -10419386.31},
            {"dimension_value": "WC", "contribution": -10385249.43},
        ],
        tool="contribution",
        metric="sales_amount",
        label="销售额",
    )

    assert relation_issues("西部（WC）降幅贡献最小。", [message]) == []


def test_rank_claim_must_match_the_tool_rank():
    message = _message(
        [
            {"dimension_value": "R1", "value": 300, "rank": 1},
            {"dimension_value": "R2", "value": 200, "rank": 2},
        ]
    )

    issues = relation_issues("R2 排名第 1。", [message])

    assert [item["kind"] for item in issues] == ["rank_mismatch"]


def test_quarter_is_not_mistaken_for_a_rank_claim():
    message = _message(
        [
            {"dimension_value": "ECOM", "value": 100251973.05, "rank": 1},
            {"dimension_value": "DIST", "value": 74153877.78, "rank": 2},
        ],
        arguments={"metric": "sales_amount", "dimension": "channel", "n": 2},
    )

    issues = relation_issues("2025年第四季度销售额最高的渠道是电商（ECOM）。", [message])

    assert issues == []


def test_negated_tie_claim_is_not_a_max_claim():
    message = _message(
        [
            {"dimension_value": "NC", "value": 0.295126, "rank": 1},
            {"dimension_value": "SC", "value": 0.295097, "rank": 2},
            {"dimension_value": "WC", "value": 0.294995, "rank": 3},
            {"dimension_value": "EC", "value": 0.287860, "rank": 4},
        ],
        arguments={"metric": "gross_margin", "dimension": "region", "n": 4},
        metric="gross_margin",
        label="毛利率",
    )

    issues = relation_issues(
        "前三名（NC、SC、WC）非常接近，相差仅 0.000131，但数值并不相等，因此不能称为并列最高。",
        [message],
    )

    assert issues == []


def test_count_claim_must_match_the_returned_row_count():
    message = _message([{"dimension_value": f"R{index}", "value": 500 - index * 10} for index in range(5)])

    issues = relation_issues("共 3 个区域贡献了主要销售额。", [message])

    assert [item["kind"] for item in issues] == ["count_mismatch"]


def test_top_n_rows_must_be_ordered_by_the_requested_direction():
    message = _message(
        [
            {"dimension_value": "R1", "value": 100, "rank": 1},
            {"dimension_value": "R2", "value": 300, "rank": 2},
        ],
        arguments={"metric": "sales_amount", "dimension": "region", "n": 2},
    )

    issues = relation_issues("已完成排名。", [message])

    assert [item["kind"] for item in issues] == ["top_n_order_mismatch"]


def test_answer_order_cannot_reverse_the_top_n_result():
    message = _message(
        [
            {"dimension_value": "R1", "value": 300, "rank": 1},
            {"dimension_value": "R2", "value": 200, "rank": 2},
        ]
    )

    issues = relation_issues("R2 高于 R1。", [message])

    assert [item["kind"] for item in issues] == ["answer_order_mismatch"]


def test_monthly_rows_are_compared_within_the_named_period():
    message = _message(
        [
            {
                "period": "2025-10",
                "dimensions": {"region": "EC"},
                "dimension_labels": {"region": "华东"},
                "value": "10",
            },
            {
                "period": "2025-10",
                "dimensions": {"region": "NC"},
                "dimension_labels": {"region": "华北"},
                "value": "12",
            },
            {
                "period": "2025-11",
                "dimensions": {"region": "EC"},
                "dimension_labels": {"region": "华东"},
                "value": "20",
            },
            {
                "period": "2025-11",
                "dimensions": {"region": "NC"},
                "dimension_labels": {"region": "华北"},
                "value": "30",
            },
        ],
        tool="query_metric",
        metric="sales_amount",
        label="销售额",
    )

    issues = relation_issues("各区域 11 月销售额均为当季最高：华东 20、华北 30。", [message])

    assert issues == []


def test_monthly_global_claim_is_still_checked_against_the_named_period():
    message = _message(
        [
            {
                "period": "2025-10",
                "dimensions": {"region": "EC"},
                "dimension_labels": {"region": "华东"},
                "value": "10",
            },
            {
                "period": "2025-10",
                "dimensions": {"region": "NC"},
                "dimension_labels": {"region": "华北"},
                "value": "12",
            },
            {
                "period": "2025-11",
                "dimensions": {"region": "EC"},
                "dimension_labels": {"region": "华东"},
                "value": "20",
            },
            {
                "period": "2025-11",
                "dimensions": {"region": "NC"},
                "dimension_labels": {"region": "华北"},
                "value": "30",
            },
        ],
        tool="query_metric",
        metric="sales_amount",
        label="销售额",
    )

    issues = relation_issues("华东 11 月的 20 为全部数据点中的最大值。", [message])

    assert [item["kind"] for item in issues] == ["max_mismatch"]
    assert issues[0]["member"] == "EC"


def test_rank_comparison_phrase_is_not_a_rank_claim():
    message = _message(
        [
            {"dimension_value": "R1", "value": 300, "rank": 1},
            {"dimension_value": "R2", "value": 200, "rank": 2},
        ]
    )

    assert relation_issues("R2 与第一名相差 100。", [message]) == []
    assert relation_issues("R2 排名第 1。", [message])[0]["kind"] == "rank_mismatch"


def test_answer_order_ignores_the_extremum_subject_and_the_other_metric_block():
    margin = _message(
        [
            {"dimension_value": "EC", "dimension_label": "华东", "value": "0.287860", "rank": 1},
            {"dimension_value": "WC", "dimension_label": "西部", "value": "0.294995", "rank": 2},
            {"dimension_value": "SC", "dimension_label": "华南", "value": "0.295097", "rank": 3},
            {"dimension_value": "NC", "dimension_label": "华北", "value": "0.295126", "rank": 4},
        ],
        arguments={
            "metric": "gross_margin",
            "dimension": "region",
            "n": 4,
            "ascending": True,
        },
        metric="gross_margin",
        label="毛利率",
    )
    stockout = _message(
        [
            {"dimension_value": "WC", "dimension_label": "西部", "value": "0.031772", "rank": 1},
            {"dimension_value": "NC", "dimension_label": "华北", "value": "0.031807", "rank": 2},
            {"dimension_value": "SC", "dimension_label": "华南", "value": "0.031813", "rank": 3},
            {"dimension_value": "EC", "dimension_label": "华东", "value": "0.283026", "rank": 4},
        ],
        arguments={
            "metric": "stockout_rate",
            "dimension": "region",
            "n": 4,
            "ascending": True,
        },
        metric="stockout_rate",
        label="缺货率",
    )
    answer = (
        "1. **毛利率最低的区域是华东（EC）**：0.287860，低于西部 0.294995、"
        "华南 0.295097、华北 0.295126。\n"
        "2. **华东的缺货率最高**：0.283026，其余为西部 0.031772、"
        "华北 0.031807、华南 0.031813。"
    )

    assert relation_issues(answer, [margin, stockout]) == []


def test_number_in_a_monthly_table_inherits_the_metric_heading():
    answer = (
        "**2025 年第四季度各区域预算偏差金额**\n\n"
        "| 区域 | 2025-10 | 2025-11 |\n"
        "|---|---|---|\n"
        "| 华东（EC） | 1215417.39 | 1423421.33 |"
    )

    assert _number_matches(answer, "1215417.39", terms=["预算偏差"])
    wrong_heading = answer.replace("预算偏差", "净利润")
    assert not _number_matches(wrong_heading, "1215417.39", terms=["预算偏差"])


# --------------------------------------------------------------------------
# 7.6.3 provenance uses the rows the model actually saw
# --------------------------------------------------------------------------


def _recorded(hidden_value: int) -> dict:
    result = SimpleNamespace(
        summary="返回 4 行明细。",
        data={
            "items": [
                {"dimension_value": "P003", "value": 18163},
                {"dimension_value": "P008", "value": 14779},
                {"dimension_value": "P001", "value": hidden_value},
            ]
        },
    )
    return {"call-1": ("top_n", {}, result)}


def test_source_numbers_prefer_displayed_rows_over_the_full_result():
    message = _message(_p026_rows()[:2], row_count=3, truncated=True)

    with_messages = _source_numbers(_recorded(55555), [message])
    without_messages = _source_numbers(_recorded(55555), None)

    assert Decimal("55555") not in with_messages
    assert Decimal("18163") in with_messages
    assert Decimal("55555") in without_messages


def test_source_numbers_ignore_untrusted_messages():
    message = _message(_p026_rows(), trusted=False)

    values = _source_numbers(_recorded(55555), [message])

    assert values == []


def test_source_numbers_never_fall_back_when_capture_is_empty():
    values = _source_numbers(_recorded(55555), [])

    assert values == []


# --------------------------------------------------------------------------
# 7.6.4 tool-message archive
# --------------------------------------------------------------------------


def test_archive_records_identity_hash_and_visible_rows():
    message = _message(_p026_rows(), content='{"summary": "返回 3 行"}')

    archived = _archive_tool_messages("run-123", "phase7-026", [message])

    assert len(archived) == 1
    entry = archived[0]
    assert entry["run_id"] == "run-123"
    assert entry["case_id"] == "phase7-026"
    assert entry["tool_call_id"] == "call-1"
    assert entry["tool"] == "top_n"
    assert entry["content"] == message.content
    assert entry["content_sha256"] == hashlib.sha256(message.content.encode("utf-8")).hexdigest()
    assert entry["visible_data"]["rows"][0]["dimension_value"] == "P003"
    assert entry["row_count"] == 3


def test_archive_marks_reused_calls_and_the_requested_tool():
    message = _message(
        _p026_rows(),
        reused=True,
        arguments={"metric": "sales_amount", "dimension": "region", "n": 3},
    )
    message = message.model_copy(update={"requested_tool": "top_n"})

    entry = _archive_tool_messages("run-1", "case-1", [message])[0]

    assert entry["reused"] is True
    assert entry["requested_tool"] == "top_n"
    assert entry["status"] == "duplicate_suppressed"


def test_archive_redacts_secret_like_assignments():
    text = "查询完成 api_key: sk-live-123 password=hunter2"

    redacted = _redact_archive_text(text)

    assert "sk-live-123" not in redacted
    assert "hunter2" not in redacted
    assert redacted.count("***") == 2


def test_archive_caps_oversized_content_without_losing_the_hash():
    content = "数" * 20_000
    message = _message(_p026_rows(), content=content)

    entry = _archive_tool_messages("run-1", "case-1", [message])[0]

    assert entry["content_truncated"] is True
    assert entry["content_bytes"] == len(content.encode("utf-8"))
    assert entry["content_sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert len(entry["content"].encode("utf-8")) <= 16_384


# --------------------------------------------------------------------------
# Runtime / evaluator integration
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def runtime_env(tmp_path_factory):
    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=24,
        agent_max_tool_calls=4,
        agent_max_seconds=10,
        agent_tool_timeout_seconds=2,
        agent_tool_max_retries=0,
    )
    path = tmp_path_factory.mktemp("phase7-6") / "evaluator.db"
    write_engine = make_engine(f"sqlite:///{path}")
    Base.metadata.create_all(write_engine)
    with Session(write_engine) as seed:
        generate_data(seed, date(2024, 1, 1), 24, reset=False)
        seed.commit()
    read_engine = make_readonly_engine(f"sqlite:///{path}", settings.query_timeout_ms)
    factory = sessionmaker(bind=read_engine, class_=Session, expire_on_commit=False)
    registry = ToolRegistry(
        AnalysisGateway(factory, settings),
        AuthorizationPolicy(settings),
        settings,
    )
    try:
        yield settings, registry
    finally:
        read_engine.dispose()
        write_engine.dispose()


def test_dedupe_reuse_is_captured_as_a_trusted_reused_message(runtime_env):
    settings, registry = runtime_env
    llm = ScriptedLLM(
        [
            tool_response(
                "c1",
                "query_metric",
                {
                    "metric": "inventory_days",
                    "dimensions": ["product"],
                    "grain": "total",
                    "start": "2024-02-01",
                    "end": "2024-02-01",
                },
            ),
            tool_response(
                "c2",
                "top_n",
                {
                    "metric": "inventory_days",
                    "dimension": "product",
                    "start": "2024-02-01",
                    "end": "2024-02-01",
                    "n": 8,
                },
            ),
            finish_response("已完成 8 个产品的库存天数排名。"),
        ]
    )
    runtime = AgentRuntime(llm_client=llm, registry=registry, settings=settings)

    result = runtime.run("8 个产品的库存天数排名", ANALYST)

    normal, reused = result.tool_messages
    assert normal.reused is False
    assert reused.reused is True
    assert reused.status == "duplicate_suppressed"
    assert reused.tool == "query_metric"
    assert reused.requested_tool == "top_n"
    assert reused.trusted is True
    assert reused.evidence_ids == normal.evidence_ids
    assert reused.visible_data["rows"] == normal.visible_data["rows"]


def test_tool_result_without_evidence_is_not_trusted_and_cannot_verify_numbers(
    runtime_env,
):
    settings, registry = runtime_env

    def ok_without_evidence(gateway, args):
        return ToolResult(
            status="ok",
            summary="返回 1 行明细。",
            data={
                "points": [
                    {
                        "period": "2025-01-01",
                        "dimensions": {"region": "EAST"},
                        "value": 12345,
                    }
                ]
            },
        )

    definitions = [
        ToolDefinition(
            name="query_metric",
            description="ok result without evidence",
            args_model=MetricQueryArgs,
            handler=ok_without_evidence,
        )
    ]
    custom = ToolRegistry(
        registry.gateway,
        registry.authorization,
        settings,
        definitions=definitions,
    )
    llm = ScriptedLLM(
        [
            tool_response(
                "c1",
                "query_metric",
                {"metric": "sales_amount", "start": "2025-01-01", "end": "2025-01-01"},
            ),
            finish_response("EAST 区域的销售额为 12345。"),
        ]
    )
    runtime = AgentRuntime(llm_client=llm, registry=custom, settings=settings)

    result = runtime.run("查询 EAST 区域销售额", ANALYST)

    assert result.tool_messages[0].trusted is False
    assert any("无法在工具证据中直接核对数字" in item and "12345" in item for item in result.warnings)


def _top_n_plan_llm(answer_for_rows):
    def decide(llm: ScriptedLLM):
        tool_messages = [item for item in llm.calls[-1]["messages"] if item["role"] == "tool"]
        if not tool_messages:
            return tool_response(
                "c1",
                "top_n",
                {
                    "metric": "sales_amount",
                    "dimension": "region",
                    "start": "2025-03-01",
                    "end": "2025-03-01",
                    "n": 3,
                },
            )
        rows = json.loads(tool_messages[-1]["content"])["data"]["rows"]
        return finish_response(answer_for_rows(rows))

    return ScriptedLLM([decide, decide])


def test_runtime_archived_tool_message_equals_the_message_the_model_received(runtime_env):
    settings, registry = runtime_env
    llm = _top_n_plan_llm(lambda rows: "已完成区域排名。")
    runtime = AgentRuntime(llm_client=llm, registry=registry, settings=settings)

    result = runtime.run("分析2025年3月区域销售排名", ANALYST)

    received = [item for item in llm.calls[-1]["messages"] if item["role"] == "tool"]
    assert len(result.tool_messages) == 1
    captured = result.tool_messages[0]
    assert captured.content == received[0]["content"]
    assert captured.content_sha256 == hashlib.sha256(received[0]["content"].encode("utf-8")).hexdigest()
    assert captured.trusted is True
    assert captured.visible_data is not None
    assert captured.visible_data["rows"] == json.loads(received[0]["content"])["data"]["rows"]


def test_eval_record_archives_tool_messages_and_flags_a_wrong_relation(runtime_env):
    settings, registry = runtime_env

    def answer(rows):
        second = rows[1]
        return f"{second['dimension_value']} 的销售额最高，为 {second['value']} 元。"

    llm = _top_n_plan_llm(answer)
    runtime = AgentRuntime(llm_client=llm, registry=registry, settings=settings)
    case = LiveEvalCase(
        id="phase7-6-relation",
        category="Sales",
        question="分析2025年3月区域销售排名",
        required_calls=[
            PlannedCall(
                tool="top_n",
                arguments={
                    "metric": "sales_amount",
                    "dimension": "region",
                    "start": "2025-03-01",
                    "end": "2025-03-01",
                    "n": 3,
                },
            )
        ],
        expect_number=False,
        min_answer_chars=1,
    )

    result = run_live_agent_case(
        case,
        runtime=runtime,
        registry=registry,
        principal=ANALYST,
    )

    assert result.tool_messages
    archived = result.tool_messages[0]
    assert archived["run_id"].startswith("eval-phase7-6-relation-")
    assert archived["case_id"] == "phase7-6-relation"
    assert archived["tool"] == "top_n"
    assert result.relation_validation_passed is False
    assert any(item["kind"] == "max_mismatch" for item in result.relation_issues)
    assert result.conclusion_overreach is True
    assert result.detail["tool_messages_archived"] == len(result.tool_messages)
    exported = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
    assert '"tool_messages"' in exported


def test_eval_record_passes_when_the_claimed_maximum_is_correct(runtime_env):
    settings, registry = runtime_env

    def answer(rows):
        first = rows[0]
        return f"{first['dimension_value']} 的销售额最高，为 {first['value']} 元。"

    llm = _top_n_plan_llm(answer)
    runtime = AgentRuntime(llm_client=llm, registry=registry, settings=settings)
    case = LiveEvalCase(
        id="phase7-6-correct-max",
        category="Sales",
        question="分析2025年3月区域销售排名",
        required_calls=[
            PlannedCall(
                tool="top_n",
                arguments={
                    "metric": "sales_amount",
                    "dimension": "region",
                    "start": "2025-03-01",
                    "end": "2025-03-01",
                    "n": 3,
                },
            )
        ],
        expect_number=False,
        min_answer_chars=1,
    )

    result = run_live_agent_case(
        case,
        runtime=runtime,
        registry=registry,
        principal=ANALYST,
    )

    assert result.relation_validation_passed is True
    assert result.relation_issues == []
    assert result.answer_number_provenance is True


# --------------------------------------------------------------------------
# 7.8.1 multi-member superlative checks (evaluator fix #2)
# --------------------------------------------------------------------------


def _multi_rows() -> list[dict]:
    return [
        {"dimension_value": "P003", "denominator": 18163, "numerator": 500},
        {"dimension_value": "P008", "denominator": 14779, "numerator": 900},
        {"dimension_value": "P009", "denominator": 9000, "numerator": 700},
    ]


def test_p008_wrong_inventory_days_maximum_is_rejected():
    message = _message(
        [
            {"dimension_value": "P003", "value": 18163},
            {"dimension_value": "P008", "value": 14779},
        ],
        metric="inventory_days",
        label="库存天数",
    )

    issues = relation_issues("P008 的库存天数最大，为 14779。", [message])

    assert [item["kind"] for item in issues] == ["max_mismatch"]
    assert issues[0]["member"] == "P008"
    assert issues[0]["claimed"] == "14779"
    assert issues[0]["actual"] == "18163"
    assert issues[0]["actual_member"] == "P003"


def test_historical_p008_denominator_sentence_is_rejected():
    message = _message(
        [
            {"dimension_value": "P001", "denominator": 5000},
            {"dimension_value": "P002", "denominator": 6000},
            {"dimension_value": "P003", "denominator": 18163},
            {"dimension_value": "P004", "denominator": 7000},
            {"dimension_value": "P005", "denominator": 8000},
            {"dimension_value": "P006", "denominator": 9000},
            {"dimension_value": "P007", "denominator": 10000},
            {"dimension_value": "P008", "denominator": 14779},
        ],
        metric="inventory_days",
        label="库存天数",
    )

    issues = relation_issues(
        "其中 P008 分母（14779.000000）为 8 个产品中最大，库存为零。",
        [message],
    )

    assert [item["kind"] for item in issues] == ["max_mismatch"]
    assert issues[0]["member"] == "P008"
    assert issues[0]["field"] == "denominator"
    assert issues[0]["actual"] == "18163"
    assert issues[0]["actual_member"] == "P003"


def test_p003_correct_inventory_days_maximum_passes():
    message = _message(
        [
            {"dimension_value": "P003", "value": 18163},
            {"dimension_value": "P008", "value": 14779},
        ],
        metric="inventory_days",
        label="库存天数",
    )

    assert relation_issues("P003 的库存天数最大，为 18163。", [message]) == []


def test_two_true_tied_firsts_pass():
    message = _message(
        [
            {"dimension_value": "P003", "denominator": 18163},
            {"dimension_value": "P009", "denominator": 18163},
            {"dimension_value": "P008", "denominator": 14779},
        ],
        label="销售额",
    )

    assert relation_issues("P003 和 P009 的分母最高。", [message]) == []
    assert relation_issues("分母最高的是 P003 和 P009。", [message]) == []


def test_claiming_first_and_second_as_highest_fails():
    message = _message(_multi_rows(), label="销售额")

    issues = relation_issues("P003 和 P008 的分母最高。", [message])

    assert [item["kind"] for item in issues] == ["max_mismatch"]
    assert issues[0]["member"] == "P008"
    assert issues[0]["actual_member"] == "P003"


def test_every_wrong_member_in_a_multi_member_claim_is_identified():
    message = _message(_multi_rows(), label="销售额")

    issues = relation_issues("P003、P008 和 P009 的分母最大。", [message])

    assert {item["kind"] for item in issues} == {"max_mismatch"}
    assert sorted(item["member"] for item in issues) == ["P008", "P009"]
    assert {item["actual"] for item in issues} == {"18163"}


def test_tied_members_with_unique_wording_are_rejected():
    message = _message(
        [
            {"dimension_value": "P003", "denominator": 18163},
            {"dimension_value": "P009", "denominator": 18163},
            {"dimension_value": "P008", "denominator": 14779},
        ],
        label="销售额",
    )

    issues = relation_issues("P003 和 P009 的分母最高，且为唯一最大值。", [message])

    assert [item["kind"] for item in issues] == ["unique_claim_violated"]


def test_truncated_data_cannot_infer_a_global_extremum():
    message = _message(
        [
            {"dimension_value": "P003", "denominator": 18163},
            {"dimension_value": "P008", "denominator": 14779},
        ],
        label="销售额",
        row_count=25,
        truncated=True,
    )

    issues = relation_issues("所有产品的分母中，P003 最大。", [message])

    assert [item["kind"] for item in issues] == ["truncated_scope_overreach"]
    assert issues[0]["truncated"] is True
    assert relation_issues("已展示的产品中，P003 最大。", [message]) == []


def test_minimum_and_multi_member_minimum_are_validated():
    message = _message(_multi_rows(), label="销售额")

    assert relation_issues("P009 的分母最小。", [message]) == []

    issues = relation_issues("P008 和 P009 的分母最低。", [message])

    assert [item["kind"] for item in issues] == ["min_mismatch"]
    assert issues[0]["member"] == "P008"
    assert issues[0]["claimed"] == "14779"
    assert issues[0]["actual"] == "9000"
    assert issues[0]["actual_member"] == "P009"


def test_max_and_min_claims_in_one_sentence_are_both_checked():
    message = _message(_multi_rows(), label="销售额")

    issues = relation_issues("P003 的分母最高，P008 的分母最低。", [message])

    assert [item["kind"] for item in issues] == ["min_mismatch"]
    assert issues[0]["member"] == "P008"


def test_field_hints_keep_numerator_and_denominator_apart():
    message = _message(_multi_rows(), label="销售额")

    assert relation_issues("P003 的分母最大。", [message]) == []
    assert relation_issues("P008 的分子最大。", [message]) == []

    wrong_numerator = relation_issues("P003 的分子最大。", [message])
    wrong_denominator = relation_issues("P008 的分母最大。", [message])

    assert [item["kind"] for item in wrong_numerator] == ["max_mismatch"]
    assert wrong_numerator[0]["field"] == "numerator"
    assert wrong_numerator[0]["claimed"] == "500"
    assert wrong_numerator[0]["actual"] == "900"
    assert [item["kind"] for item in wrong_denominator] == ["max_mismatch"]
    assert wrong_denominator[0]["field"] == "denominator"


def test_english_multi_member_superlative_is_validated():
    message = _message(
        [
            {"dimension_value": "P003", "denominator": 18163},
            {"dimension_value": "P009", "denominator": 18163},
            {"dimension_value": "P008", "denominator": 14779},
        ],
        label="sales",
    )

    assert relation_issues("P003 and P009 have the highest denominator.", [message]) == []

    issues = relation_issues("P003 and P008 have the highest denominator.", [message])

    assert [item["kind"] for item in issues] == ["max_mismatch"]
    assert issues[0]["member"] == "P008"


def test_metric_wording_is_not_treated_as_a_member():
    message = _message(_multi_rows(), label="销售额")

    assert relation_issues("销售额的分母最高的是 P003。", [message]) == []


def test_same_sentence_max_and_min_do_not_bleed_into_one_claim():
    message = _message(_multi_rows(), label="销售额")

    assert relation_issues("分母最高的是 P003（18163），最低的是 P009（9000）。", [message]) == []

    issues = relation_issues("分母最高的是 P003，最低的是 P008。", [message])

    assert [item["kind"] for item in issues] == ["min_mismatch"]
    assert issues[0]["member"] == "P008"


def test_parenthetical_contrast_does_not_create_a_second_claim():
    message = _message(_multi_rows(), label="销售额")

    issues = relation_issues("P003 的分母最高（比最低的 P009 高 9000）。", [message])

    assert issues == []


def test_percentage_and_rounding_of_a_returned_rate_are_sourced():
    state = _state(
        [],
        evidence="人力成本：2025 64508794 vs 2024 51305820，变化率 0.257339。",
    )

    assert AgentRuntime._unverified_numbers("变化率 0.257339，即同比增长 25.73%。", state) == []
    assert AgentRuntime._unverified_numbers("变化率约为 0.26。", state) == []
    assert AgentRuntime._unverified_numbers("同比增长 88.88%。", state) == ["88.88"]


def test_contribution_claim_compares_the_contribution_field():
    message = _message(
        [
            {
                "dimension_value": "EC",
                "current_value": 73380575,
                "baseline_value": 87174826,
                "contribution": -13794250,
                "absolute_change": -13794250,
            },
            {
                "dimension_value": "SC",
                "current_value": 78249814,
                "baseline_value": 68519596,
                "contribution": 9730218,
                "absolute_change": 9730218,
            },
            {
                "dimension_value": "NC",
                "current_value": 81581989,
                "baseline_value": 71968467,
                "contribution": 9613522,
                "absolute_change": 9613522,
            },
        ],
        tool="contribution",
        label="销售额",
        arguments={"metric": "sales_amount", "dimension": "region"},
    )

    assert relation_issues("SC 贡献最大，为 9730218.74。", [message]) == []


def test_top_n_phrasing_is_not_a_single_member_minimum_claim():
    message = _message(
        [
            {"rank": 1, "dimension_value": "C006", "value": 0.572464},
            {"rank": 2, "dimension_value": "C007", "value": 1.734261},
            {"rank": 3, "dimension_value": "C005", "value": 4.971426},
        ],
        label="营销 ROI",
        arguments={
            "metric": "ROI",
            "dimension": "campaign",
            "n": 3,
            "ascending": True,
        },
    )

    answer = "最低的三个活动（升序排名）为：C006、C007、C005。其中 C007 和 C005 在返回结果中仍为相对最低的三名。"

    assert relation_issues(answer, [message]) == []


def test_year_is_not_counted_as_an_unsourced_business_number():
    sources = [Decimal("1234567")]

    assert _unsourced_numbers("2025年第四季度销售额为 1234567。", sources) == []
    assert _unsourced_numbers("2025年多出 8888888。", sources) == ["8888888"]


def test_sum_of_three_returned_values_is_sourced():
    state = _state([], evidence="A 9730218.74；B 9613522.50；C 8286191.98。")

    assert AgentRuntime._unverified_numbers("合计 27629933.22。", state) == []


def test_named_amount_and_quantity_claims_use_numerator_and_denominator():
    message = _message(
        [
            {"dimension_value": "SUP-A", "value": 837.978624, "numerator": 7465551.56, "denominator": 8909},
            {"dimension_value": "SUP-B", "value": 1306.125183, "numerator": 8570793.45, "denominator": 6562},
            {"dimension_value": "SUP-C", "value": 382.503143, "numerator": 9201878.10, "denominator": 24057},
            {"dimension_value": "SUP-D", "value": 493.971782, "numerator": 11933370.30, "denominator": 24158},
        ],
        tool="query_metric",
        label="单位采购成本",
    )

    answer = "SUP-D 采购量（24158）和采购金额（11933370.30）均为最大。"

    assert relation_issues(answer, [message]) == []


def test_number_accuracy_reads_a_later_numbered_list_item():
    answer = (
        "**结论：2025年第四季度采购金额最高的四家供应商为：**\n\n"
        "1. **SUP-B（远东供应链）**：73895938.10 元，占比 0.341404\n"
        "2. **SUP-A（华信制造）**：59660375.68 元，占比 0.275635\n"
    )

    assert _number_matches(answer, "73895938.10", terms=["采购金额"])
    assert _number_matches(answer, "59660375.68", terms=["采购金额"])


def test_number_accuracy_reads_a_bullet_under_a_bold_heading():
    answer = (
        "**2025年12月营收同比2024年12月变化情况：**\n\n- 2025年12月营收：79916777.37 元\n- 同比变化率：**0.012393**\n"
    )

    assert _number_matches(answer, "0.012393", terms=["营收"])


def test_difference_of_returned_values_is_sourced_when_the_smaller_comes_first():
    sources = [Decimal("1114848"), Decimal("6940828")]

    assert _unsourced_numbers("两者相差 5825980。", sources) == []


def test_unverified_difference_uses_the_positive_gap_regardless_of_order():
    state = _state([], evidence="HR 1114848；SALES 6940828。")

    assert AgentRuntime._unverified_numbers("两者相差 5825980。", state) == []


def test_negative_rate_can_be_restated_as_a_positive_percentage():
    sources = [Decimal("23401752.76"), Decimal("31064102.41"), Decimal("-0.246663")]

    assert _unsourced_numbers("同比下降 24.6663%。", sources) == []


# --------------------------------------------------------------------------
# 7.9 follow-up: remaining real-output false positives
# --------------------------------------------------------------------------


def test_rate_bullet_after_metric_bullets_is_attributed():
    answer = (
        "**结论：2025年12月毛利同比下降。**\n\n"
        "- 2025年12月毛利：23401752.760000 元\n"
        "- 2024年12月毛利：31064102.410000 元\n"
        "- 同比变化率：**-0.246663**（即同比下降 24.6663%）\n"
    )

    assert _number_matches(answer, "-0.246663", terms=["毛利"])


def test_contribution_column_under_a_sales_table_is_attributed():
    answer = (
        "| 区域 | Q3 销售额 | Q4 销售额 | 贡献变化 | 方向 |\n"
        "|---|---|---|---|---|\n"
        "| 华东 (EC) | 87174826.07 | 73380575.21 | **-13794250.86** | 下降 |\n"
    )

    assert _number_matches(answer, "-13794250.86", terms=["销售额"])


def test_change_column_under_a_profit_table_is_attributed():
    answer = (
        "| 渠道 | Q3 毛利 | Q4 毛利 | 变化额 | 贡献占比 |\n"
        "|---|---|---|---|---|\n"
        "| 电商 ECOM | 33930499.05 | 28635710.18 | -5294788.87 | 0.342567 |\n"
    )

    assert _number_matches(answer, "-5294788.87", terms=["毛利"])


def test_swapped_metric_columns_are_still_rejected():
    answer = "| 区域 | 销售额 | 毛利 |\n|---|---|---|\n| EC | 100 | 200 |\n"

    assert not _number_matches(answer, "200", terms=["销售额"])


def test_integer_percentage_of_a_derived_ratio_is_sourced():
    sources = [Decimal("18098380"), Decimal("300394683.4")]

    assert _unsourced_numbers("人力成本占营收约 6%。", sources) == []


def test_unverified_number_accepts_the_same_value_with_trailing_zeros():
    state = _state([], evidence="销售额 286559001.330000。")

    assert AgentRuntime._unverified_numbers("销售额 286559001.33。", state) == []


def test_unverified_number_accepts_a_sum_of_four_returned_values():
    state = _state([], evidence="A 10000；B 20000；C 30000；D 40000。")

    assert AgentRuntime._unverified_numbers("合计 100000。", state) == []


def test_two_top_n_datasets_do_not_cross_contaminate_relations():
    margin = _message(
        [
            {"rank": 1, "dimension_value": "EC", "dimension_label": "华东", "value": 0.287860},
            {"rank": 2, "dimension_value": "WC", "dimension_label": "西部", "value": 0.294995},
            {"rank": 3, "dimension_value": "SC", "dimension_label": "华南", "value": 0.295097},
            {"rank": 4, "dimension_value": "NC", "dimension_label": "华北", "value": 0.295126},
        ],
        metric="gross_margin",
        label="毛利率",
        arguments={"metric": "gross_margin", "ascending": True, "n": 4},
    )
    stockout = _message(
        [
            {"rank": 1, "dimension_value": "EC", "dimension_label": "华东", "value": 0.283026},
            {"rank": 2, "dimension_value": "SC", "dimension_label": "华南", "value": 0.031813},
            {"rank": 3, "dimension_value": "NC", "dimension_label": "华北", "value": 0.031807},
            {"rank": 4, "dimension_value": "WC", "dimension_label": "西部", "value": 0.031772},
        ],
        metric="stockout_rate",
        label="缺货率",
        arguments={"metric": "stockout_rate", "ascending": False, "n": 4},
    )
    answer = (
        "**毛利率排名（最低→最高）**：\n"
        "- EC 华东 = 0.287860（最低）\n"
        "- WC 西部 = 0.294995\n"
        "- SC 华南 = 0.295097\n"
        "- NC 华北 = 0.295126\n\n"
        "**缺货率排名（最高→最低）**：\n"
        "- EC 华东 = 0.283026（最高）\n"
        "- SC 华南 = 0.031813\n"
        "- NC 华北 = 0.031807\n"
        "- WC 西部 = 0.031772\n\n"
        "华南缺货率第二高，西部第四。"
    )

    assert relation_issues(answer, [margin, stockout]) == []


def test_investment_claim_uses_the_denominator_field():
    message = _message(
        [
            {"rank": 1, "dimension_value": "C006", "value": 0.572464, "denominator": 1361202.53},
            {"rank": 2, "dimension_value": "C007", "value": 1.734261, "denominator": 659668.17},
            {"rank": 3, "dimension_value": "C005", "value": 4.971426, "denominator": 699861.41},
        ],
        metric="ROI",
        label="营销 ROI",
        arguments={"metric": "ROI", "ascending": True, "n": 3},
    )

    assert (
        relation_issues(
            "C006（品牌曝光 Q4）ROI 仅 0.572464，其投入 1361202.53 为三个活动中最高。",
            [message],
        )
        == []
    )


def test_largest_drag_compares_the_most_negative_contribution():
    message = _message(
        [
            {"dimension_value": "ECOM", "current_value": 28635710.18, "contribution": -5294788.87},
            {"dimension_value": "DIST", "current_value": 22039684.00, "contribution": -3782976.97},
            {"dimension_value": "DIRECT", "current_value": 19450674.93, "contribution": -3327548.12},
            {"dimension_value": "RETAIL", "current_value": 17983909.01, "contribution": -3050901.09},
        ],
        tool="contribution",
        metric="gross_profit",
        label="毛利",
        arguments={"metric": "gross_profit", "dimension": "channel"},
    )

    assert relation_issues("电商渠道是最大拖累，贡献了 -5294788.87。", [message]) == []


def test_truncated_result_rejects_an_absence_claim_about_unshown_rows():
    message = _message(
        [{"name": f"metric_{index}", "label": f"指标 {index}"} for index in range(20)],
        tool="list_metrics",
        trusted=False,
        row_count=34,
        truncated=True,
        metric=None,
        label=None,
    )

    issues = relation_issues("其余 30 个指标中均无其他利润口径。", [message])

    assert [item["kind"] for item in issues] == ["truncated_absence_overreach"]
    assert issues[0]["truncated"] is True


def test_displayed_row_count_statement_is_not_an_absence_claim():
    message = _message(
        [{"name": f"metric_{index}", "label": f"指标 {index}"} for index in range(20)],
        tool="list_metrics",
        trusted=False,
        row_count=34,
        truncated=True,
        metric=None,
        label=None,
    )

    assert relation_issues("其余 14 个指标未展示。", [message]) == []


# --------------------------------------------------------------------------
# Phase 9.1-Final: relation-check false positives found on real GLM answers
# --------------------------------------------------------------------------


def _two_metric_messages() -> list[ModelToolMessage]:
    margin = _message(
        [
            {"dimension_value": "EC", "value": 0.287860},
            {"dimension_value": "WC", "value": 0.294995},
            {"dimension_value": "SC", "value": 0.295097},
            {"dimension_value": "NC", "value": 0.295126},
        ],
        metric="gross_margin",
        label="毛利率",
        arguments={"metric": "gross_margin", "dimension": "region", "n": 4, "ascending": True},
    )
    stockout = _message(
        [
            {"dimension_value": "EC", "value": 0.283026},
            {"dimension_value": "SC", "value": 0.031818},
            {"dimension_value": "NC", "value": 0.031800},
            {"dimension_value": "WC", "value": 0.031772},
        ],
        metric="stockout_rate",
        label="缺货率",
    )
    return [margin, stockout]


def test_metric_proximity_stays_with_the_metric_the_marker_qualifies():
    messages = _two_metric_messages()

    answer = "EC 毛利率最低且缺货率最高（0.283026 与 0.0318 量级相差近 9 倍）。"

    assert relation_issues(answer, messages) == []


def test_parenthetical_member_list_does_not_replace_the_subject():
    message = _message(
        [
            {"dimension_value": "P002", "value": 1822.871524},
            {"dimension_value": "P003", "value": 287.020081},
        ],
        tool="query_metric",
        arguments={"metric": "sales_unit_price", "dimension": "product", "n": 2},
        metric="sales_unit_price",
        label="平均销售单价",
    )

    answer = "无线键鼠套装（P002 之外最低的 P003）287.020081 为最低值。"

    assert relation_issues(answer, [message]) == []


def test_comparison_phrase_does_not_select_the_supporting_column():
    message = _message(
        [
            {
                "dimension_value": "C006",
                "value": 0.446114,
                "numerator": 229908.63,
                "denominator": 515358.42,
            },
            {
                "dimension_value": "C007",
                "value": 1.737348,
                "numerator": 381099.40,
                "denominator": 219356.98,
            },
        ],
        metric="ROI",
        label="营销 ROI",
        arguments={"metric": "ROI", "dimension": "campaign", "n": 2, "ascending": True},
    )

    answer = "C006 归因收入 229908.63，投放 515358.42，ROI = 0.446114，收入低于投入，为两个活动中最低。"

    assert relation_issues(answer, [message]) == []


def test_rank_range_and_rank_pair_comparison_are_not_rank_claims():
    message = _message(
        [
            {"dimension_value": "SALES", "value": 0.12, "rank": 1},
            {"dimension_value": "HR", "value": 0.0, "rank": 2},
            {"dimension_value": "FIN", "value": 0.0, "rank": 3},
            {"dimension_value": "OPS", "value": 0.0, "rank": 4},
            {"dimension_value": "IT", "value": 0.0, "rank": 5},
        ],
        metric="turnover_rate",
        label="离职率",
    )

    assert (
        relation_issues(
            "SALES 为第 1 名，第 2 至第 5 名的离职率数值相等（HR、FIN、OPS、IT 均为 0）。",
            [message],
        )
        == []
    )

    margin = _message(
        [
            {"dimension_value": "NC", "value": 0.295126, "rank": 1},
            {"dimension_value": "SC", "value": 0.295097, "rank": 2},
            {"dimension_value": "WC", "value": 0.294995, "rank": 3},
            {"dimension_value": "EC", "value": 0.287860, "rank": 4},
        ],
        metric="gross_margin",
        label="毛利率",
    )

    assert relation_issues("NC（第一名）与 EC（第四名）相差 0.007266。", [margin]) == []


def test_ranking_idiom_uses_the_preceding_subject_and_subset_scope():
    message = _message(
        [
            {"dimension_value": "EC", "value": 0.716974, "rank": 1},
            {"dimension_value": "SC", "value": 0.968187, "rank": 2},
            {"dimension_value": "NC", "value": 0.968193, "rank": 3},
            {"dimension_value": "WC", "value": 0.968228, "rank": 4},
        ],
        metric="fulfillment_rate",
        label="需求满足率",
        arguments={"metric": "fulfillment_rate", "dimension": "region", "n": 4, "ascending": True},
    )

    answer = "SC、NC、WC 三个区域非常接近，其中 SC 华南数值最小，按排名列第二，但与 NC、WC 差异极小。"

    assert relation_issues(answer, [message]) == []


def test_unranked_member_list_is_not_an_order_claim():
    message = _message(
        [{"dimension_value": f"P{index:03d}", "value": 1000 - index, "rank": index} for index in range(1, 9)],
        metric="sales_amount",
        label="销售额",
    )

    answer = "完整名单为 P003、P001、P008，其中没有 P999。"

    assert relation_issues(answer, [message]) == []


def test_largest_decline_contribution_is_the_most_negative_row():
    message = _message(
        [
            {"dimension_value": "NC", "contribution": -12159846.56},
            {"dimension_value": "SC", "contribution": -11706262.12},
            {"dimension_value": "EC", "contribution": -10419386.31},
            {"dimension_value": "WC", "contribution": -10385249.43},
        ],
        tool="contribution",
        metric="sales_amount",
        label="销售额",
    )

    assert relation_issues("其中 NC 贡献最大。", [message]) == []


def test_rank_and_superlative_guards_do_not_disable_real_mismatches():
    message = _message(
        [
            {"dimension_value": "NC", "value": 0.295126, "rank": 1},
            {"dimension_value": "SC", "value": 0.295097, "rank": 2},
            {"dimension_value": "WC", "value": 0.294995, "rank": 3},
            {"dimension_value": "EC", "value": 0.287860, "rank": 4},
        ],
        metric="gross_margin",
        label="毛利率",
    )

    assert relation_issues("EC 排名第 1。", [message])[0]["kind"] == "rank_mismatch"
    assert relation_issues("EC 的毛利率最高。", [message])[0]["kind"] == "max_mismatch"


def test_subset_scope_does_not_hide_a_wrong_subset_member():
    message = _message(
        [
            {"dimension_value": "EC", "value": 0.716974},
            {"dimension_value": "SC", "value": 0.968187},
            {"dimension_value": "NC", "value": 0.968193},
            {"dimension_value": "WC", "value": 0.968228},
        ],
        metric="fulfillment_rate",
        label="需求满足率",
        arguments={"metric": "fulfillment_rate", "dimension": "region", "n": 4, "ascending": True},
    )

    issues = relation_issues("SC、NC、WC 三个区域非常接近，其中 NC 数值最小。", [message])

    assert [item["kind"] for item in issues] == ["min_mismatch"]
    assert issues[0]["member"] == "NC"


def test_all_negative_contribution_guard_does_not_hide_a_wrong_member():
    message = _message(
        [
            {"dimension_value": "NC", "contribution": -12159846.56},
            {"dimension_value": "SC", "contribution": -11706262.12},
            {"dimension_value": "EC", "contribution": -10419386.31},
            {"dimension_value": "WC", "contribution": -10385249.43},
        ],
        tool="contribution",
        metric="sales_amount",
        label="销售额",
    )

    assert relation_issues("其中 EC 贡献最大。", [message])[0]["kind"] == "min_mismatch"

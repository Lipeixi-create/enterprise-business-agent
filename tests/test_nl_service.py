from __future__ import annotations

from datetime import date

import pytest

from enterprise_business_agent.analysis.contracts import AnalysisStatus
from enterprise_business_agent.config import Settings
from enterprise_business_agent.llm import LLMCallResult, LLMTimeout, LLMUnavailable
from enterprise_business_agent.nl import NaturalLanguageAnalyzer, NaturalLanguageRequest, ResultType


class FakeLLM:
    model = "fake-model"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[str] = []

    def complete_json(self, *, system: str, prompt: str) -> LLMCallResult:
        self.calls.append(prompt)
        if not self.responses:
            raise AssertionError("unexpected LLM call")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMCallResult(data=item, model=self.model, latency_ms=1.0)


@pytest.fixture()
def settings() -> Settings:
    return Settings(data_start=date(2024, 1, 1), data_months=24)


def _analyze(session, settings, question, *, llm=None, **kwargs):
    analyzer = NaturalLanguageAnalyzer(session, settings, llm_client=llm)
    return analyzer.analyze(NaturalLanguageRequest(question=question, as_of=date(2025, 4, 1), **kwargs))


def test_semantic_query_end_to_end(fixed_dataset, settings):
    response = _analyze(fixed_dataset, settings, "2025年第一季度销售额")
    assert response.status == AnalysisStatus.OK
    assert response.result_type == ResultType.METRIC_QUERY
    assert response.metric == "sales_amount"
    assert response.result.points
    assert response.explanation
    assert response.data_sources == ["sales_fact"]


def test_region_filter_is_applied(fixed_dataset, settings):
    response = _analyze(fixed_dataset, settings, "华东销售额环比变化")
    assert response.status == AnalysisStatus.OK
    item = response.result.items[0]
    assert item.current_value == 1000
    assert item.baseline_value == 1400
    assert item.change_rate < 0


def test_ambiguous_metric_returns_candidates(fixed_dataset, settings):
    response = _analyze(fixed_dataset, settings, "本季度利润变化")
    assert response.status == AnalysisStatus.AMBIGUOUS
    assert {item.name for item in response.candidates} == {"gross_profit", "net_profit"}
    assert response.error.code == "ambiguous_metric"


def test_unknown_metric_is_unsupported(fixed_dataset, settings):
    response = _analyze(fixed_dataset, settings, "客户满意度是多少")
    assert response.status == AnalysisStatus.UNSUPPORTED
    assert response.error.code == "unsupported_question"
    assert response.result is None


def test_forecast_question_is_rejected_without_llm_call(fixed_dataset, settings):
    llm = FakeLLM({"operation": "query", "metric": "sales_amount"})
    response = _analyze(fixed_dataset, settings, "请预测下个月的销售额", llm=llm, allow_sql_fallback=True)
    assert response.status == AnalysisStatus.UNSUPPORTED
    assert response.error.code == "unsupported_question"
    assert response.result is None
    assert llm.calls == []


def test_missing_period_returns_insufficient_data(fixed_dataset, settings):
    response = _analyze(fixed_dataset, settings, "2025年12月华东销售额")
    assert response.status == AnalysisStatus.INSUFFICIENT_DATA
    assert response.error.code == "insufficient_data"


def test_investigation_reports_observations_separately_from_inference(fixed_dataset, settings):
    response = _analyze(fixed_dataset, settings, "华东销售下降是否与核心商品缺货有关")
    assert response.result_type == ResultType.INVESTIGATION
    assert response.result.inference_type == "observational"
    assert response.result.observations
    assert response.result.inference
    assert any("不代表真实商业世界的因果关系" in item for item in response.result.disclaimers)
    assert response.result.observations[0].name == "east_sales"


def test_llm_plan_is_used_when_available(fixed_dataset, settings):
    llm = FakeLLM(
        {
            "operation": "query",
            "metric": "sales_amount",
            "start": "2025-01-01",
            "end": "2025-01-01",
            "dimensions": ["region"],
        }
    )
    response = _analyze(fixed_dataset, settings, "某个自由表述的问题", llm=llm)
    assert response.planner == "llm"
    assert response.planner_model == "fake-model"
    assert response.status == AnalysisStatus.OK
    assert len(llm.calls) == 1


def test_llm_timeout_on_unresolvable_question_returns_timeout_status(fixed_dataset, settings):
    llm = FakeLLM(LLMTimeout("timed out"))
    response = _analyze(fixed_dataset, settings, "客户满意度是多少", llm=llm)
    assert response.status == AnalysisStatus.LLM_TIMEOUT
    assert response.error.code == "llm_timeout"
    assert response.result is None


def test_llm_unavailable_with_rule_fallback_is_marked_as_rule(fixed_dataset, settings):
    llm = FakeLLM(LLMUnavailable("down"))
    response = _analyze(fixed_dataset, settings, "华东销售额环比变化", llm=llm)
    assert response.status == AnalysisStatus.OK
    assert response.planner == "rule"
    assert "llm_unavailable_using_rule_planner" in response.warnings


def test_semantic_path_wins_and_sql_fallback_is_not_called(fixed_dataset, settings):
    llm = FakeLLM(
        {
            "operation": "query",
            "metric": "sales_amount",
            "start": "2025-01-01",
            "end": "2025-01-01",
            "dimensions": ["region"],
        }
    )
    response = _analyze(
        fixed_dataset,
        settings,
        "某个自由表述的问题",
        llm=llm,
        allow_sql_fallback=True,
    )
    assert response.planner == "llm"
    assert response.result_type == ResultType.METRIC_QUERY
    assert len(llm.calls) == 1


def test_guarded_sql_fallback_runs_for_unmapped_question(fixed_dataset, settings):
    llm = FakeLLM(
        {"operation": "query", "metric": "refund_amount"},
        {
            "sql": "SELECT region_id, SUM(sales_amount) AS total FROM sales_fact GROUP BY region_id LIMIT 10",
            "params": {},
        },
    )
    response = _analyze(
        fixed_dataset,
        settings,
        "按渠道统计退款金额",
        llm=llm,
        allow_sql_fallback=True,
    )
    assert response.status == AnalysisStatus.OK
    assert response.result_type == ResultType.SQL_QUERY
    assert response.result.row_count == 2
    assert response.data_sources == ["sales_fact"]
    assert response.sql_hash


def test_sql_fallback_rejects_dml_and_reports_guard_rejected(fixed_dataset, settings):
    llm = FakeLLM(
        {"operation": "query", "metric": "refund_amount"},
        {"sql": "DELETE FROM sales_fact", "params": {}},
        {"sql": "DELETE FROM sales_fact", "params": {}},
    )
    response = _analyze(
        fixed_dataset,
        settings,
        "按渠道统计退款金额",
        llm=llm,
        allow_sql_fallback=True,
    )
    assert response.status == AnalysisStatus.GUARD_REJECTED
    assert response.error.code == "forbidden_statement"
    assert response.result is None


def test_sql_fallback_timeout_reports_llm_timeout(fixed_dataset, settings):
    llm = FakeLLM(LLMTimeout("timed out"), LLMTimeout("timed out"))
    response = _analyze(
        fixed_dataset,
        settings,
        "按渠道统计退款金额",
        llm=llm,
        allow_sql_fallback=True,
    )
    assert response.status == AnalysisStatus.LLM_TIMEOUT
    assert response.error.code == "llm_timeout"


def test_sql_fallback_is_not_used_without_explicit_opt_in(fixed_dataset, settings):
    llm = FakeLLM({"operation": "query", "metric": "refund_amount"})
    response = _analyze(fixed_dataset, settings, "按渠道统计退款金额", llm=llm)
    assert response.status == AnalysisStatus.UNSUPPORTED
    assert len(llm.calls) == 1
    assert response.result is None


def test_sql_fallback_empty_result_is_insufficient_data(fixed_dataset, settings):
    llm = FakeLLM(
        {"operation": "query", "metric": "refund_amount"},
        {
            "sql": "SELECT region_id FROM sales_fact WHERE region_id = :rid LIMIT 10",
            "params": {"rid": 999},
        },
    )
    response = _analyze(
        fixed_dataset,
        settings,
        "按渠道统计退款金额",
        llm=llm,
        allow_sql_fallback=True,
    )
    assert response.status == AnalysisStatus.INSUFFICIENT_DATA
    assert response.result.row_count == 0
    assert response.result.rows == []


def test_sql_fallback_rejects_literal_filter_values(fixed_dataset, settings):
    llm = FakeLLM(
        {"operation": "query", "metric": "refund_amount"},
        {"sql": "SELECT region_id FROM sales_fact WHERE region_id = 1", "params": {}},
        {"sql": "SELECT region_id FROM sales_fact WHERE region_id = 1", "params": {}},
    )
    response = _analyze(
        fixed_dataset,
        settings,
        "按渠道统计退款金额",
        llm=llm,
        allow_sql_fallback=True,
    )
    assert response.status == AnalysisStatus.GUARD_REJECTED
    assert response.error.code == "unbound_value"


def test_sql_fallback_prompt_has_no_database_credentials(fixed_dataset, settings):
    llm = FakeLLM(
        {"operation": "query", "metric": "refund_amount"},
        {"sql": "SELECT region_id FROM sales_fact LIMIT 1", "params": {}},
    )
    _analyze(
        fixed_dataset,
        settings,
        "按渠道统计退款金额",
        llm=llm,
        allow_sql_fallback=True,
    )
    joined = "\n".join(llm.calls)
    assert settings.application_database_url not in joined
    assert "sqlite:///" not in joined


def test_sql_fallback_requires_llm(fixed_dataset, settings):
    response = _analyze(
        fixed_dataset,
        settings,
        "按渠道统计退款金额",
        allow_sql_fallback=True,
    )
    assert response.status == AnalysisStatus.LLM_UNAVAILABLE
    assert response.error.code == "llm_unavailable"

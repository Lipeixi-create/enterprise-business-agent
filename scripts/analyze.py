from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session

from enterprise_business_agent.analysis.contracts import (
    AnomalyRequest,
    ContributionRequest,
    DimensionFilter,
    MetricQueryRequest,
    PeriodComparisonRequest,
    TopNRequest,
    TrendRequest,
)
from enterprise_business_agent.analysis.engine import (
    analyze_contribution,
    analyze_trend,
    compare_periods,
    detect_anomalies,
    get_top_n,
    query_metric,
)
from enterprise_business_agent.config import get_settings
from enterprise_business_agent.db import make_engine


def _parse_filter(raw: str) -> DimensionFilter:
    if "!=" in raw:
        dimension, value = raw.split("!=", 1)
        operator = "ne"
    elif "=" in raw:
        dimension, value = raw.split("=", 1)
        operator = "eq"
    else:
        raise argparse.ArgumentTypeError(f"filter must look like dimension=value: {raw}")
    dimension = dimension.strip()
    if not dimension:
        raise argparse.ArgumentTypeError(f"filter dimension is empty: {raw}")
    if "," in value:
        return DimensionFilter(
            dimension=dimension,
            operator="in",
            value=[item.strip() for item in value.split(",") if item.strip()],
        )
    return DimensionFilter(dimension=dimension, operator=operator, value=value.strip())


def _dump(payload: BaseModel) -> None:
    print(json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, indent=2))


def _query(session: Session, args: argparse.Namespace) -> None:
    _dump(
        query_metric(
            session,
            MetricQueryRequest(
                metric=args.metric,
                start=args.start,
                end=args.end,
                grain=args.grain,
                dimensions=args.dimension or [],
                filters=args.filter or [],
                aggregation=args.aggregation,
            ),
        )
    )


def _compare(session: Session, args: argparse.Namespace) -> None:
    _dump(
        compare_periods(
            session,
            PeriodComparisonRequest(
                metric=args.metric,
                kind=args.kind,
                period=args.period,
                year=args.year,
                quarter=args.quarter,
                dimensions=args.dimension or [],
                filters=args.filter or [],
            ),
        )
    )


def _top(session: Session, args: argparse.Namespace) -> None:
    _dump(
        get_top_n(
            session,
            TopNRequest(
                metric=args.metric,
                dimension=args.dimension,
                start=args.start,
                end=args.end,
                n=args.n,
                filters=args.filter or [],
                ascending=args.ascending,
            ),
        )
    )


def _contribution(session: Session, args: argparse.Namespace) -> None:
    _dump(
        analyze_contribution(
            session,
            ContributionRequest(
                metric=args.metric,
                dimension=args.dimension,
                current_start=args.current_start,
                current_end=args.current_end,
                baseline_start=args.baseline_start,
                baseline_end=args.baseline_end,
                filters=args.filter or [],
                top_n=args.top_n,
            ),
        )
    )


def _trend(session: Session, args: argparse.Namespace) -> None:
    _dump(
        analyze_trend(
            session,
            TrendRequest(
                metric=args.metric,
                start=args.start,
                end=args.end,
                grain=args.grain,
                filters=args.filter or [],
            ),
        )
    )


def _anomaly(session: Session, args: argparse.Namespace) -> None:
    _dump(
        detect_anomalies(
            session,
            AnomalyRequest(
                metric=args.metric,
                start=args.start,
                end=args.end,
                method=args.method,
                threshold=args.threshold,
                z_threshold=args.z_threshold,
                min_history=args.min_history,
                filters=args.filter or [],
            ),
        )
    )


DEMOS: dict[str, tuple[str, Callable[[Session], BaseModel]]] = {}


def _demo_profit(session: Session) -> BaseModel:
    return compare_periods(
        session,
        PeriodComparisonRequest(metric="净利润", kind="qoq", year=2025, quarter=4),
    )


def _demo_east_sales(session: Session) -> BaseModel:
    return compare_periods(
        session,
        PeriodComparisonRequest(
            metric="销售收入",
            kind="mom",
            period="2025-12",
            filters=[DimensionFilter(dimension="region", operator="eq", value="EC")],
        ),
    )


def _demo_roi(session: Session) -> BaseModel:
    return get_top_n(
        session,
        TopNRequest(metric="ROI", dimension="campaign", start="2025-01", end="2025-12", n=5),
    )


def _demo_inventory(session: Session) -> BaseModel:
    return get_top_n(
        session,
        TopNRequest(
            metric="缺货率",
            dimension="product",
            start="2025-10",
            end="2025-12",
            n=5,
        ),
    )


def _demo_labor(session: Session) -> BaseModel:
    return analyze_contribution(
        session,
        ContributionRequest(
            metric="人工成本",
            dimension="department",
            current_start="2025-01",
            current_end="2025-12",
            baseline_start="2024-01",
            baseline_end="2024-12",
        ),
    )


DEMOS.update(
    {
        "profit": ("本季度利润变化（2025Q4 净利润环比）", _demo_profit),
        "east-sales": ("华东销售环比变化（2025-12 vs 2025-11）", _demo_east_sales),
        "roi": ("营销活动 ROI Top 5（2025 全年）", _demo_roi),
        "inventory": ("商品库存风险排名（2025Q4 缺货率 Top 5）", _demo_inventory),
        "labor": ("部门人工成本变化（2025 vs 2024 贡献度）", _demo_labor),
    }
)


def _demo(session: Session, args: argparse.Namespace) -> None:
    names = list(DEMOS) if args.name == "all" else [args.name]
    payload: list[dict[str, Any]] = []
    for name in names:
        title, runner = DEMOS[name]
        payload.append({"name": name, "title": title, "result": runner(session).model_dump(mode="json")})
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic business analysis CLI (Phase 2)")
    parser.add_argument("--database-url", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    p_query = sub.add_parser("query", help="query one metric by time and dimensions")
    p_query.add_argument("--metric", required=True)
    p_query.add_argument("--start", required=True)
    p_query.add_argument("--end", required=True)
    p_query.add_argument("--grain", default="month", choices=["month", "quarter", "year", "total"])
    p_query.add_argument("--dimension", action="append")
    p_query.add_argument("--filter", action="append", type=_parse_filter)
    p_query.add_argument("--aggregation", default=None, choices=["sum", "avg", "last", "count", "ratio"])

    p_compare = sub.add_parser("compare", help="mom/yoy/qoq comparison")
    p_compare.add_argument("--metric", required=True)
    p_compare.add_argument("--kind", required=True, choices=["mom", "yoy", "qoq", "yoy_quarter"])
    p_compare.add_argument("--period", default=None)
    p_compare.add_argument("--year", type=int, default=None)
    p_compare.add_argument("--quarter", type=int, default=None, choices=[1, 2, 3, 4])
    p_compare.add_argument("--dimension", action="append")
    p_compare.add_argument("--filter", action="append", type=_parse_filter)

    p_top = sub.add_parser("top", help="Top N ranking")
    p_top.add_argument("--metric", required=True)
    p_top.add_argument("--dimension", required=True)
    p_top.add_argument("--start", required=True)
    p_top.add_argument("--end", required=True)
    p_top.add_argument("--n", type=int, default=5)
    p_top.add_argument("--ascending", action="store_true")
    p_top.add_argument("--filter", action="append", type=_parse_filter)

    p_contribution = sub.add_parser("contribution", help="contribution analysis")
    p_contribution.add_argument("--metric", required=True)
    p_contribution.add_argument("--dimension", required=True)
    p_contribution.add_argument("--current-start", required=True)
    p_contribution.add_argument("--current-end", required=True)
    p_contribution.add_argument("--baseline-start", required=True)
    p_contribution.add_argument("--baseline-end", required=True)
    p_contribution.add_argument("--top-n", type=int, default=None)
    p_contribution.add_argument("--filter", action="append", type=_parse_filter)

    p_trend = sub.add_parser("trend", help="trend direction and step changes")
    p_trend.add_argument("--metric", required=True)
    p_trend.add_argument("--start", required=True)
    p_trend.add_argument("--end", required=True)
    p_trend.add_argument("--grain", default="month", choices=["month", "quarter", "year", "total"])
    p_trend.add_argument("--filter", action="append", type=_parse_filter)

    p_anomaly = sub.add_parser("anomaly", help="anomaly detection over time")
    p_anomaly.add_argument("--metric", required=True)
    p_anomaly.add_argument("--start", required=True)
    p_anomaly.add_argument("--end", required=True)
    p_anomaly.add_argument("--method", default="mom_threshold", choices=["mom_threshold", "zscore"])
    p_anomaly.add_argument("--threshold", type=float, default=0.20)
    p_anomaly.add_argument("--z-threshold", type=float, default=2.0)
    p_anomaly.add_argument("--min-history", type=int, default=3)
    p_anomaly.add_argument("--filter", action="append", type=_parse_filter)

    p_demo = sub.add_parser("demo", help="run built-in demos")
    p_demo.add_argument("--name", default="all", choices=["all", *DEMOS.keys()])

    return parser


COMMANDS: dict[str, Callable[[Session, argparse.Namespace], None]] = {
    "query": _query,
    "compare": _compare,
    "top": _top,
    "contribution": _contribution,
    "trend": _trend,
    "anomaly": _anomaly,
    "demo": _demo,
}


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    settings = get_settings(args.database_url)
    with Session(make_engine(settings.database_url)) as session:
        COMMANDS[args.command](session, args)


if __name__ == "__main__":
    main()

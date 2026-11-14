"""Deterministic attribution over existing analysis engine output.

Attribution never recomputes SQL and never asks a model for numbers. It takes
the ContributionResponse already produced by ``analysis.engine`` and adds the
distinction between absolute change, relative change and contribution rate,
plus explicit checks for zero denominators and unallocated residuals.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from .contracts import AnalysisStatus, ContributionResponse


class AttributionItem(BaseModel):
    dimension_value: str
    dimension_label: str | None = None
    current_value: Decimal | None = None
    baseline_value: Decimal | None = None
    absolute_change: Decimal | None = None
    contribution: Decimal | None = None
    contribution_rate: Decimal | None = None
    direction: str


class AttributionResult(BaseModel):
    status: AnalysisStatus
    metric: str
    label: str
    dimension: str
    unit: str
    current_start: str
    current_end: str
    baseline_start: str
    baseline_end: str
    total_current: Decimal | None = None
    total_baseline: Decimal | None = None
    absolute_change: Decimal | None = None
    relative_change: Decimal | None = None
    allocatable: bool = False
    items: list[AttributionItem] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    reason: str | None = None


def build_attribution(result: ContributionResponse) -> AttributionResult:
    """Classify a contribution result without changing any value."""

    relative_change: Decimal | None = None
    missing: list[str] = []
    checks: list[str] = []
    if result.total_baseline in (None, 0, Decimal("0")):
        missing.append("baseline_total")
        checks.append("基准期总量为 0 或缺失，无法计算相对变化率")
    elif result.total_change is not None:
        relative_change = result.total_change / result.total_baseline

    if result.residual not in (None, 0, Decimal("0")):
        tolerance = abs(result.total_change or Decimal("0")) * Decimal("0.01")
        if abs(result.residual) > tolerance:
            checks.append(f"贡献合计与总变化存在残差 {result.residual}，维度成员可能不完整")
        else:
            checks.append("贡献合计与总变化在 1% 容差内一致")

    if not result.allocatable:
        checks.append("该指标不是可加指标，贡献拆解不适用")
        missing.append("additive_metric")

    if not result.items:
        missing.append("contribution_items")

    items = [
        AttributionItem(
            dimension_value=item.dimension_value,
            dimension_label=item.dimension_label,
            current_value=item.current_value,
            baseline_value=item.baseline_value,
            absolute_change=item.absolute_change,
            contribution=item.contribution,
            contribution_rate=item.contribution_rate,
            direction=item.direction,
        )
        for item in result.items
    ]
    items.sort(
        key=lambda item: abs(item.contribution or Decimal("0")),
        reverse=True,
    )
    status = result.status
    reason = result.reason
    if status == AnalysisStatus.OK and not items:
        status = AnalysisStatus.INSUFFICIENT_DATA
        reason = reason or "贡献拆解没有返回可用维度"
    return AttributionResult(
        status=status,
        metric=result.metric,
        label=result.label,
        dimension=result.dimension,
        unit=result.unit,
        current_start=result.current_start.isoformat(),
        current_end=result.current_end.isoformat(),
        baseline_start=result.baseline_start.isoformat(),
        baseline_end=result.baseline_end.isoformat(),
        total_current=result.total_current,
        total_baseline=result.total_baseline,
        absolute_change=result.total_change,
        relative_change=relative_change,
        allocatable=result.allocatable,
        items=items,
        checks=checks,
        missing_data=missing,
        reason=reason,
    )


def attribution_from_evidence(result: dict[str, Any]) -> AttributionResult | None:
    """Build attribution from a stored contribution evidence payload."""

    if not result or result.get("metric") is None:
        return None
    try:
        parsed = ContributionResponse.model_validate(result)
    except ValueError:
        return None
    return build_attribution(parsed)


__all__ = ["AttributionItem", "AttributionResult", "build_attribution", "attribution_from_evidence"]

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .schemas import DimensionDefinition, MetricDefinition
from .semantic import load_dimensions, load_metrics, normalize_name


class ResolutionCandidate(BaseModel):
    name: str
    label: str
    description: str


class MetricResolution(BaseModel):
    query: str
    status: Literal["resolved", "ambiguous", "unsupported"]
    metric_name: str | None = None
    label: str | None = None
    candidates: list[ResolutionCandidate] = Field(default_factory=list)
    message: str | None = None
    metric: MetricDefinition | None = Field(default=None, exclude=True)


class MetricResolver:
    """Resolve canonical metric names, labels and aliases without guessing."""

    def __init__(self, metrics: list[MetricDefinition] | None = None):
        self.metrics = metrics if metrics is not None else load_metrics()
        self._by_name = {normalize_name(item.name): item for item in self.metrics}
        self._by_label = {normalize_name(item.label): item for item in self.metrics}
        aliases: dict[str, list[MetricDefinition]] = {}
        for metric in self.metrics:
            for alias in (metric.name, metric.label, *metric.synonyms):
                aliases.setdefault(normalize_name(alias), []).append(metric)
        self._aliases = aliases

    def get(self, name: str) -> MetricDefinition | None:
        return self._by_name.get(normalize_name(name))

    def resolve(self, query: str) -> MetricResolution:
        normalized = normalize_name(query)
        if not normalized:
            return MetricResolution(query=query, status="unsupported", message="指标名称为空")

        metric = self._by_name.get(normalized)
        if metric is not None:
            return MetricResolution(
                query=query,
                status="resolved",
                metric_name=metric.name,
                label=metric.label,
                metric=metric,
            )

        metric = self._by_label.get(normalized)
        if metric is not None and sum(1 for item in self.metrics if normalize_name(item.label) == normalized) == 1:
            return MetricResolution(
                query=query,
                status="resolved",
                metric_name=metric.name,
                label=metric.label,
                metric=metric,
            )

        candidates = []
        seen: set[str] = set()
        for item in self._aliases.get(normalized, []):
            if item.name not in seen:
                seen.add(item.name)
                candidates.append(item)
        if len(candidates) == 1:
            metric = candidates[0]
            return MetricResolution(
                query=query,
                status="resolved",
                metric_name=metric.name,
                label=metric.label,
                metric=metric,
            )
        if len(candidates) > 1:
            return MetricResolution(
                query=query,
                status="ambiguous",
                candidates=[
                    ResolutionCandidate(name=item.name, label=item.label, description=item.description)
                    for item in sorted(candidates, key=lambda value: value.name)
                ],
                message="指标名称存在歧义，请使用 canonical metric 名称或补充口径",
            )
        return MetricResolution(
            query=query,
            status="unsupported",
            message="指标未在 semantic_layer/metrics.json 中定义，或当前数据表无法计算",
        )


class DimensionRegistry:
    def __init__(self, dimensions: list[DimensionDefinition] | None = None):
        self.dimensions = dimensions if dimensions is not None else load_dimensions()
        self._by_name = {normalize_name(item.name): item for item in self.dimensions}
        self._aliases: dict[str, list[DimensionDefinition]] = {}
        for item in self.dimensions:
            for alias in (item.name, item.label, *item.synonyms):
                self._aliases.setdefault(normalize_name(alias), []).append(item)

    def get(self, name: str) -> DimensionDefinition | None:
        return self._by_name.get(normalize_name(name))

    def resolve(self, name: str) -> DimensionDefinition | None:
        normalized = normalize_name(name)
        exact = self._by_name.get(normalized)
        if exact is not None:
            return exact
        candidates = self._aliases.get(normalized, [])
        return candidates[0] if len(candidates) == 1 else None

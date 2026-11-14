from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import Engine, insert, select
from sqlalchemy.exc import IntegrityError

from ..analysis.attribution import attribution_from_evidence
from .contracts import EvidenceRecord, Finding, FindingSeverity, FindingType
from .schema import FINDING_TABLE, create_schema


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: Any) -> str:
    return str(value) if value is not None else "无"


def _sign(value: Any) -> int:
    try:
        number = Decimal(str(value))
    except Exception:
        return 0
    return (number > 0) - (number < 0)


class FindingEngine:
    """Deterministic findings derived only from persisted evidence.

    Confidence is a fixed rule per finding kind, not a model output. A
    correlation never becomes a causal claim; it is emitted as ``correlation``
    or ``hypothesis`` with explicit limitations.
    """

    def build(self, run_id: str, evidences: Iterable[EvidenceRecord]) -> list[Finding]:
        items = [item for item in evidences if item.status == "valid"]
        findings: list[Finding] = []
        for evidence in items:
            findings.extend(self._from_evidence(run_id, evidence))
        findings.extend(self._cross_domain(run_id, items))
        return findings

    # -- per-evidence --------------------------------------------------
    def _from_evidence(self, run_id: str, evidence: EvidenceRecord) -> list[Finding]:
        if evidence.evidence_type == "comparison":
            return self._comparison(run_id, evidence)
        if evidence.evidence_type == "contribution":
            return self._contribution(run_id, evidence)
        if evidence.evidence_type == "ranking":
            return self._ranking(run_id, evidence)
        if evidence.evidence_type == "anomaly":
            return self._anomaly(run_id, evidence)
        if evidence.evidence_type == "trend":
            return self._trend(run_id, evidence)
        if evidence.evidence_type == "metric_value":
            return self._metric_value(run_id, evidence)
        return []

    def _comparison(self, run_id: str, evidence: EvidenceRecord) -> list[Finding]:
        out = []
        for item in evidence.result.get("items") or []:
            if item.get("current_value") is None or item.get("baseline_value") is None:
                continue
            change_rate = item.get("change_rate")
            statement = (
                f"{evidence.result.get('label') or evidence.metric_code} "
                f"{item.get('current_period')} 为 {_text(item.get('current_value'))}，"
                f"基准 {item.get('baseline_period')} 为 {_text(item.get('baseline_value'))}，"
                f"绝对变化 {_text(item.get('absolute_change'))}，"
                f"变化率 {_text(change_rate)}。"
            )
            severity: FindingSeverity = (
                "primary"
                if change_rate is not None and abs(Decimal(str(change_rate))) >= Decimal("0.1")
                else "secondary"
            )
            out.append(
                self._make(
                    run_id,
                    evidence,
                    title=f"{evidence.result.get('label') or evidence.metric_code} 变化",
                    finding_type="change",
                    severity=severity,
                    statement=statement,
                    confidence=0.9 if change_rate is not None else 0.7,
                    confidence_basis="确定性期间对比同时返回当期值、基准值和变化率",
                    limitations=[] if change_rate is not None else ["变化率缺失，只能确认两端数值"],
                    suggested_validation=["用 contribution 工具拆解变化来源"],
                    key=item.get("current_period"),
                )
            )
        return out

    def _contribution(self, run_id: str, evidence: EvidenceRecord) -> list[Finding]:
        attribution = attribution_from_evidence(evidence.result)
        if attribution is None:
            return self._legacy_contribution(run_id, evidence)
        if not attribution.items:
            return []
        top = attribution.items[:3]
        detail = "；".join(
            f"{item.dimension_value} 贡献 {_text(item.contribution)}（贡献率 {_text(item.contribution_rate)}）"
            for item in top
        )
        relative = (
            _text(attribution.relative_change)
            if attribution.relative_change is not None
            else "不可计算（基准期缺失或为 0）"
        )
        label = attribution.label or evidence.metric_code
        statement = (
            f"{label} 在 {attribution.current_start} 至 {attribution.current_end} "
            f"相对基准期：绝对变化 {_text(attribution.absolute_change)}，"
            f"相对变化率 {relative}；按 {attribution.dimension} 的贡献拆解：{detail}。"
        )
        limitations = [check for check in attribution.checks if "残差" in check or "不适用" in check]
        if attribution.missing_data:
            limitations.append("缺失数据：" + "、".join(attribution.missing_data))
        return [
            self._make(
                run_id,
                evidence,
                title=f"{label} 贡献拆解",
                finding_type="contribution",
                severity="primary",
                statement=statement,
                confidence=0.85 if attribution.allocatable else 0.6,
                confidence_basis="确定性加性贡献拆解，绝对变化、相对变化与贡献率分别计算",
                limitations=limitations,
                suggested_validation=[
                    "核对贡献合计与总变化是否一致",
                    "对最大贡献维度继续下钻",
                ],
                key="contribution",
            )
        ]

    def _legacy_contribution(self, run_id: str, evidence: EvidenceRecord) -> list[Finding]:
        """Handle older evidence payloads that predate the attribution contract."""

        items = [item for item in evidence.result.get("items") or [] if item.get("contribution") is not None]
        if not items:
            return []
        items.sort(key=lambda item: abs(Decimal(str(item["contribution"]))), reverse=True)
        top = items[:3]
        detail = "；".join(
            f"{item.get('dimension_value')} 贡献 {_text(item.get('contribution'))}"
            f"（贡献率 {_text(item.get('contribution_rate'))}）"
            for item in top
        )
        statement = (
            f"{evidence.result.get('label') or evidence.metric_code} 在 "
            f"{evidence.time_range_start} 至 {evidence.time_range_end} 相对基准期，"
            f"按 {evidence.result.get('dimension')} 的贡献拆解：{detail}。"
        )
        return [
            self._make(
                run_id,
                evidence,
                title=f"{evidence.result.get('label') or evidence.metric_code} 贡献拆解",
                finding_type="contribution",
                severity="primary",
                statement=statement,
                confidence=0.85,
                confidence_basis="确定性加性贡献拆解，贡献合计可与总变化核对",
                limitations=[]
                if evidence.result.get("residual") in (None, 0, "0")
                else [f"存在未分配残差 {evidence.result.get('residual')}"],
                suggested_validation=[
                    "核对贡献合计与总变化是否一致",
                    "对最大贡献维度继续下钻",
                ],
                key="contribution",
            )
        ]

    def _ranking(self, run_id: str, evidence: EvidenceRecord) -> list[Finding]:
        items = [item for item in evidence.result.get("items") or [] if item.get("value") is not None]
        if not items:
            return []
        top = items[:3]
        detail = "；".join(
            f"第{item.get('rank')}名 {item.get('dimension_value')} 为 {_text(item.get('value'))}" for item in top
        )
        return [
            self._make(
                run_id,
                evidence,
                title=f"{evidence.result.get('label') or evidence.metric_code} 排名",
                finding_type="fact",
                severity="context",
                statement=(
                    f"{evidence.result.get('label') or evidence.metric_code} 在 "
                    f"{evidence.time_range_start} 至 {evidence.time_range_end} "
                    f"按 {evidence.result.get('dimension')} 排名：{detail}。"
                ),
                confidence=0.9,
                confidence_basis="确定性排名结果，数值直接来自工具输出",
                limitations=[],
                suggested_validation=["确认排名维度的成员是否完整"],
                key="ranking",
            )
        ]

    def _anomaly(self, run_id: str, evidence: EvidenceRecord) -> list[Finding]:
        out = []
        for point in (evidence.result.get("points") or [])[:5]:
            statement = (
                f"{evidence.result.get('label') or evidence.metric_code} 在 {point.get('period')} "
                f"出现{('上升' if point.get('direction') == 'up' else '下降')}异常，"
                f"值 {_text(point.get('value'))}，变化率 {_text(point.get('change_rate'))}，"
                f"规则 {point.get('rule')} 阈值 {_text(point.get('threshold'))}。"
            )
            out.append(
                self._make(
                    run_id,
                    evidence,
                    title=f"{point.get('period')} 异常",
                    finding_type="anomaly",
                    severity="secondary",
                    statement=statement,
                    confidence=0.75,
                    confidence_basis="确定性异常规则命中，规则和阈值随结果返回",
                    limitations=["异常是统计偏离，不是业务原因"],
                    suggested_validation=["用 compare_periods 确认变化幅度", "下钻维度定位来源"],
                    key=point.get("period"),
                )
            )
        return out

    def _trend(self, run_id: str, evidence: EvidenceRecord) -> list[Finding]:
        direction = evidence.result.get("direction")
        if direction in {None, "unknown"}:
            return []
        return [
            self._make(
                run_id,
                evidence,
                title=f"{evidence.result.get('label') or evidence.metric_code} 趋势",
                finding_type="change",
                severity="secondary",
                statement=(
                    f"{evidence.result.get('label') or evidence.metric_code} 在 "
                    f"{evidence.time_range_start} 至 {evidence.time_range_end} 的趋势方向为 "
                    f"{direction}。"
                ),
                confidence=0.7,
                confidence_basis="确定性趋势规则判断，不是因果结论",
                limitations=["趋势方向不等于业务原因"],
                suggested_validation=["检查关键拐点与异常月份"],
                key="trend",
            )
        ]

    def _metric_value(self, run_id: str, evidence: EvidenceRecord) -> list[Finding]:
        if not evidence.result_summary:
            return []
        return [
            self._make(
                run_id,
                evidence,
                title=f"{evidence.result.get('label') or evidence.metric_code} 观测值",
                finding_type="fact",
                severity="context",
                statement=evidence.result_summary,
                confidence=0.9 if evidence.traceable else 0.6,
                confidence_basis="工具返回的确定性聚合结果"
                + ("，来源表与时间范围完整" if evidence.traceable else "，可追溯字段不完整"),
                limitations=[] if evidence.traceable else ["证据可追溯字段不完整"],
                suggested_validation=["如需结论，补充对比或贡献拆解"],
                key="value",
            )
        ]

    # -- cross-domain --------------------------------------------------
    def _cross_domain(self, run_id: str, evidences: list[EvidenceRecord]) -> list[Finding]:
        candidates = [
            item
            for item in evidences
            if item.evidence_type in {"comparison", "trend", "contribution"}
            and item.domain
            and self._direction(item) != 0
        ]
        out: list[Finding] = []
        seen: set[tuple[str, str]] = set()
        for index, left in enumerate(candidates):
            for right in candidates[index + 1 :]:
                if left.domain == right.domain:
                    continue
                pair = tuple(sorted((left.evidence_id, right.evidence_id)))
                if pair in seen:
                    continue
                seen.add(pair)
                left_sign, right_sign = self._direction(left), self._direction(right)
                same = left_sign == right_sign
                hypothesis = not same
                statement = (
                    f"{left.metric_code}（{left.domain}）与 {right.metric_code}"
                    f"（{right.domain}）在相近期间呈现{'同向' if same else '反向'}变化，"
                    f"{'可能相关' if same else '可能互相抵消或存在时滞'}，"
                    "这只是统计关联，需要进一步验证，不能据此断言因果关系。"
                )
                out.append(
                    self._make_pair(
                        run_id,
                        left,
                        right,
                        title="跨部门关联（统计）",
                        finding_type="hypothesis" if hypothesis else "correlation",
                        severity="context",
                        statement=statement,
                        confidence=0.3 if hypothesis else 0.35,
                        confidence_basis="跨业务域统计方向一致/相反，未做机制验证",
                        limitations=[
                            "仅为统计关联，不构成因果关系",
                            "未控制共同驱动因素",
                        ],
                        suggested_validation=[
                            "检查是否存在共同驱动因素",
                            "补充时间先后与机制证据后再下结论",
                        ],
                    )
                )
        return out

    @staticmethod
    def _direction(evidence: EvidenceRecord) -> int:
        if evidence.evidence_type == "trend":
            direction = evidence.result.get("direction")
            return 1 if direction == "growth" else -1 if direction == "decline" else 0
        if evidence.evidence_type == "comparison":
            values = [
                item.get("change_rate")
                for item in evidence.result.get("items") or []
                if item.get("change_rate") is not None
            ]
            return _sign(values[0]) if len(values) == 1 else 0
        if evidence.evidence_type == "contribution":
            return _sign(evidence.result.get("total_change"))
        return 0

    # -- builders ------------------------------------------------------
    def _make(
        self,
        run_id: str,
        evidence: EvidenceRecord,
        *,
        title: str,
        finding_type: FindingType,
        severity: FindingSeverity,
        statement: str,
        confidence: float,
        confidence_basis: str,
        limitations: list[str],
        suggested_validation: list[str],
        key: Any,
    ) -> Finding:
        return self._make_pair(
            run_id,
            evidence,
            None,
            title=title,
            finding_type=finding_type,
            severity=severity,
            statement=statement,
            confidence=confidence,
            confidence_basis=confidence_basis,
            limitations=limitations,
            suggested_validation=suggested_validation,
            key=key,
        )

    def _make_pair(
        self,
        run_id: str,
        left: EvidenceRecord,
        right: EvidenceRecord | None,
        *,
        title: str,
        finding_type: FindingType,
        severity: FindingSeverity,
        statement: str,
        confidence: float,
        confidence_basis: str,
        limitations: list[str],
        suggested_validation: list[str],
        key: Any = None,
    ) -> Finding:
        evidence_ids = [left.evidence_id] + ([right.evidence_id] if right else [])
        digest = hashlib.sha256("|".join([run_id, finding_type, statement, *evidence_ids]).encode("utf-8")).hexdigest()[
            :16
        ]
        return Finding(
            finding_id=f"F-{digest}",
            run_id=run_id,
            title=title,
            domain=left.domain,
            finding_type=finding_type,
            severity=severity,
            statement=statement,
            evidence_ids=evidence_ids,
            confidence=confidence,
            confidence_basis=confidence_basis,
            explanation=None,
            limitations=limitations,
            suggested_validation=suggested_validation,
            created_at=_now(),
        )

    def recommendation(
        self,
        run_id: str,
        *,
        action: str,
        rationale: str,
        evidence_ids: list[str],
        domain: str | None = None,
        confidence: float = 0.5,
        limitations: list[str] | None = None,
        suggested_validation: list[str] | None = None,
    ) -> Finding:
        digest = hashlib.sha256(
            "|".join([run_id, "recommendation", action, *evidence_ids]).encode("utf-8")
        ).hexdigest()[:16]
        return Finding(
            finding_id=f"F-{digest}",
            run_id=run_id,
            title=action,
            domain=domain,
            finding_type="recommendation",
            severity="secondary",
            statement=rationale,
            evidence_ids=evidence_ids,
            confidence=confidence,
            confidence_basis="由确定性发现模板生成，需人工确认后执行",
            limitations=limitations or ["建议不自动执行"],
            suggested_validation=suggested_validation or ["执行前人工复核"],
            created_at=_now(),
        )


class FindingStore:
    def __init__(self, engine: Engine, *, auto_create: bool = True):
        self.engine = engine
        self._lock = threading.RLock()
        if auto_create:
            create_schema(engine)

    def save_many(self, findings: Iterable[Finding]) -> list[Finding]:
        items = list(findings)
        stored: list[Finding] = []
        with self._lock, self.engine.begin() as connection:
            for finding in items:
                try:
                    with connection.begin_nested():
                        connection.execute(insert(FINDING_TABLE).values(**self._to_row(finding)))
                    stored.append(finding)
                except IntegrityError:
                    existing = connection.execute(
                        select(FINDING_TABLE).where(FINDING_TABLE.c.finding_id == finding.finding_id)
                    ).first()
                    if existing is not None:
                        stored.append(self._from_row(existing))
        return stored

    def list(self, run_id: str) -> list[Finding]:
        with self._lock, self.engine.connect() as connection:
            rows = connection.execute(
                select(FINDING_TABLE)
                .where(FINDING_TABLE.c.run_id == run_id)
                .order_by(FINDING_TABLE.c.created_at, FINDING_TABLE.c.finding_id)
            ).all()
        return [self._from_row(row) for row in rows]

    def get(self, run_id: str, finding_id: str) -> Finding | None:
        with self._lock, self.engine.connect() as connection:
            row = connection.execute(
                select(FINDING_TABLE).where(
                    FINDING_TABLE.c.run_id == run_id,
                    FINDING_TABLE.c.finding_id == finding_id,
                )
            ).first()
        return None if row is None else self._from_row(row)

    @staticmethod
    def _to_row(finding: Finding) -> dict[str, Any]:
        return {
            "finding_id": finding.finding_id,
            "run_id": finding.run_id,
            "title": finding.title,
            "domain": finding.domain,
            "finding_type": finding.finding_type,
            "severity": finding.severity,
            "statement": finding.statement,
            "evidence_ids": json.dumps(finding.evidence_ids, ensure_ascii=False),
            "confidence": finding.confidence,
            "confidence_basis": finding.confidence_basis,
            "explanation": finding.explanation,
            "limitations": json.dumps(finding.limitations, ensure_ascii=False),
            "suggested_validation": json.dumps(finding.suggested_validation, ensure_ascii=False),
            "created_at": finding.created_at.isoformat(timespec="seconds"),
        }

    @staticmethod
    def _from_row(row) -> Finding:
        return Finding(
            finding_id=row.finding_id,
            run_id=row.run_id,
            title=row.title,
            domain=row.domain,
            finding_type=row.finding_type,
            severity=row.severity,
            statement=row.statement,
            evidence_ids=json.loads(row.evidence_ids),
            confidence=row.confidence,
            confidence_basis=row.confidence_basis,
            explanation=row.explanation,
            limitations=json.loads(row.limitations),
            suggested_validation=json.loads(row.suggested_validation),
            created_at=datetime.fromisoformat(row.created_at),
        )

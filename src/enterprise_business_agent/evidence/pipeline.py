from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine

from ..llm import LLMClient
from ..observability.setup import span
from .approval import ApprovalStore
from .collector import EvidenceCollector
from .contracts import EvidenceRecord
from .findings import FindingEngine, FindingStore
from .ledger import EvidenceLedger
from .report import ReportGenerator, ReportStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..agent.tools import ToolResult


class Phase5Result(BaseModel):
    run_id: str
    evidence_count: int = 0
    finding_ids: list[str] = Field(default_factory=list)
    report_id: str | None = None
    report_version: int | None = None
    report_status: str | None = None
    validation_passed: bool | None = None
    approval_id: str | None = None
    approval_state: str | None = None
    approval_required: bool | None = None
    approval_fingerprint: str | None = None


class Phase5Pipeline:
    """Evidence collection, findings and report generation for one run store."""

    def __init__(
        self,
        engine: Engine,
        *,
        llm: LLMClient | None = None,
        narrative_enabled: bool = True,
        max_fix_attempts: int = 1,
        narrative_max_tokens: int = 800,
    ):
        self.ledger = EvidenceLedger(engine)
        self.collector = EvidenceCollector(self.ledger)
        self.finding_engine = FindingEngine()
        self.finding_store = FindingStore(engine)
        self.report_store = ReportStore(engine)
        self.approval_store = ApprovalStore(engine)
        self.report_generator = ReportGenerator(
            self.report_store,
            llm=llm,
            narrative_enabled=narrative_enabled,
            max_fix_attempts=max_fix_attempts,
            narrative_max_tokens=narrative_max_tokens,
        )

    def record_tool_result(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        tool: str,
        arguments: dict[str, Any],
        result: "ToolResult",
    ) -> list[EvidenceRecord]:
        with span(
            "evidence.collect",
            evidence__tool=tool,
            evidence__run_id=run_id,
        ) as current:
            records = self.collector.collect(
                run_id=run_id,
                tool_call_id=tool_call_id,
                tool=tool,
                arguments=arguments,
                result=result,
            )
            if current is not None:
                try:
                    current.set_attribute("evidence.count", len(records))
                except Exception:
                    pass
            return records

    def finalize(
        self,
        *,
        run_id: str,
        question: str,
        principal_id: str,
        role: str,
        provider: str | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
        stop_reason: str | None = None,
    ) -> Phase5Result:
        evidences = self.ledger.list(run_id)
        with span("finding.build", finding__evidence_count=len(evidences)) as current:
            findings = self.finding_engine.build(run_id, evidences)
            stored = self.finding_store.save_many(findings)
            if current is not None:
                try:
                    current.set_attribute("finding.count", len(stored or findings))
                except Exception:
                    pass
        # A resumed run may reach finalize again after a crash. The report
        # version is append-only, so reuse the existing version instead of
        # manufacturing a new one for the same run.
        report = self.report_store.latest(run_id)
        if report is None:
            with span("report.generate", report__run_id=run_id) as current:
                report = self.report_generator.generate(
                    run_id=run_id,
                    question=question,
                    principal_id=principal_id,
                    role=role,
                    evidences=evidences,
                    findings=stored or findings,
                    provider=provider,
                    model=model,
                    prompt_version=prompt_version,
                    stop_reason=stop_reason,
                )
                if current is not None:
                    try:
                        current.set_attribute("report.id", report.report_id)
                        current.set_attribute("report.version", report.version)
                        current.set_attribute("report.status", report.status)
                        current.set_attribute(
                            "report.validation_passed",
                            bool(report.validation.passed),
                        )
                    except Exception:
                        pass
        else:
            with span(
                "report.reuse",
                report__id=report.report_id,
                report__version=report.version,
            ):
                pass
        with span("approval.ensure", approval__run_id=run_id) as current:
            approval = self.approval_store.ensure_for_report(report, requested_by=principal_id)
            if current is not None:
                try:
                    current.set_attribute("approval.id", approval.approval_id)
                    current.set_attribute("approval.state", approval.state)
                    current.set_attribute("approval.required", bool(approval.requires_approval))
                except Exception:
                    pass
        return Phase5Result(
            run_id=run_id,
            evidence_count=len(evidences),
            finding_ids=[item.finding_id for item in stored or findings],
            report_id=report.report_id,
            report_version=report.version,
            report_status=report.status,
            validation_passed=report.validation.passed,
            approval_id=approval.approval_id,
            approval_state=approval.state,
            approval_required=approval.requires_approval,
            approval_fingerprint=approval.report_fingerprint,
        )


__all__ = ["Phase5Pipeline", "Phase5Result"]

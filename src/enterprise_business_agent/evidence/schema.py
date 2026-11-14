"""Phase 5 operational tables: evidence ledger, findings and reports.

These tables live in the run store database (``RUN_STORE_URL``), not in the
business database. The application holds a read-only connection to the business
database; run metadata is operational state and therefore belongs next to
``agent_runs`` and ``agent_audit_events``.

``create_schema`` is idempotent, so an existing run store created before Phase 5
is upgraded in place without touching its existing rows.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Engine

EVIDENCE_METADATA = MetaData()

EVIDENCE_TABLE = Table(
    "agent_evidence",
    EVIDENCE_METADATA,
    Column("evidence_id", String(128), primary_key=True),
    Column("run_id", String(64), nullable=False, index=True),
    Column("tool_call_id", String(128), nullable=False, index=True),
    Column("evidence_type", String(32), nullable=False),
    Column("domain", String(64), nullable=True),
    Column("metric_code", String(64), nullable=True),
    Column("metric_definition_version", String(32), nullable=True),
    Column("time_range_start", String(10), nullable=True),
    Column("time_range_end", String(10), nullable=True),
    Column("baseline_period", String(32), nullable=True),
    Column("baseline_time_range_start", String(10), nullable=True),
    Column("baseline_time_range_end", String(10), nullable=True),
    Column("grain", String(16), nullable=True),
    Column("filters", Text, nullable=False),
    Column("dimensions", Text, nullable=False),
    Column("query_parameters", Text, nullable=False),
    Column("result", Text, nullable=False),
    Column("result_summary", Text, nullable=True),
    Column("unit", String(32), nullable=True),
    Column("source_tables", Text, nullable=False),
    Column("calculation_method", String(200), nullable=True),
    Column("status", String(24), nullable=False),
    Column("error_code", String(64), nullable=True),
    Column("error_message", Text, nullable=True),
    Column("traceable", Boolean, nullable=False, default=False),
    Column("fingerprint", String(64), nullable=False),
    Column("created_at", String(40), nullable=False),
    UniqueConstraint("run_id", "fingerprint", name="uq_agent_evidence_run_fingerprint"),
    Index("ix_agent_evidence_run_type", "run_id", "evidence_type"),
)

FINDING_TABLE = Table(
    "agent_findings",
    EVIDENCE_METADATA,
    Column("finding_id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=False, index=True),
    Column("title", String(200), nullable=False),
    Column("domain", String(64), nullable=True),
    Column("finding_type", String(24), nullable=False),
    Column("severity", String(16), nullable=False),
    Column("statement", Text, nullable=False),
    Column("evidence_ids", Text, nullable=False),
    Column("confidence", Float, nullable=False),
    Column("confidence_basis", Text, nullable=False),
    Column("explanation", Text, nullable=True),
    Column("limitations", Text, nullable=False),
    Column("suggested_validation", Text, nullable=False),
    Column("created_at", String(40), nullable=False),
    Index("ix_agent_findings_run_type", "run_id", "finding_type"),
)

REPORT_TABLE = Table(
    "agent_reports",
    EVIDENCE_METADATA,
    Column("report_id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=False, index=True),
    Column("report_version", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    Column("report_json", Text, nullable=False),
    Column("validation_json", Text, nullable=False),
    Column("created_at", String(40), nullable=False),
    UniqueConstraint("run_id", "report_version", name="uq_agent_reports_run_version"),
)


def create_schema(engine: Engine) -> None:
    """Create Phase 5 tables if they are missing. Safe to call on every start."""

    EVIDENCE_METADATA.create_all(engine, checkfirst=True)


def drop_schema(engine: Engine) -> None:
    """Test helper. Dropped explicitly so business tables are never touched."""

    EVIDENCE_METADATA.drop_all(engine)

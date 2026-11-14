from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import Engine, insert, select
from sqlalchemy.exc import IntegrityError

from .contracts import EvidenceRecord
from .schema import EVIDENCE_METADATA, EVIDENCE_TABLE, create_schema


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class EvidenceLedger:
    """Durable evidence store keyed by run_id and tool_call_id.

    Recording is idempotent per ``(run_id, fingerprint)``. The fingerprint is
    computed by the collector from the tool name and validated arguments, so a
    retry of the exact same call reuses the original row while a different time
    range or filter set produces a distinct evidence record.
    """

    def __init__(self, engine: Engine, *, auto_create: bool = True):
        self.engine = engine
        self._lock = threading.RLock()
        if auto_create:
            create_schema(engine)

    # -- writes --------------------------------------------------------
    def record(self, record: EvidenceRecord) -> tuple[EvidenceRecord, bool]:
        results = self.record_many([record])
        return results[0]

    def record_many(
        self, records: Iterable[EvidenceRecord]
    ) -> list[tuple[EvidenceRecord, bool]]:
        items = list(records)
        if not items:
            return []
        outcomes: list[tuple[EvidenceRecord, bool]] = []
        with self._lock, self.engine.begin() as connection:
            for record in items:
                existing = connection.execute(
                    select(EVIDENCE_TABLE).where(
                        EVIDENCE_TABLE.c.run_id == record.run_id,
                        EVIDENCE_TABLE.c.fingerprint == record.fingerprint,
                    )
                ).first()
                if existing is not None:
                    outcomes.append((self._row_to_record(existing), False))
                    continue
                try:
                    with connection.begin_nested():
                        connection.execute(
                            insert(EVIDENCE_TABLE).values(**self._record_to_row(record))
                        )
                    outcomes.append((record, True))
                except IntegrityError:
                    # A concurrent writer inserted the same fingerprint first.
                    existing = connection.execute(
                        select(EVIDENCE_TABLE).where(
                            EVIDENCE_TABLE.c.run_id == record.run_id,
                            EVIDENCE_TABLE.c.fingerprint == record.fingerprint,
                        )
                    ).first()
                    if existing is None:
                        raise
                    outcomes.append((self._row_to_record(existing), False))
        return outcomes

    # -- reads ---------------------------------------------------------
    def get(self, run_id: str, evidence_id: str) -> EvidenceRecord | None:
        with self._lock, self.engine.connect() as connection:
            row = connection.execute(
                select(EVIDENCE_TABLE).where(
                    EVIDENCE_TABLE.c.run_id == run_id,
                    EVIDENCE_TABLE.c.evidence_id == evidence_id,
                )
            ).first()
        return None if row is None else self._row_to_record(row)

    def list(self, run_id: str) -> list[EvidenceRecord]:
        with self._lock, self.engine.connect() as connection:
            rows = connection.execute(
                select(EVIDENCE_TABLE)
                .where(EVIDENCE_TABLE.c.run_id == run_id)
                .order_by(EVIDENCE_TABLE.c.created_at, EVIDENCE_TABLE.c.evidence_id)
            ).all()
        return [self._row_to_record(row) for row in rows]

    def count(self, run_id: str) -> int:
        with self._lock, self.engine.connect() as connection:
            rows = connection.execute(
                select(EVIDENCE_TABLE.c.evidence_id).where(
                    EVIDENCE_TABLE.c.run_id == run_id
                )
            ).all()
        return len(rows)

    # -- mapping -------------------------------------------------------
    @staticmethod
    def _record_to_row(record: EvidenceRecord) -> dict[str, Any]:
        return {
            "evidence_id": record.evidence_id,
            "run_id": record.run_id,
            "tool_call_id": record.tool_call_id,
            "evidence_type": record.evidence_type,
            "domain": record.domain,
            "metric_code": record.metric_code,
            "metric_definition_version": record.metric_definition_version,
            "time_range_start": record.time_range_start,
            "time_range_end": record.time_range_end,
            "baseline_period": record.baseline_period,
            "baseline_time_range_start": record.baseline_time_range_start,
            "baseline_time_range_end": record.baseline_time_range_end,
            "grain": record.grain,
            "filters": _json(record.filters),
            "dimensions": _json(record.dimensions),
            "query_parameters": _json(record.query_parameters),
            "result": _json(record.result),
            "result_summary": record.result_summary,
            "unit": record.unit,
            "source_tables": _json(record.source_tables),
            "calculation_method": record.calculation_method,
            "status": record.status,
            "error_code": record.error_code,
            "error_message": record.error_message,
            "traceable": record.traceable,
            "fingerprint": record.fingerprint,
            "created_at": record.created_at.isoformat(timespec="seconds"),
        }

    @staticmethod
    def _row_to_record(row) -> EvidenceRecord:
        return EvidenceRecord(
            evidence_id=row.evidence_id,
            run_id=row.run_id,
            tool_call_id=row.tool_call_id,
            evidence_type=row.evidence_type,
            domain=row.domain,
            metric_code=row.metric_code,
            metric_definition_version=row.metric_definition_version,
            time_range_start=row.time_range_start,
            time_range_end=row.time_range_end,
            baseline_period=row.baseline_period,
            baseline_time_range_start=row.baseline_time_range_start,
            baseline_time_range_end=row.baseline_time_range_end,
            grain=row.grain,
            filters=json.loads(row.filters),
            dimensions=json.loads(row.dimensions),
            query_parameters=json.loads(row.query_parameters),
            result=json.loads(row.result),
            result_summary=row.result_summary,
            unit=row.unit,
            source_tables=json.loads(row.source_tables),
            calculation_method=row.calculation_method,
            status=row.status,
            error_code=row.error_code,
            error_message=row.error_message,
            traceable=bool(row.traceable),
            fingerprint=row.fingerprint,
            created_at=datetime.fromisoformat(row.created_at),
        )


__all__ = ["EvidenceLedger", "EVIDENCE_METADATA", "create_schema", "_now"]

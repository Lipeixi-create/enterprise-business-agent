from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    Boolean,
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    inspect,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from ..observability.setup import traced
from .contracts import ReportDocument, StoredReport

ApprovalState = Literal["not_required", "pending", "approved", "rejected", "cancelled"]
ApprovalAction = Literal["approve", "reject", "cancel"]

TERMINAL_APPROVAL_STATES = frozenset({"not_required", "approved", "rejected", "cancelled"})


def _now() -> str:
    # Microseconds keep the audit trail strictly ordered even when the request
    # is entered and decided within the same wall-clock second.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


APPROVAL_METADATA = MetaData()

_APPROVALS = Table(
    "agent_report_approvals",
    APPROVAL_METADATA,
    Column("approval_id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=False, index=True),
    Column("report_id", String(64), nullable=False),
    Column("report_version", Integer, nullable=False),
    Column("report_fingerprint", String(64), nullable=False),
    Column("state", String(20), nullable=False, index=True),
    Column("requires_approval", Boolean, nullable=False),
    Column("requested_by", String(128), nullable=False),
    Column("requested_at", String(40), nullable=False),
    Column("decided_by", String(128), nullable=True),
    Column("decided_at", String(40), nullable=True),
    Column("comment", Text, nullable=True),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    UniqueConstraint("report_id", "report_version", name="uq_agent_report_approval_version"),
    Index("ix_agent_report_approvals_run_version", "run_id", "report_version"),
)

_APPROVAL_EVENTS = Table(
    "agent_report_approval_events",
    APPROVAL_METADATA,
    Column("event_id", String(64), primary_key=True),
    Column("approval_id", String(64), nullable=False, index=True),
    Column("run_id", String(64), nullable=False, index=True),
    Column("report_id", String(64), nullable=False),
    Column("report_version", Integer, nullable=False),
    Column("from_state", String(20), nullable=True),
    Column("to_state", String(20), nullable=False),
    Column("actor", String(128), nullable=False),
    Column("comment", Text, nullable=True),
    Column("created_at", String(40), nullable=False),
    Index(
        "ix_agent_report_approval_events_approval",
        "approval_id",
        "created_at",
    ),
)


def create_approval_schema(engine: Engine) -> None:
    APPROVAL_METADATA.create_all(engine, checkfirst=True)


def drop_approval_schema(engine: Engine) -> None:
    APPROVAL_METADATA.drop_all(engine, checkfirst=True)


def report_fingerprint(report: ReportDocument) -> str:
    """Stable content fingerprint for a report version.

    ``report_info.version`` is part of the document, so a new version always
    produces a different fingerprint. The canonical JSON form avoids ordering
    and whitespace differences between the generator and a database roundtrip.
    """

    canonical = json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str
    run_id: str
    report_id: str
    report_version: int
    report_fingerprint: str
    state: ApprovalState
    requires_approval: bool
    requested_by: str
    requested_at: str
    decided_by: str | None = None
    decided_at: str | None = None
    comment: str | None = None
    created_at: str
    updated_at: str


class ApprovalEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    approval_id: str
    run_id: str
    report_id: str
    report_version: int
    from_state: str | None = None
    to_state: str
    actor: str
    comment: str | None = None
    created_at: str


class ApprovalError(Exception):
    code = "approval_error"


class ApprovalNotFound(ApprovalError):
    code = "approval_not_found"


class InvalidApprovalTransition(ApprovalError):
    code = "invalid_approval_transition"


class ApprovalStale(ApprovalError):
    code = "approval_stale"


class ApprovalSeparationViolation(ApprovalError):
    code = "approval_separation_violation"


_ACTION_STATE: dict[str, ApprovalState] = {
    "approve": "approved",
    "reject": "rejected",
    "cancel": "cancelled",
}


def _row_to_record(row: Any) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=row.approval_id,
        run_id=row.run_id,
        report_id=row.report_id,
        report_version=row.report_version,
        report_fingerprint=row.report_fingerprint,
        state=row.state,
        requires_approval=bool(row.requires_approval),
        requested_by=row.requested_by,
        requested_at=row.requested_at,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        comment=row.comment,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class ApprovalStore:
    """Report approval state machine bound to one report version.

    The store is intentionally separate from business execution: an approval
    records a human decision and an audit trail, and nothing in this module
    executes a purchase, price change, budget change or HR action.
    """

    def __init__(self, engine: Engine | str, *, auto_create: bool | None = None):
        if isinstance(engine, str):
            engine = create_engine(engine, future=True, pool_pre_ping=True)
        self.engine = engine
        if auto_create is None:
            auto_create = self.engine.url.get_backend_name() == "sqlite"
        if auto_create:
            create_approval_schema(self.engine)
        elif not inspect(self.engine).has_table("agent_report_approvals"):
            raise RuntimeError(
                "approval schema is missing; run scripts/init_runtime_store.py "
                "with the setup role before starting the API with the runtime role"
            )

    def ensure_for_report(
        self, stored: StoredReport, *, requested_by: str
    ) -> ApprovalRecord:
        existing = self.get(stored.report_id, stored.version)
        if existing is not None:
            return existing
        requires = stored.status == "validated" and any(
            item.requires_approval for item in stored.report.recommendations
        )
        state: ApprovalState = "pending" if requires else "not_required"
        now = _now()
        approval_id = str(uuid.uuid4())
        values = {
            "approval_id": approval_id,
            "run_id": stored.run_id,
            "report_id": stored.report_id,
            "report_version": stored.version,
            "report_fingerprint": report_fingerprint(stored.report),
            "state": state,
            "requires_approval": requires,
            "requested_by": requested_by,
            "requested_at": now,
            "decided_by": None,
            "decided_at": None,
            "comment": None,
            "created_at": now,
            "updated_at": now,
        }
        with self.engine.begin() as connection:
            try:
                connection.execute(_APPROVALS.insert().values(**values))
            except IntegrityError:
                pass
            else:
                connection.execute(
                    _APPROVAL_EVENTS.insert().values(
                        event_id=str(uuid.uuid4()),
                        approval_id=approval_id,
                        run_id=stored.run_id,
                        report_id=stored.report_id,
                        report_version=stored.version,
                        from_state=None,
                        to_state=state,
                        actor=requested_by,
                        comment=(
                            "high-risk recommendation entered approval"
                            if requires
                            else "report has no high-risk recommendation"
                        ),
                        created_at=now,
                    )
                )
        record = self.get(stored.report_id, stored.version)
        if record is None:  # pragma: no cover - defensive, insert raced and lost
            raise ApprovalNotFound("approval row could not be read after insert")
        return record

    def get(self, report_id: str, version: int) -> ApprovalRecord | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(_APPROVALS).where(
                    _APPROVALS.c.report_id == report_id,
                    _APPROVALS.c.report_version == version,
                )
            ).first()
        return None if row is None else _row_to_record(row)

    def latest_for_run(self, run_id: str) -> ApprovalRecord | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(_APPROVALS)
                .where(_APPROVALS.c.run_id == run_id)
                .order_by(
                    _APPROVALS.c.report_version.desc(),
                    _APPROVALS.c.created_at.desc(),
                )
                .limit(1)
            ).first()
        return None if row is None else _row_to_record(row)

    def list_for_run(self, run_id: str) -> list[ApprovalRecord]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(_APPROVALS)
                .where(_APPROVALS.c.run_id == run_id)
                .order_by(_APPROVALS.c.report_version, _APPROVALS.c.created_at)
            ).all()
        return [_row_to_record(row) for row in rows]

    def list_approvals(
        self,
        *,
        states: list[str] | None = None,
        approver: str | None = None,
        requester: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        visible_to: str | None = None,
        read_any: bool = False,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "requested_at",
        sort_dir: str = "desc",
    ) -> dict[str, Any]:
        """Cross-run approval workbench query.

        A caller without ``approval:read_any`` only sees approvals they
        requested or decided. This is the narrowest scope the current data
        model can enforce; there is no department/region assignment on an
        approval, so inventing one here would be dishonest.
        """

        from sqlalchemy import func, or_

        conditions: list[Any] = []
        if states:
            conditions.append(_APPROVALS.c.state.in_(states))
        if approver:
            conditions.append(_APPROVALS.c.decided_by == approver)
        if requester:
            conditions.append(_APPROVALS.c.requested_by == requester)
        if created_from:
            conditions.append(_APPROVALS.c.created_at >= created_from)
        if created_to:
            conditions.append(_APPROVALS.c.created_at <= created_to)
        if not read_any:
            if visible_to is None:
                conditions.append(_APPROVALS.c.approval_id.is_(None))
            else:
                conditions.append(
                    or_(
                        _APPROVALS.c.requested_by == visible_to,
                        _APPROVALS.c.decided_by == visible_to,
                    )
                )

        sortable = {
            "requested_at": _APPROVALS.c.requested_at,
            "created_at": _APPROVALS.c.created_at,
            "updated_at": _APPROVALS.c.updated_at,
            "decided_at": _APPROVALS.c.decided_at,
            "state": _APPROVALS.c.state,
            "report_version": _APPROVALS.c.report_version,
        }
        column = sortable.get(sort_by, _APPROVALS.c.requested_at)
        order = column.asc() if sort_dir == "asc" else column.desc()
        offset = (max(1, page) - 1) * page_size
        with self.engine.connect() as connection:
            total = int(
                connection.execute(
                    select(func.count())
                    .select_from(_APPROVALS)
                    .where(*conditions)
                ).scalar_one()
            )
            rows = connection.execute(
                select(_APPROVALS)
                .where(*conditions)
                .order_by(order, _APPROVALS.c.approval_id)
                .limit(page_size)
                .offset(offset)
            ).all()
        pages = (total + page_size - 1) // page_size if total else 0
        return {
            "items": [_row_to_record(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
            "sort_by": sort_by,
            "sort_dir": sort_dir,
        }

    def events(self, approval_id: str) -> list[ApprovalEvent]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(_APPROVAL_EVENTS)
                .where(_APPROVAL_EVENTS.c.approval_id == approval_id)
                .order_by(_APPROVAL_EVENTS.c.created_at, _APPROVAL_EVENTS.c.event_id)
            ).all()
        return [
            ApprovalEvent(
                event_id=row.event_id,
                approval_id=row.approval_id,
                run_id=row.run_id,
                report_id=row.report_id,
                report_version=row.report_version,
                from_state=row.from_state,
                to_state=row.to_state,
                actor=row.actor,
                comment=row.comment,
                created_at=row.created_at,
            )
            for row in rows
        ]

    @traced(
        "approval.decide",
        approval__report_id="report_id",
        approval__report_version="version",
        approval__action="action",
        approval__actor="actor",
    )
    def decide(
        self,
        *,
        report_id: str,
        version: int,
        action: ApprovalAction,
        actor: str,
        current_fingerprint: str,
        comment: str | None = None,
    ) -> tuple[ApprovalRecord, bool]:
        """Apply one decision. Returns ``(record, duplicate)``.

        Repeated identical decisions are idempotent. A different decision after
        a terminal state is rejected, and a content fingerprint mismatch is
        rejected before any state change.
        """

        target = _ACTION_STATE[action]
        query = (
            select(_APPROVALS)
            .where(
                _APPROVALS.c.report_id == report_id,
                _APPROVALS.c.report_version == version,
            )
            .with_for_update()
        )
        with self.engine.begin() as connection:
            row = connection.execute(query).first()
            if row is None:
                raise ApprovalNotFound("no approval request for this report version")
            record = _row_to_record(row)
            if record.report_fingerprint != current_fingerprint:
                raise ApprovalStale(
                    "the report content no longer matches the approval request"
                )
            if action in {"approve", "reject"} and actor == record.requested_by:
                raise ApprovalSeparationViolation(
                    "the report owner cannot approve or reject their own report"
                )
            if record.state == target:
                if record.decided_by == actor:
                    return record, True
                raise InvalidApprovalTransition(
                    f"approval is already {record.state}"
                )
            if record.state != "pending":
                raise InvalidApprovalTransition(
                    f"cannot {action} an approval in state {record.state!r}"
                )
            now = _now()
            connection.execute(
                update(_APPROVALS)
                .where(_APPROVALS.c.approval_id == record.approval_id)
                .values(
                    state=target,
                    decided_by=actor,
                    decided_at=now,
                    comment=comment,
                    updated_at=now,
                )
            )
            connection.execute(
                _APPROVAL_EVENTS.insert().values(
                    event_id=str(uuid.uuid4()),
                    approval_id=record.approval_id,
                    run_id=record.run_id,
                    report_id=record.report_id,
                    report_version=record.report_version,
                    from_state=record.state,
                    to_state=target,
                    actor=actor,
                    comment=comment,
                    created_at=now,
                )
            )
            updated = connection.execute(
                select(_APPROVALS).where(
                    _APPROVALS.c.approval_id == record.approval_id
                )
            ).first()
        return _row_to_record(updated), False

    def delete_for_run(self, run_id: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                delete(_APPROVAL_EVENTS).where(_APPROVAL_EVENTS.c.run_id == run_id)
            )
            connection.execute(
                delete(_APPROVALS).where(_APPROVALS.c.run_id == run_id)
            )


__all__ = [
    "APPROVAL_METADATA",
    "ApprovalAction",
    "ApprovalError",
    "ApprovalEvent",
    "ApprovalNotFound",
    "ApprovalRecord",
    "ApprovalSeparationViolation",
    "ApprovalStale",
    "ApprovalState",
    "ApprovalStore",
    "InvalidApprovalTransition",
    "TERMINAL_APPROVAL_STATES",
    "create_approval_schema",
    "drop_approval_schema",
    "report_fingerprint",
]

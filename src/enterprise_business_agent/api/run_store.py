from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import (
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    event,
    func,
    inspect,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.pool import StaticPool

from ..agent.checkpoint import CHECKPOINT_METADATA
from ..evidence.approval import APPROVAL_METADATA
from ..evidence.schema import EVIDENCE_METADATA


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_precise() -> str:
    # Lease comparisons are string comparisons, so every timestamp must use the
    # same width. Microseconds avoid a lease looking expired immediately after
    # it was granted within the same wall-clock second.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _iso_at(seconds: float) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=max(0.0, seconds))
    ).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class RunClaim:
    """A fenced lease on one run.

    ``claim_version`` increases on every successful acquisition. A worker must
    present the same version for every state write, so a worker that lost its
    lease cannot overwrite the new owner's result.
    """

    run_id: str
    worker_id: str
    claim_version: int
    lease_expires_at: str


class RunClaimLost(RuntimeError):
    """Raised when a fenced write no longer owns the run."""

    code = "run_claim_lost"


@dataclass(frozen=True)
class ClaimAttempt:
    claim: RunClaim | None
    reason: str
    lease_expires_at: str | None = None
    held_by: str | None = None


class RunStoreConnectionErrorCounter:
    """Process-local count of database connection failures."""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, exception_context: Any) -> None:
        error = getattr(exception_context, "original_exception", None)
        if isinstance(error, OperationalError) or getattr(
            exception_context, "is_disconnect", False
        ):
            self.count += 1


RUN_STORE_METADATA = MetaData()

_RUNS = Table(
    "agent_runs",
    RUN_STORE_METADATA,
    Column("run_id", String(64), primary_key=True),
    Column("request_id", String(128), nullable=False),
    Column("owner", String(128), nullable=False, index=True),
    Column("status", String(32), nullable=False, index=True),
    Column("question", Text, nullable=False),
    Column("idempotency_key", String(200), nullable=True),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    Column("payload", Text, nullable=False),
    # Batch 3 claim/lease/fencing. The runtime role only needs DML; the setup
    # role adds these columns in place for databases created by Batch 2.
    Column("claim_owner", String(128), nullable=True),
    Column("claim_version", Integer, nullable=False, default=0, server_default="0"),
    Column("claimed_at", String(40), nullable=True),
    Column("lease_expires_at", String(40), nullable=True, index=True),
    Column("execution_seconds", Float, nullable=False, default=0.0, server_default="0"),
    Column("last_resumed_at", String(40), nullable=True),
    Column("resume_count", Integer, nullable=False, default=0, server_default="0"),
    UniqueConstraint("owner", "idempotency_key", name="uq_agent_runs_owner_idempotency"),
    Index("ix_agent_runs_status_updated", "status", "updated_at"),
    Index("ix_agent_runs_active_lease", "status", "lease_expires_at"),
)

_EVENTS = Table(
    "agent_audit_events",
    RUN_STORE_METADATA,
    Column("event_id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=True, index=True),
    Column("request_id", String(128), nullable=True),
    Column("principal_id", String(128), nullable=True, index=True),
    Column("event_type", String(64), nullable=False, index=True),
    Column("tool_call_id", String(128), nullable=True),
    Column("detail", Text, nullable=False),
    Column("created_at", String(40), nullable=False, index=True),
)

_TOOL_RESULTS = Table(
    "agent_tool_results",
    RUN_STORE_METADATA,
    Column("run_id", String(64), primary_key=True),
    Column("call_key", String(64), primary_key=True),
    Column("tool_name", String(64), nullable=False),
    Column("call_id", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("result_json", Text, nullable=False),
    Column("created_at", String(40), nullable=False),
    Index("ix_agent_tool_results_run", "run_id"),
)


_BATCH3_RUN_COLUMNS: dict[str, str] = {
    "claim_owner": "VARCHAR(128)",
    "claim_version": "INTEGER NOT NULL DEFAULT 0",
    "claimed_at": "VARCHAR(40)",
    "lease_expires_at": "VARCHAR(40)",
    "execution_seconds": "DOUBLE PRECISION NOT NULL DEFAULT 0",
    "last_resumed_at": "VARCHAR(40)",
    "resume_count": "INTEGER NOT NULL DEFAULT 0",
}


def ensure_run_store_columns(engine) -> list[str]:
    """Add Batch 3 columns to an existing ``agent_runs`` table in place.

    ``MetaData.create_all(checkfirst=True)`` never evolves an existing table.
    The setup role therefore runs this small additive migration before the
    runtime role starts. No row is deleted and no business table is touched.
    """

    existing = {column["name"] for column in inspect(engine).get_columns("agent_runs")}
    added: list[str] = []
    with engine.begin() as connection:
        for name, ddl in _BATCH3_RUN_COLUMNS.items():
            if name in existing:
                continue
            connection.exec_driver_sql(
                f"ALTER TABLE agent_runs ADD COLUMN {name} {ddl}"
            )
            added.append(name)
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_agent_runs_active_lease "
            "ON agent_runs (status, lease_expires_at)"
        )
    return added


def create_run_store_schema(engine) -> None:
    RUN_STORE_METADATA.create_all(engine, checkfirst=True)
    ensure_run_store_columns(engine)


def drop_run_store_schema(engine) -> None:
    RUN_STORE_METADATA.drop_all(engine, checkfirst=True)


class PersistentRunStore:
    """Run results and audit/trace events in a SQLAlchemy-managed table.

    This is intentionally a small durable store, not a task queue. A run row
    starts as ``queued``, becomes ``running`` and ends in a terminal status;
    after a process restart any non-terminal row is marked ``interrupted``
    rather than silently staying ``running`` forever.

    The payload is the same JSON document the synchronous API returns, so the
    read endpoint behaves identically for both modes.

    SQLite is created lazily for local development. PostgreSQL never issues
    DDL from this class: the setup role runs :func:`create_run_store_schema`
    first, and the runtime role only receives DML grants.
    """

    def __init__(
        self,
        url: str,
        *,
        ttl_seconds: float | None = None,
        auto_create: bool | None = None,
    ):
        kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 10}
            if ":memory:" in url or url in {"sqlite://", "sqlite:///"}:
                kwargs["poolclass"] = StaticPool
        else:
            # A runtime role must not be able to hold a transaction open
            # indefinitely or issue a statement without a deadline.
            kwargs["connect_args"] = {
                "application_name": "eba_run_store",
                "connect_timeout": 5,
                "options": (
                    "-c statement_timeout=30000 "
                    "-c lock_timeout=10000 "
                    "-c idle_in_transaction_session_timeout=30000"
                ),
            }
        self.engine = create_engine(url, **kwargs)
        if auto_create is None:
            auto_create = self.engine.url.get_backend_name() == "sqlite"
        if auto_create:
            create_run_store_schema(self.engine)
        elif not inspect(self.engine).has_table("agent_runs"):
            raise RuntimeError(
                "run store schema is missing; run scripts/init_runtime_store.py "
                "with the setup role before starting the API with the runtime role"
            )
        self.ttl_seconds = ttl_seconds
        self.connection_errors = RunStoreConnectionErrorCounter()
        event.listen(self.engine, "handle_error", self.connection_errors)
        self._lock = threading.RLock()

    def close(self) -> None:
        self.engine.dispose()

    # -- run documents -------------------------------------------------
    def create(
        self,
        *,
        run_id: str,
        request_id: str,
        owner: str,
        status: str,
        question: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Insert a new run. Returns (payload, created).

        A duplicate ``(owner, idempotency_key)`` returns the existing run
        instead of creating a second task.
        """

        now = _now()
        document = json.dumps(payload, ensure_ascii=False, default=str)
        with self._lock, self.engine.begin() as connection:
            try:
                connection.execute(
                    _RUNS.insert().values(
                        run_id=run_id,
                        request_id=request_id,
                        owner=owner,
                        status=status,
                        question=question,
                        idempotency_key=idempotency_key,
                        created_at=now,
                        updated_at=now,
                        payload=document,
                    )
                )
                return payload, True
            except IntegrityError:
                pass
        if idempotency_key is None:
            raise RuntimeError("run insert failed without an idempotency key")
        existing = self.find_by_idempotency(owner, idempotency_key)
        if existing is None:
            raise RuntimeError("duplicate run insert but no existing row was found")
        return existing, False

    def put(
        self,
        run_id: str,
        payload: dict[str, Any],
        owner: str = "anonymous",
        *,
        request_id: str | None = None,
        status: str | None = None,
        question: str | None = None,
        idempotency_key: str | None = None,
        fence: RunClaim | None = None,
    ) -> None:
        document = json.dumps(payload, ensure_ascii=False, default=str)
        now = _now()
        state = status or str(payload.get("status") or "unknown")
        with self._lock, self.engine.begin() as connection:
            existing = connection.execute(
                select(_RUNS.c.run_id).where(_RUNS.c.run_id == run_id)
            ).first()
            if existing is None:
                if fence is not None:
                    raise RunClaimLost("cannot write a run that does not exist")
                connection.execute(
                    _RUNS.insert().values(
                        run_id=run_id,
                        request_id=request_id or run_id,
                        owner=owner,
                        status=state,
                        question=question or str(payload.get("question") or ""),
                        idempotency_key=idempotency_key,
                        created_at=now,
                        updated_at=now,
                        payload=document,
                    )
                )
            else:
                statement = update(_RUNS).where(_RUNS.c.run_id == run_id)
                if fence is not None:
                    statement = statement.where(
                        _RUNS.c.claim_owner == fence.worker_id,
                        _RUNS.c.claim_version == fence.claim_version,
                    )
                result = connection.execute(
                    statement.values(status=state, updated_at=now, payload=document)
                )
                if fence is not None and not result.rowcount:
                    raise RunClaimLost(
                        f"worker {fence.worker_id!r} no longer owns run {run_id!r}"
                    )

    def get(self, run_id: str, owner: str | None = None) -> dict[str, Any] | None:
        with self._lock, self.engine.connect() as connection:
            query = select(
                _RUNS.c.payload, _RUNS.c.owner, _RUNS.c.status
            ).where(_RUNS.c.run_id == run_id)
            row = connection.execute(query).first()
        if row is None:
            return None
        if owner is not None and row.owner != owner:
            return None
        document = json.loads(row.payload)
        document["status"] = row.status
        return document

    def owner_of(self, run_id: str) -> str | None:
        with self._lock, self.engine.connect() as connection:
            row = connection.execute(
                select(_RUNS.c.owner).where(_RUNS.c.run_id == run_id)
            ).first()
        return None if row is None else row.owner

    def get_status(self, run_id: str, owner: str | None = None) -> str | None:
        with self._lock, self.engine.connect() as connection:
            query = select(_RUNS.c.status, _RUNS.c.owner).where(_RUNS.c.run_id == run_id)
            row = connection.execute(query).first()
        if row is None or (owner is not None and row.owner != owner):
            return None
        return row.status

    def find_by_idempotency(self, owner: str, key: str) -> dict[str, Any] | None:
        with self._lock, self.engine.connect() as connection:
            row = connection.execute(
                select(_RUNS.c.payload, _RUNS.c.status).where(
                    _RUNS.c.owner == owner,
                    _RUNS.c.idempotency_key == key,
                )
            ).first()
        if row is None:
            return None
        document = json.loads(row.payload)
        document["status"] = row.status
        return document

    def list_runs(
        self,
        *,
        owner: str | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return run summaries for a workbench history list.

        The full payload stays in storage; only fields the UI needs are
        returned. Ownership filtering is applied in SQL so a caller cannot
        page across another principal's runs.
        """

        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        query = select(
            _RUNS.c.run_id,
            _RUNS.c.request_id,
            _RUNS.c.owner,
            _RUNS.c.status,
            _RUNS.c.question,
            _RUNS.c.created_at,
            _RUNS.c.updated_at,
            _RUNS.c.payload,
        )
        if owner is not None:
            query = query.where(_RUNS.c.owner == owner)
        if status is not None:
            query = query.where(_RUNS.c.status == status)
        query = query.order_by(_RUNS.c.updated_at.desc()).limit(limit).offset(offset)
        with self._lock, self.engine.connect() as connection:
            rows = connection.execute(query).all()
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                document = json.loads(row.payload)
            except (TypeError, ValueError):
                document = {}
            items.append(
                {
                    "run_id": row.run_id,
                    "request_id": row.request_id,
                    "owner": row.owner,
                    "status": row.status,
                    "question": row.question,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                    "stop_reason": document.get("stop_reason"),
                    "error_count": len(document.get("errors") or []),
                }
            )
        return items

    def update_status(
        self,
        run_id: str,
        status: str,
        *,
        owner: str | None = None,
        payload: dict[str, Any] | None = None,
        fence: RunClaim | None = None,
    ) -> bool:
        values: dict[str, Any] = {"status": status, "updated_at": _now()}
        if payload is not None:
            values["payload"] = json.dumps(payload, ensure_ascii=False, default=str)
        statement = update(_RUNS).where(_RUNS.c.run_id == run_id)
        if owner is not None:
            statement = statement.where(_RUNS.c.owner == owner)
        if fence is not None:
            statement = statement.where(
                _RUNS.c.claim_owner == fence.worker_id,
                _RUNS.c.claim_version == fence.claim_version,
            )
        with self._lock, self.engine.begin() as connection:
            result = connection.execute(statement.values(**values))
        if fence is not None and not result.rowcount:
            raise RunClaimLost(
                f"worker {fence.worker_id!r} no longer owns run {run_id!r}"
            )
        return bool(result.rowcount)

    def mark_interrupted(self) -> int:
        """A restarted process cannot resume an in-flight LLM loop.

        Only rows without a live lease are touched. A second API process may
        legitimately own a run, so marking every ``running`` row would corrupt
        a healthy multi-process deployment.
        """

        now = _now_precise()
        with self._lock, self.engine.begin() as connection:
            rows = connection.execute(
                select(_RUNS.c.run_id, _RUNS.c.payload).where(
                    _RUNS.c.status.in_(["queued", "running", "cancelling"]),
                    or_(
                        _RUNS.c.lease_expires_at.is_(None),
                        _RUNS.c.lease_expires_at < now,
                    ),
                )
            ).all()
            for row in rows:
                payload = json.loads(row.payload)
                payload["status"] = "interrupted"
                payload["stop_reason"] = "process_restart"
                connection.execute(
                    update(_RUNS)
                    .where(
                        _RUNS.c.run_id == row.run_id,
                        or_(
                            _RUNS.c.lease_expires_at.is_(None),
                            _RUNS.c.lease_expires_at < now,
                        ),
                    )
                    .values(
                        status="interrupted",
                        updated_at=now,
                        claim_owner=None,
                        lease_expires_at=None,
                        payload=json.dumps(payload, ensure_ascii=False, default=str),
                    )
                )
        return len(rows)

    # -- claim / lease / fencing ---------------------------------------
    def acquire_claim(
        self,
        run_id: str,
        worker_id: str,
        lease_seconds: float,
        *,
        allowed_statuses: Iterable[str],
    ) -> ClaimAttempt:
        """Atomically acquire an expired or unowned run.

        The conditional ``UPDATE`` is the lock: two workers racing on the same
        row cannot both match ``lease_expires_at IS NULL OR < now``. The
        returned ``claim_version`` is the fencing token for every later write.
        """

        now = _now_precise()
        expires = _iso_at(lease_seconds)
        statuses = sorted(set(allowed_statuses))
        with self._lock, self.engine.begin() as connection:
            result = connection.execute(
                update(_RUNS)
                .where(
                    _RUNS.c.run_id == run_id,
                    _RUNS.c.status.in_(statuses),
                    or_(
                        _RUNS.c.lease_expires_at.is_(None),
                        _RUNS.c.lease_expires_at < now,
                    ),
                )
                .values(
                    claim_owner=worker_id,
                    claimed_at=now,
                    lease_expires_at=expires,
                    claim_version=_RUNS.c.claim_version + 1,
                    updated_at=now,
                )
            )
            if result.rowcount:
                row = connection.execute(
                    select(
                        _RUNS.c.claim_version, _RUNS.c.lease_expires_at
                    ).where(_RUNS.c.run_id == run_id)
                ).first()
                return ClaimAttempt(
                    RunClaim(
                        run_id=run_id,
                        worker_id=worker_id,
                        claim_version=int(row.claim_version),
                        lease_expires_at=str(row.lease_expires_at),
                    ),
                    "claimed",
                    str(row.lease_expires_at),
                )
            row = connection.execute(
                select(
                    _RUNS.c.status,
                    _RUNS.c.claim_owner,
                    _RUNS.c.lease_expires_at,
                ).where(_RUNS.c.run_id == run_id)
            ).first()
        if row is None:
            return ClaimAttempt(None, "not_found")
        if row.lease_expires_at and row.lease_expires_at >= now:
            return ClaimAttempt(
                None, "lease_held", row.lease_expires_at, row.claim_owner
            )
        return ClaimAttempt(
            None, "not_resumable", row.lease_expires_at, row.claim_owner
        )

    def renew_claim(self, claim: RunClaim, lease_seconds: float) -> bool:
        expires = _iso_at(lease_seconds)
        with self._lock, self.engine.begin() as connection:
            result = connection.execute(
                update(_RUNS)
                .where(
                    _RUNS.c.run_id == claim.run_id,
                    _RUNS.c.claim_owner == claim.worker_id,
                    _RUNS.c.claim_version == claim.claim_version,
                )
                .values(lease_expires_at=expires, updated_at=_now_precise())
            )
        return bool(result.rowcount)

    def release_claim(self, claim: RunClaim) -> bool:
        with self._lock, self.engine.begin() as connection:
            result = connection.execute(
                update(_RUNS)
                .where(
                    _RUNS.c.run_id == claim.run_id,
                    _RUNS.c.claim_owner == claim.worker_id,
                    _RUNS.c.claim_version == claim.claim_version,
                )
                .values(claim_owner=None, lease_expires_at=None)
            )
        return bool(result.rowcount)

    def claim_state(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, self.engine.connect() as connection:
            row = connection.execute(
                select(
                    _RUNS.c.claim_owner,
                    _RUNS.c.claim_version,
                    _RUNS.c.claimed_at,
                    _RUNS.c.lease_expires_at,
                ).where(_RUNS.c.run_id == run_id)
            ).first()
        if row is None:
            return None
        return {
            "claim_owner": row.claim_owner,
            "claim_version": int(row.claim_version or 0),
            "claimed_at": row.claimed_at,
            "lease_expires_at": row.lease_expires_at,
        }

    # -- lifecycle -----------------------------------------------------
    def lifecycle_state(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, self.engine.connect() as connection:
            row = connection.execute(
                select(
                    _RUNS.c.created_at,
                    _RUNS.c.execution_seconds,
                    _RUNS.c.last_resumed_at,
                    _RUNS.c.resume_count,
                ).where(_RUNS.c.run_id == run_id)
            ).first()
        if row is None:
            return None
        return {
            "created_at": row.created_at,
            "execution_seconds": float(row.execution_seconds or 0.0),
            "last_resumed_at": row.last_resumed_at,
            "resume_count": int(row.resume_count or 0),
        }

    def record_execution(
        self,
        claim: RunClaim,
        seconds: float,
        *,
        resumed: bool,
    ) -> float:
        """Accumulate one attempt's wall-clock time against the whole run."""

        now = _now_precise()
        values: dict[str, Any] = {
            "execution_seconds": _RUNS.c.execution_seconds + max(0.0, seconds)
        }
        if resumed:
            values["last_resumed_at"] = now
            values["resume_count"] = _RUNS.c.resume_count + 1
        with self._lock, self.engine.begin() as connection:
            result = connection.execute(
                update(_RUNS)
                .where(
                    _RUNS.c.run_id == claim.run_id,
                    _RUNS.c.claim_owner == claim.worker_id,
                    _RUNS.c.claim_version == claim.claim_version,
                )
                .values(**values)
            )
            if not result.rowcount:
                raise RunClaimLost(
                    f"worker {claim.worker_id!r} no longer owns run {claim.run_id!r}"
                )
            row = connection.execute(
                select(_RUNS.c.execution_seconds).where(
                    _RUNS.c.run_id == claim.run_id
                )
            ).first()
        return float(row.execution_seconds or 0.0)

    # -- tool idempotency ----------------------------------------------
    @staticmethod
    def _owns(connection, claim: RunClaim) -> bool:
        row = connection.execute(
            select(_RUNS.c.run_id).where(
                _RUNS.c.run_id == claim.run_id,
                _RUNS.c.claim_owner == claim.worker_id,
                _RUNS.c.claim_version == claim.claim_version,
            )
        ).first()
        return row is not None

    def get_tool_result(
        self, run_id: str, call_key: str
    ) -> dict[str, Any] | None:
        """Return the completed result for a logical tool call, if any.

        ``call_key`` is derived from the tool name and canonical arguments, so
        it stays stable across a process restart. The result is persisted only
        after the tool completed; an in-flight call has no row and is safe to
        execute again.
        """

        with self._lock, self.engine.connect() as connection:
            row = connection.execute(
                select(_TOOL_RESULTS.c.result_json).where(
                    _TOOL_RESULTS.c.run_id == run_id,
                    _TOOL_RESULTS.c.call_key == call_key,
                )
            ).first()
        return None if row is None else json.loads(row.result_json)

    def put_tool_result(
        self,
        *,
        run_id: str,
        call_key: str,
        tool_name: str,
        call_id: str,
        status: str,
        result: dict[str, Any],
        fence: RunClaim | None = None,
    ) -> None:
        values = {
            "run_id": run_id,
            "call_key": call_key,
            "tool_name": tool_name,
            "call_id": call_id,
            "status": status,
            "result_json": json.dumps(result, ensure_ascii=False, default=str),
            "created_at": _now(),
        }
        with self._lock, self.engine.begin() as connection:
            if fence is not None and not self._owns(connection, fence):
                raise RunClaimLost(
                    f"worker {fence.worker_id!r} no longer owns run {run_id!r}"
                )
            existing = connection.execute(
                select(_TOOL_RESULTS.c.call_key).where(
                    _TOOL_RESULTS.c.run_id == run_id,
                    _TOOL_RESULTS.c.call_key == call_key,
                )
            ).first()
            if existing is None:
                connection.execute(_TOOL_RESULTS.insert().values(**values))
            else:
                connection.execute(
                    update(_TOOL_RESULTS)
                    .where(
                        _TOOL_RESULTS.c.run_id == run_id,
                        _TOOL_RESULTS.c.call_key == call_key,
                    )
                    .values(**values)
                )

    def count_tool_results(self, run_id: str) -> int:
        with self._lock, self.engine.connect() as connection:
            rows = connection.execute(
                select(_TOOL_RESULTS.c.call_key).where(
                    _TOOL_RESULTS.c.run_id == run_id
                )
            ).all()
        return len(rows)

    # -- audit / trace -------------------------------------------------
    def append_event(
        self,
        *,
        event_type: str,
        run_id: str | None = None,
        request_id: str | None = None,
        principal_id: str | None = None,
        tool_call_id: str | None = None,
        detail: dict[str, Any] | None = None,
        fence: RunClaim | None = None,
    ) -> str:
        event_id = str(uuid.uuid4())
        with self._lock, self.engine.begin() as connection:
            if fence is not None and not self._owns(connection, fence):
                raise RunClaimLost(
                    f"worker {fence.worker_id!r} no longer owns run {fence.run_id!r}"
                )
            connection.execute(
                _EVENTS.insert().values(
                    event_id=event_id,
                    run_id=run_id,
                    request_id=request_id,
                    principal_id=principal_id,
                    event_type=event_type,
                    tool_call_id=tool_call_id,
                    detail=json.dumps(detail or {}, ensure_ascii=False, default=str),
                    created_at=_now_precise(),
                )
            )
        return event_id

    def list_events(
        self, run_id: str, owner: str | None = None
    ) -> list[dict[str, Any]]:
        if owner is not None:
            document = self.get(run_id, owner=owner)
            if document is None:
                return []
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(_EVENTS)
                .where(_EVENTS.c.run_id == run_id)
                .order_by(_EVENTS.c.created_at, _EVENTS.c.event_id)
            ).all()
        return [
            {
                "event_id": row.event_id,
                "run_id": row.run_id,
                "request_id": row.request_id,
                "principal_id": row.principal_id,
                "event_type": row.event_type,
                "tool_call_id": row.tool_call_id,
                "detail": json.loads(row.detail),
                "created_at": row.created_at,
            }
            for row in rows
        ]

    def list_security_events(
        self,
        *,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return run-independent security events for the admin audit view."""

        from ..security.audit import SECURITY_EVENT_TYPES

        query = select(_EVENTS).where(
            _EVENTS.c.run_id.is_(None),
            _EVENTS.c.event_type.in_(sorted(SECURITY_EVENT_TYPES)),
        )
        if event_type is not None:
            if event_type not in SECURITY_EVENT_TYPES:
                return []
            query = query.where(_EVENTS.c.event_type == event_type)
        query = query.order_by(_EVENTS.c.created_at.desc()).limit(max(1, min(limit, 1000)))
        with self.engine.connect() as connection:
            rows = connection.execute(query).all()
        return [
            {
                "event_id": row.event_id,
                "request_id": row.request_id,
                "principal_id": row.principal_id,
                "event_type": row.event_type,
                "detail": json.loads(row.detail),
                "created_at": row.created_at,
            }
            for row in rows
        ]

    def purge_older_than(self, cutoff_iso: str) -> int:
        """Backward-compatible retention call: purge terminal runs by status.

        Batch 3 replaced the single cut-off with :meth:`cleanup_runs`, which
        deletes every child table in one transaction. This wrapper keeps older
        callers working while using the same safe path.
        """

        report = self.cleanup_runs(
            dry_run=False,
            retention_seconds={
                "completed": _seconds_since(cutoff_iso),
                "failed": _seconds_since(cutoff_iso),
                "cancelled": _seconds_since(cutoff_iso),
                "unfinished": _seconds_since(cutoff_iso),
            },
        )
        return int(report["run_count"])

    # -- cleanup -------------------------------------------------------
    def cleanup_runs(
        self,
        *,
        dry_run: bool,
        retention_seconds: dict[str, float],
        batch_limit: int = 1_000,
    ) -> dict[str, Any]:
        """Delete expired runs and every associated runtime row atomically.

        A run with a live lease is never selected. Long-unfinished runs are
        selected by creation age and only when their lease is absent/expired.
        Dry-run executes the same selection and counts, but rolls back.
        """

        started_at = _now_precise()
        now = datetime.now(timezone.utc)
        cutoffs: dict[str, str] = {}
        for status in ("completed", "failed", "cancelled", "unfinished"):
            seconds = float(retention_seconds.get(status, 0))
            cutoffs[status] = (now - timedelta(seconds=max(0.0, seconds))).isoformat(
                timespec="microseconds"
            )

        conditions = [
            _RUNS.c.status.in_(_COMPLETED_STATUSES)
            & (_RUNS.c.updated_at < cutoffs["completed"]),
            _RUNS.c.status.in_(_FAILED_STATUSES)
            & (_RUNS.c.updated_at < cutoffs["failed"]),
            _RUNS.c.status.in_(_CANCELLED_STATUSES)
            & (_RUNS.c.updated_at < cutoffs["cancelled"]),
            _RUNS.c.status.in_(_UNFINISHED_STATUSES)
            & (_RUNS.c.created_at < cutoffs["unfinished"])
            & or_(
                _RUNS.c.lease_expires_at.is_(None),
                _RUNS.c.lease_expires_at < _now_precise(),
            ),
        ]

        with self.engine.connect() as connection:
            transaction = connection.begin()
            try:
                ids = [
                    row.run_id
                    for row in connection.execute(
                        select(_RUNS.c.run_id)
                        .where(or_(*conditions))
                        .order_by(_RUNS.c.updated_at)
                        .limit(max(1, batch_limit))
                    ).all()
                ]
                if dry_run:
                    deleted = self._count_children(connection, ids)
                    transaction.rollback()
                else:
                    deleted = self._delete_children(connection, ids)
                    detail = {
                        "dry_run": False,
                        "cutoffs": cutoffs,
                        "run_count": len(ids),
                        "deleted": deleted,
                        "started_at": started_at,
                        "status": "ok",
                    }
                    connection.execute(
                        _EVENTS.insert().values(
                            event_id=str(uuid.uuid4()),
                            run_id=None,
                            request_id=None,
                            principal_id="system:cleanup",
                            event_type="run_cleanup",
                            tool_call_id=None,
                            detail=json.dumps(detail, ensure_ascii=False, default=str),
                            created_at=_now_precise(),
                        )
                    )
                    transaction.commit()
                return {
                    "dry_run": dry_run,
                    "cutoffs": cutoffs,
                    "run_count": len(ids),
                    "run_ids": ids[:20],
                    "deleted": deleted,
                    "started_at": started_at,
                    "finished_at": _now_precise(),
                    "status": "ok",
                    "failure_reason": None,
                }
            except Exception as exc:
                transaction.rollback()
                if not dry_run:
                    self._audit_cleanup_failure(started_at, exc)
                return {
                    "dry_run": dry_run,
                    "cutoffs": cutoffs,
                    "run_count": 0,
                    "run_ids": [],
                    "deleted": {},
                    "started_at": started_at,
                    "finished_at": _now_precise(),
                    "status": "failed",
                    "failure_reason": type(exc).__name__,
                }

    def _child_tables(self) -> list[tuple[str, Any, str]]:
        candidates = [
            ("agent_audit_events", _EVENTS, "run_id"),
            ("agent_report_approval_events", _APPROVAL_EVENTS, "run_id"),
            ("agent_report_approvals", _APPROVALS, "run_id"),
            ("agent_reports", _REPORTS, "run_id"),
            ("agent_findings", _FINDINGS, "run_id"),
            ("agent_evidence", _EVIDENCE, "run_id"),
            ("agent_checkpoint_writes", _CHECKPOINT_WRITES, "thread_id"),
            ("agent_checkpoints", _CHECKPOINTS, "thread_id"),
            ("agent_tool_results", _TOOL_RESULTS, "run_id"),
        ]
        inspector = inspect(self.engine)
        return [
            item for item in candidates if inspector.has_table(item[0])
        ]

    def _count_children(self, connection, ids: list[str]) -> dict[str, int]:
        counts: dict[str, int] = {"agent_runs": len(ids)}
        if not ids:
            return counts
        for name, table, key in self._child_tables():
            counts[name] = int(
                connection.execute(
                    select(func.count())
                    .select_from(table)
                    .where(table.c[key].in_(ids))
                ).scalar_one()
            )
        return counts

    def _delete_children(self, connection, ids: list[str]) -> dict[str, int]:
        deleted: dict[str, int] = {}
        if not ids:
            return {"agent_runs": 0}
        for name, table, key in self._child_tables():
            result = connection.execute(delete(table).where(table.c[key].in_(ids)))
            deleted[name] = int(result.rowcount or 0)
        result = connection.execute(delete(_RUNS).where(_RUNS.c.run_id.in_(ids)))
        deleted["agent_runs"] = int(result.rowcount or 0)
        return deleted

    def _audit_cleanup_failure(self, started_at: str, exc: Exception) -> None:
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    _EVENTS.insert().values(
                        event_id=str(uuid.uuid4()),
                        run_id=None,
                        request_id=None,
                        principal_id="system:cleanup",
                        event_type="run_cleanup",
                        tool_call_id=None,
                        detail=json.dumps(
                            {
                                "dry_run": False,
                                "started_at": started_at,
                                "status": "failed",
                                "failure_reason": type(exc).__name__,
                            },
                            ensure_ascii=False,
                        ),
                        created_at=_now_precise(),
                    )
                )
        except Exception:
            # A failed audit write must not hide the original cleanup failure.
            pass

    def detect_orphans(self, *, sample_limit: int = 20) -> dict[str, Any]:
        """Report child rows whose Run/Report/Checkpoint parent is missing."""

        run_ids = select(_RUNS.c.run_id)
        report_ids = select(_REPORTS.c.report_id)
        approval_ids = select(_APPROVALS.c.approval_id)
        checkpoint_keys = select(
            _CHECKPOINTS.c.thread_id,
            _CHECKPOINTS.c.checkpoint_ns,
            _CHECKPOINTS.c.checkpoint_id,
        )
        checks: list[tuple[str, Any, Any]] = [
            ("agent_audit_events_missing_run", _EVENTS, ~_EVENTS.c.run_id.in_(run_ids)),
            (
                "agent_tool_results_missing_run",
                _TOOL_RESULTS,
                ~_TOOL_RESULTS.c.run_id.in_(run_ids),
            ),
            (
                "agent_checkpoints_missing_run",
                _CHECKPOINTS,
                ~_CHECKPOINTS.c.thread_id.in_(run_ids),
            ),
            (
                "agent_checkpoint_writes_missing_run",
                _CHECKPOINT_WRITES,
                ~_CHECKPOINT_WRITES.c.thread_id.in_(run_ids),
            ),
            (
                "agent_evidence_missing_run",
                _EVIDENCE,
                ~_EVIDENCE.c.run_id.in_(run_ids),
            ),
            (
                "agent_findings_missing_run",
                _FINDINGS,
                ~_FINDINGS.c.run_id.in_(run_ids),
            ),
            (
                "agent_reports_missing_run",
                _REPORTS,
                ~_REPORTS.c.run_id.in_(run_ids),
            ),
            (
                "agent_report_approvals_missing_run",
                _APPROVALS,
                ~_APPROVALS.c.run_id.in_(run_ids),
            ),
            (
                "agent_report_approval_events_missing_run",
                _APPROVAL_EVENTS,
                ~_APPROVAL_EVENTS.c.run_id.in_(run_ids),
            ),
            (
                "agent_report_approvals_missing_report",
                _APPROVALS,
                ~_APPROVALS.c.report_id.in_(report_ids),
            ),
            (
                "agent_report_approval_events_missing_approval",
                _APPROVAL_EVENTS,
                ~_APPROVAL_EVENTS.c.approval_id.in_(approval_ids),
            ),
        ]
        counts: dict[str, int] = {}
        samples: dict[str, list[str]] = {}
        inspector = inspect(self.engine)
        with self.engine.connect() as connection:
            for name, table, condition in checks:
                if not inspector.has_table(table.name):
                    continue
                counts[name] = int(
                    connection.execute(
                        select(func.count()).select_from(table).where(condition)
                    ).scalar_one()
                )
                if counts[name]:
                    key = table.c.run_id if "run_id" in table.c else table.c.thread_id
                    samples[name] = [
                        str(value)
                        for value in connection.execute(
                            select(key).where(condition).limit(sample_limit)
                        ).scalars()
                    ]
        return {
            "counts": counts,
            "samples": samples,
            "total": sum(counts.values()),
            "checked_at": _now_precise(),
        }


def _seconds_since(cutoff_iso: str) -> float:
    try:
        cutoff = datetime.fromisoformat(cutoff_iso)
    except ValueError:
        return 0.0
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - cutoff).total_seconds())


_COMPLETED_STATUSES = (
    "completed",
    "completed_with_warnings",
    "no_evidence",
    "no_data",
)
_FAILED_STATUSES = (
    "failed",
    "llm_unavailable",
    "llm_timeout",
    "tool_failure",
    "budget_exhausted",
    "timeout",
    "invalid_action",
    "repeated_tool_call",
)
_CANCELLED_STATUSES = ("cancelled",)
_UNFINISHED_STATUSES = ("queued", "running", "cancelling", "interrupted")

_EVIDENCE = EVIDENCE_METADATA.tables["agent_evidence"]
_FINDINGS = EVIDENCE_METADATA.tables["agent_findings"]
_REPORTS = EVIDENCE_METADATA.tables["agent_reports"]
_APPROVALS = APPROVAL_METADATA.tables["agent_report_approvals"]
_APPROVAL_EVENTS = APPROVAL_METADATA.tables["agent_report_approval_events"]
_CHECKPOINTS = CHECKPOINT_METADATA.tables["agent_checkpoints"]
_CHECKPOINT_WRITES = CHECKPOINT_METADATA.tables["agent_checkpoint_writes"]

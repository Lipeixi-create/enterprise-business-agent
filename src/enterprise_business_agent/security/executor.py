from __future__ import annotations

import base64
import re
import time
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.sql.selectable import Select

from ..observability.setup import span
from .errors import ExecutionFailure, QueryErrorCode
from .semantic_guard import TrustedSelect
from .sql_guard import GuardedQuery, SQLGuard

_READ_ONLY_PREFIX = re.compile(r"(?is)^\s*(select|with)\b")


class QueryExecutionResult(BaseModel):
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: float = 0.0
    sql_hash: str
    tables: list[str] = Field(default_factory=list)
    dialect: str
    limit: int
    readonly: bool = True


def _set_attributes(span: Any, values: dict[str, Any]) -> None:
    if span is None:
        return
    try:
        for key, value in values.items():
            span.set_attribute(key, value)
    except Exception:  # noqa: BLE001 - tracing must never break execution
        return


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    return str(value)


class SafeQueryExecutor:
    """Execute only already-guarded SELECT statements in a read-only transaction."""

    def __init__(
        self,
        engine: Engine,
        *,
        max_rows: int = 200,
        timeout_ms: int = 15_000,
        audit: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.engine = engine
        self.dialect = engine.dialect.name
        self.max_rows = max_rows
        self.timeout_ms = timeout_ms
        self.audit = audit
        self._is_sqlite = self.dialect == "sqlite"

    def _write_audit(self, payload: dict[str, Any]) -> None:
        if self.audit is None:
            return
        try:
            self.audit(payload)
        except Exception:
            # Auditing must never change query semantics.
            pass

    def _sqlite_driver_connection(self, connection):
        fairy = connection.connection
        return getattr(fairy, "dbapi_connection", None) or getattr(fairy, "driver_connection", None)

    def execute(self, guarded: GuardedQuery) -> QueryExecutionResult:
        if not _READ_ONLY_PREFIX.match(guarded.sql):
            raise ExecutionFailure(
                QueryErrorCode.READ_ONLY_VIOLATION,
                "executor only accepts SELECT statements",
                {"sql_hash": guarded.sql_hash},
                stage="executor",
            )

        return self._execute(
            sql_hash=guarded.sql_hash,
            tables=tuple(guarded.tables),
            dialect=guarded.dialect,
            limit=guarded.limit,
            runner=lambda connection: connection.execute(text(guarded.sql), guarded.params),
        )

    def execute_select(self, trusted: TrustedSelect) -> QueryExecutionResult:
        """Execute a structurally validated semantic Select on a read-only connection.

        This is the Agent-tool path. The caller cannot pass SQL text; the
        statement must be the SQLAlchemy Select object produced by
        QueryCompiler and independently validated by SemanticQueryGuard.
        """
        if not isinstance(trusted.statement, Select):
            raise ExecutionFailure(
                QueryErrorCode.READ_ONLY_VIOLATION,
                "structured executor only accepts SQLAlchemy Select statements",
                {"sql_hash": trusted.sql_hash},
                stage="executor",
            )
        return self._execute(
            sql_hash=trusted.sql_hash,
            tables=tuple(trusted.tables),
            dialect=trusted.dialect,
            limit=self.max_rows,
            runner=lambda connection: connection.execute(trusted.statement, trusted.params),
        )

    def _execute(
        self,
        *,
        sql_hash: str,
        tables: tuple[str, ...],
        dialect: str,
        limit: int,
        runner,
    ) -> QueryExecutionResult:
        with span(
            "db.query",
            db__system=dialect,
            db__tables=",".join(tables),
            db__limit=limit,
            sql__hash=sql_hash,
        ) as db_span:
            try:
                result = self._execute_guarded(
                    sql_hash=sql_hash,
                    tables=tables,
                    dialect=dialect,
                    limit=limit,
                    runner=runner,
                )
            except ExecutionFailure as exc:
                _set_attributes(db_span, {"db.error_code": exc.code.value})
                raise
            _set_attributes(
                db_span,
                {
                    "db.row_count": result.row_count,
                    "db.truncated": result.truncated,
                    "db.elapsed_ms": result.elapsed_ms,
                },
            )
            return result

    def _execute_guarded(
        self,
        *,
        sql_hash: str,
        tables: tuple[str, ...],
        dialect: str,
        limit: int,
        runner,
    ) -> QueryExecutionResult:
        started = time.monotonic()
        rows: list[dict[str, Any]] = []
        columns: list[str] = []
        truncated = False
        status = "ok"
        error_code: str | None = None
        try:
            with self.engine.connect() as connection:
                if self._is_sqlite:
                    self._prepare_sqlite(connection, started)
                else:
                    self._prepare_postgres(connection)
                try:
                    result = runner(connection)
                    columns = [str(item) for item in result.keys()]
                    fetched = result.mappings().fetchmany(self.max_rows + 1)
                    truncated = len(fetched) > self.max_rows
                    rows = [{key: _json_value(value) for key, value in row.items()} for row in fetched[: self.max_rows]]
                finally:
                    if self._is_sqlite:
                        self._clear_sqlite_progress(connection)
                        try:
                            connection.exec_driver_sql("PRAGMA query_only=OFF")
                        except DBAPIError:
                            pass
                    connection.rollback()
        except ExecutionFailure:
            raise
        except DBAPIError as exc:
            status = "error"
            error = self._map_dbapi_error(exc)
            error_code = error.code.value
            self._write_audit(
                {
                    "event": "sql_execution",
                    "status": status,
                    "error_code": error_code,
                    "sql_hash": sql_hash,
                    "tables": list(tables),
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                }
            )
            raise error from exc
        except SQLAlchemyError as exc:
            error = ExecutionFailure(
                QueryErrorCode.DATABASE_ERROR,
                "database execution failed",
                {"error_type": type(exc).__name__},
                stage="executor",
            )
            self._write_audit(
                {
                    "event": "sql_execution",
                    "status": "error",
                    "error_code": error.code.value,
                    "sql_hash": sql_hash,
                    "tables": list(tables),
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                }
            )
            raise error from exc

        elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        self._write_audit(
            {
                "event": "sql_execution",
                "status": status,
                "error_code": error_code,
                "sql_hash": sql_hash,
                "tables": list(tables),
                "row_count": len(rows),
                "truncated": truncated,
                "elapsed_ms": elapsed_ms,
            }
        )
        return QueryExecutionResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=elapsed_ms,
            sql_hash=sql_hash,
            tables=list(tables),
            dialect=dialect,
            limit=limit,
        )

    def _prepare_sqlite(self, connection, started: float) -> None:
        connection.exec_driver_sql("PRAGMA query_only=ON")
        connection.exec_driver_sql(f"PRAGMA busy_timeout={int(self.timeout_ms)}")
        dbapi = self._sqlite_driver_connection(connection)
        if dbapi is not None and hasattr(dbapi, "set_progress_handler"):
            deadline = started + self.timeout_ms / 1000

            def _progress() -> int:
                return 1 if time.monotonic() >= deadline else 0

            dbapi.set_progress_handler(_progress, 1_000)

    def _clear_sqlite_progress(self, connection) -> None:
        dbapi = self._sqlite_driver_connection(connection)
        if dbapi is not None and hasattr(dbapi, "set_progress_handler"):
            dbapi.set_progress_handler(None, 0)

    def _prepare_postgres(self, connection) -> None:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        connection.execute(text(f"SET LOCAL statement_timeout = {int(self.timeout_ms)}"))
        connection.execute(text("SET LOCAL lock_timeout = 3000"))

    def _map_dbapi_error(self, exc: DBAPIError) -> ExecutionFailure:
        original = exc.orig
        sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        message = str(original or exc)
        lowered = message.lower()

        if sqlstate == "57014" or "interrupted" in lowered or "query canceled" in lowered:
            return ExecutionFailure(
                QueryErrorCode.TIMEOUT,
                "query exceeded the execution timeout",
                {"sqlstate": sqlstate, "timeout_ms": self.timeout_ms},
                stage="executor",
            )
        if sqlstate == "42501" or "permission denied" in lowered:
            return ExecutionFailure(
                QueryErrorCode.PERMISSION_DENIED,
                "database permission denied for the read-only role",
                {"sqlstate": sqlstate},
                stage="executor",
            )
        if sqlstate == "25006" or "readonly database" in lowered or "read-only" in lowered:
            return ExecutionFailure(
                QueryErrorCode.READ_ONLY_VIOLATION,
                "the database rejected a write in a read-only transaction",
                {"sqlstate": sqlstate},
                stage="executor",
            )
        return ExecutionFailure(
            QueryErrorCode.DATABASE_ERROR,
            "database execution failed",
            {"sqlstate": sqlstate, "error_type": type(original).__name__},
            stage="executor",
        )

    def ping(self) -> bool:
        """Trusted health probe; bypasses Guard but still uses the read-only connection."""
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1")).scalar_one()
            return True
        except Exception:
            return False


class SecureQueryService:
    """The only supported path from candidate SQL to database rows."""

    def __init__(self, guard: SQLGuard, executor: SafeQueryExecutor):
        self.guard = guard
        self.executor = executor

    def run(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        dialect: str | None = None,
    ) -> QueryExecutionResult:
        guarded = self.guard.validate(sql, params, dialect or self.executor.dialect)
        return self.executor.execute(guarded)

    def run_guarded(self, guarded: GuardedQuery) -> QueryExecutionResult:
        return self.executor.execute(guarded)

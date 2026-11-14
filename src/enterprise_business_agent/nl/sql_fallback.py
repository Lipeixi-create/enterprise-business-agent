from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from ..analysis.contracts import AnalysisStatus
from ..config import Settings
from ..llm.client import LLMClient, LLMError, LLMInvalidResponse, LLMTimeout, LLMUnavailable
from ..llm.schemas import CandidateSQLPlan
from ..security import QueryError, QueryErrorCode, SecureQueryService
from .contracts import ErrorInfo, SQLQueryResult

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SQLFallbackRunner:
    """Optional, guarded Text-to-SQL path used only when the semantic layer cannot answer."""

    def __init__(
        self,
        settings: Settings,
        llm_client: LLMClient | None,
        query_service: SecureQueryService,
        dialect: str,
    ):
        self.settings = settings
        self.llm_client = llm_client
        self.query_service = query_service
        self.dialect = dialect
        self.max_attempts = 2

    def _coerce_params(self, params: dict[str, Any]) -> dict[str, Any]:
        coerced: dict[str, Any] = {}
        for key, value in params.items():
            if isinstance(value, str) and _ISO_DATE.match(value):
                try:
                    coerced[key] = date.fromisoformat(value)
                    continue
                except ValueError:
                    pass
            coerced[key] = value
        return coerced

    def _catalog(self) -> str:
        allowed = sorted(self.query_service.guard.allowed_tables)
        columns = {table: sorted(self.query_service.guard.allowed_columns.get(table, set())) for table in allowed}
        return json.dumps(columns, ensure_ascii=False)

    def run(
        self,
        question: str,
        *,
        previous_error: str | None = None,
        max_rows: int | None = None,
    ) -> tuple[SQLQueryResult | None, ErrorInfo | None]:
        if self.llm_client is None:
            return None, ErrorInfo(
                code="llm_unavailable",
                message="SQL fallback requires an LLM; use structured semantic analysis instead",
                stage="sql_fallback",
            )

        feedback = previous_error
        last_error: ErrorInfo | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                result = self.llm_client.complete_json(
                    system=(
                        "You generate exactly one read-only SQL SELECT for a business database. "
                        'Return JSON only: {"sql": "...", "params": {...}, "rationale": "..."}. '
                        "Use only the supplied tables and columns. Never use DML, DDL, PRAGMA, comments, "
                        "multiple statements, SELECT *, or literal values in filters. Use named :param "
                        "placeholders for every external value. Prefer aggregation and an explicit LIMIT."
                    ),
                    prompt=(
                        f"dialect: {self.dialect}\n"
                        f"tables_and_columns: {self._catalog()}\n"
                        f"question: {question}\n"
                        f"previous_guard_or_database_error: {feedback or 'none'}\n"
                        "Return one JSON object only."
                    ),
                )
                plan = CandidateSQLPlan.model_validate(result.data)
                params = self._coerce_params(plan.params)
                execution = self.query_service.run(plan.sql, params, self.dialect)
                rows = execution.rows
                truncated = execution.truncated
                if max_rows is not None and len(rows) > max_rows:
                    rows = rows[:max_rows]
                    truncated = True
                status = AnalysisStatus.OK if rows else AnalysisStatus.INSUFFICIENT_DATA
                return (
                    SQLQueryResult(
                        status=status,
                        columns=execution.columns,
                        rows=rows,
                        row_count=len(rows),
                        truncated=truncated,
                        sql_hash=execution.sql_hash,
                        tables=execution.tables,
                        explanation=(
                            "受控 SQL 补充查询：已通过 AST Guard、表白名单/列白名单校验，"
                            "并在只读事务中执行；结果未经过语义层指标口径解释。"
                        ),
                    ),
                    None,
                )
            except LLMTimeout as exc:
                return None, ErrorInfo(code=exc.code, message=str(exc), stage="sql_fallback")
            except LLMUnavailable as exc:
                return None, ErrorInfo(code=exc.code, message=str(exc), stage="sql_fallback")
            except LLMInvalidResponse as exc:
                last_error = ErrorInfo(code=exc.code, message=str(exc), stage="sql_fallback")
                feedback = str(exc)
            except LLMError as exc:
                return None, ErrorInfo(code=exc.code, message=str(exc), stage="sql_fallback")
            except QueryError as exc:
                last_error = ErrorInfo(
                    code=exc.code.value,
                    message=exc.message,
                    stage=exc.stage,
                    detail=exc.detail,
                )
                feedback = f"{exc.code.value}: {exc.message}"
                if exc.code in {QueryErrorCode.TIMEOUT, QueryErrorCode.PERMISSION_DENIED}:
                    return None, last_error
            except Exception as exc:
                last_error = ErrorInfo(
                    code="sql_fallback_error",
                    message="SQL fallback failed",
                    stage="sql_fallback",
                    detail={"error_type": type(exc).__name__},
                )
                feedback = str(exc)

        return None, last_error

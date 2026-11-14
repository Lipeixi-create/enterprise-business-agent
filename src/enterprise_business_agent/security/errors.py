from __future__ import annotations

from enum import Enum
from typing import Any


class QueryErrorCode(str, Enum):
    INVALID_SQL = "invalid_sql"
    MULTIPLE_STATEMENTS = "multiple_statements"
    FORBIDDEN_STATEMENT = "forbidden_statement"
    FORBIDDEN_EXPRESSION = "forbidden_expression"
    FORBIDDEN_FUNCTION = "forbidden_function"
    UNKNOWN_TABLE = "unknown_table"
    UNKNOWN_COLUMN = "unknown_column"
    SELECT_STAR = "select_star"
    UNBOUND_VALUE = "unbound_value"
    INVALID_PARAMETER = "invalid_parameter"
    LIMIT_NOT_LITERAL = "limit_not_literal"
    OFFSET_EXCEEDED = "offset_exceeded"
    NO_TABLE = "no_table"
    DIALECT_ERROR = "dialect_error"
    READ_ONLY_VIOLATION = "read_only_violation"
    TIMEOUT = "timeout"
    PERMISSION_DENIED = "permission_denied"
    ROW_LIMIT = "row_limit"
    DATABASE_ERROR = "database_error"


class QueryError(Exception):
    """Base class for structured query security and execution errors."""

    def __init__(
        self,
        code: QueryErrorCode | str,
        message: str,
        detail: dict[str, Any] | None = None,
        *,
        stage: str = "guard",
    ):
        self.code = code if isinstance(code, QueryErrorCode) else QueryErrorCode(code)
        self.message = message
        self.detail = detail or {}
        self.stage = stage
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "stage": self.stage,
            "detail": self.detail,
        }


class GuardRejection(QueryError):
    pass


class ExecutionFailure(QueryError):
    pass

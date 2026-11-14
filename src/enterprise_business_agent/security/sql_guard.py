from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from .. import models  # noqa: F401 - registers SQLAlchemy metadata
from ..db import Base
from .errors import GuardRejection, QueryErrorCode

_DIALECT_ALIASES = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "sqlite": "sqlite",
    "sqlite3": "sqlite",
}

_ALLOWED_TABLE_SCHEMAS = {"", "main", "public"}

_FORBIDDEN_NODE_NAMES = (
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Create",
    "Drop",
    "Alter",
    "TruncateTable",
    "Copy",
    "Transaction",
    "Commit",
    "Rollback",
    "Set",
    "Use",
    "Grant",
    "Command",
    "Pragma",
    "LoadData",
    "Into",
    "Lock",
    "Kill",
    "Analyze",
    "Refresh",
    "Describe",
    "Show",
    "Declare",
    "Returning",
    "Intersect",
    "Except",
)

_ALLOWED_FUNC_NODES = {
    "Abs",
    "And",
    "Avg",
    "Cast",
    "Case",
    "Coalesce",
    "Concat",
    "Count",
    "CumeDist",
    "DateTrunc",
    "DenseRank",
    "Extract",
    "FirstValue",
    "If",
    "Lag",
    "LastValue",
    "Lead",
    "Max",
    "Min",
    "NthValue",
    "Nullif",
    "Ntile",
    "Or",
    "PercentRank",
    "Rank",
    "Round",
    "RowNumber",
    "Sum",
    "TimeToStr",
    "TryCast",
    "TsOrDsToTimestamp",
}

_ALLOWED_ANONYMOUS_FUNCTIONS = {
    "concat",
    "date",
    "datetime",
    "greatest",
    "least",
    "length",
    "lower",
    "ltrim",
    "printf",
    "replace",
    "rtrim",
    "substr",
    "substring",
    "time",
    "trim",
    "upper",
}

_PARAM_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PYFORMAT_PATTERN = re.compile(r"%\(([A-Za-z_][A-Za-z0-9_]*)\)s")


@dataclass(frozen=True)
class GuardedQuery:
    sql: str
    params: dict[str, Any]
    tables: tuple[str, ...]
    columns: tuple[str, ...]
    limit: int
    offset: int
    dialect: str
    sql_hash: str
    original_sql: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _sql_hash(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _dialect_name(value: str) -> str:
    normalized = value.lower()
    if normalized not in _DIALECT_ALIASES:
        raise GuardRejection(
            QueryErrorCode.DIALECT_ERROR,
            f"unsupported SQL dialect: {value}",
            {"dialect": value},
        )
    return _DIALECT_ALIASES[normalized]


def _iter_nodes(tree: exp.Expression):
    return tree.walk()


def _func_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name or "").lower()
    return type(node).__name__


def _is_allowed_function(node: exp.Func) -> bool:
    if isinstance(node, exp.Anonymous):
        return str(node.name or "").lower() in _ALLOWED_ANONYMOUS_FUNCTIONS
    return type(node).__name__ in _ALLOWED_FUNC_NODES


def _inside_constant_context(node: exp.Expression) -> bool:
    parent = node.parent
    while parent is not None:
        if isinstance(parent, (exp.Limit, exp.Offset)):
            return True
        if isinstance(parent, exp.Func) and _is_allowed_function(parent):
            return True
        if isinstance(parent, (exp.Select, exp.Union)):
            break
        parent = parent.parent
    return False


def _literal_int(node: exp.Expression | None) -> int | None:
    if isinstance(node, exp.Literal) and node.is_number:
        try:
            return int(node.this)
        except (TypeError, ValueError):
            return None
    return None


def _cte_columns(cte: exp.CTE) -> set[str]:
    columns: set[str] = set()
    body = cte.this
    selects: list[exp.Select] = []
    if isinstance(body, exp.Select):
        selects.append(body)
    elif isinstance(body, exp.Union):
        for side in (body.this, body.expression):
            if isinstance(side, exp.Select):
                selects.append(side)
    for select in selects:
        for projection in select.expressions:
            if isinstance(projection, exp.Star):
                continue
            alias = projection.alias_or_name
            if alias:
                columns.add(alias.lower())
    return columns


class SQLGuard:
    """AST-based read-only SQL validator with explicit table/column/function whitelists."""

    def __init__(
        self,
        allowed_tables: set[str] | None = None,
        allowed_columns: dict[str, set[str]] | None = None,
        *,
        max_rows: int = 200,
        max_offset: int = 10_000,
        extra_functions: set[str] | None = None,
    ):
        if allowed_tables is None:
            allowed_tables = set(Base.metadata.tables.keys())
        if allowed_columns is None:
            allowed_columns = {name: set(table.columns.keys()) for name, table in Base.metadata.tables.items()}
        self.allowed_tables = {item.lower() for item in allowed_tables}
        self.allowed_columns = {
            name.lower(): {column.lower() for column in columns} for name, columns in allowed_columns.items()
        }
        self.max_rows = max_rows
        self.max_offset = max_offset
        self.extra_functions = {item.lower() for item in (extra_functions or set())}

    def _reject(self, code: QueryErrorCode, message: str, detail: dict[str, Any] | None = None) -> None:
        raise GuardRejection(code, message, detail)

    def validate(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        dialect: str = "sqlite",
    ) -> GuardedQuery:
        if not isinstance(sql, str) or not sql.strip():
            self._reject(QueryErrorCode.INVALID_SQL, "SQL is empty")
        if "--" in sql or "/*" in sql:
            self._reject(
                QueryErrorCode.INVALID_SQL,
                "SQL comments are not allowed",
            )
        write_dialect = _dialect_name(dialect)
        params = dict(params or {})

        try:
            statements = sqlglot.parse(sql, read=write_dialect)
        except ParseError as exc:
            self._reject(
                QueryErrorCode.INVALID_SQL,
                "SQL parse failed",
                {"error": str(exc), "dialect": write_dialect},
            )
        except Exception as exc:
            self._reject(
                QueryErrorCode.INVALID_SQL,
                "SQL parse failed",
                {"error": str(exc), "dialect": write_dialect},
            )

        statements = [statement for statement in statements if statement is not None]
        if len(statements) != 1:
            self._reject(
                QueryErrorCode.MULTIPLE_STATEMENTS,
                "exactly one SQL statement is allowed",
                {"statement_count": len(statements)},
            )
        tree = statements[0]
        if not isinstance(tree, (exp.Select, exp.Union)):
            self._reject(
                QueryErrorCode.FORBIDDEN_STATEMENT,
                f"only SELECT or WITH ... SELECT is allowed, got {type(tree).__name__}",
                {"statement_type": type(tree).__name__},
            )

        for node in _iter_nodes(tree):
            if type(node).__name__ in _FORBIDDEN_NODE_NAMES:
                self._reject(
                    QueryErrorCode.FORBIDDEN_STATEMENT,
                    f"forbidden SQL construct: {type(node).__name__}",
                    {"construct": type(node).__name__},
                )

        if isinstance(tree, exp.Select) and tree.args.get("into") is not None:
            self._reject(QueryErrorCode.FORBIDDEN_STATEMENT, "SELECT INTO is not allowed")

        cte_names = {str(cte.alias_or_name).lower() for cte in tree.find_all(exp.CTE) if cte.alias_or_name}
        cte_columns: dict[str, set[str]] = {
            str(cte.alias_or_name).lower(): _cte_columns(cte) for cte in tree.find_all(exp.CTE) if cte.alias_or_name
        }

        tables: set[str] = set()
        alias_to_source: dict[str, str | None] = {}
        for table in tree.find_all(exp.Table):
            name = str(table.name or "").lower()
            alias = str(table.alias_or_name or name).lower()
            if name in cte_names:
                alias_to_source[alias] = None
                continue
            schema = str(table.db or "").lower()
            catalog = str(table.catalog or "").lower()
            if schema not in _ALLOWED_TABLE_SCHEMAS or catalog:
                self._reject(
                    QueryErrorCode.UNKNOWN_TABLE,
                    f"table schema is not allowed: {table.sql()}",
                    {"table": name, "schema": schema, "catalog": catalog},
                )
            if name not in self.allowed_tables:
                self._reject(
                    QueryErrorCode.UNKNOWN_TABLE,
                    f"table is not in the read-only whitelist: {name}",
                    {"table": name},
                )
            tables.add(name)
            alias_to_source[alias] = name

        if not tables:
            self._reject(QueryErrorCode.NO_TABLE, "a read-only query must reference a whitelisted table")

        available_columns: set[str] = set()
        for table_name in tables:
            available_columns.update(self.allowed_columns.get(table_name, set()))
        for columns in cte_columns.values():
            available_columns.update(columns)
        for select in tree.find_all(exp.Select):
            for projection in select.expressions:
                if isinstance(projection, exp.Alias):
                    alias = projection.alias
                    if alias:
                        available_columns.add(alias.lower())

        seen_columns: list[str] = []
        for column in tree.find_all(exp.Column):
            name = str(column.name or "").lower()
            if not name or name == "*":
                self._reject(QueryErrorCode.SELECT_STAR, "SELECT * is not allowed")
            qualifier = str(column.table or "").lower().split(".")[-1]
            if qualifier:
                if qualifier not in alias_to_source:
                    self._reject(
                        QueryErrorCode.UNKNOWN_COLUMN,
                        f"unknown table alias or qualifier: {column.table}",
                        {"column": column.sql(), "qualifier": column.table},
                    )
                source = alias_to_source[qualifier]
                if source is None:
                    allowed = cte_columns.get(qualifier, set())
                else:
                    allowed = self.allowed_columns.get(source, set())
                if name not in allowed:
                    self._reject(
                        QueryErrorCode.UNKNOWN_COLUMN,
                        f"column is not in the read-only whitelist: {column.sql()}",
                        {"column": name, "table": source or qualifier},
                    )
            elif name not in available_columns:
                self._reject(
                    QueryErrorCode.UNKNOWN_COLUMN,
                    f"column is not in the read-only whitelist: {name}",
                    {"column": name},
                )
            seen_columns.append(column.sql(dialect=write_dialect))

        for star in tree.find_all(exp.Star):
            if isinstance(star.parent, exp.Count):
                continue
            self._reject(
                QueryErrorCode.SELECT_STAR,
                "SELECT * is not allowed; list explicit columns",
                {"expression": star.sql(dialect=write_dialect)},
            )

        for node in _iter_nodes(tree):
            if isinstance(node, exp.Parameter):
                self._reject(
                    QueryErrorCode.INVALID_PARAMETER,
                    "positional parameters are not allowed; use named :parameters",
                    {"parameter": node.sql(dialect=write_dialect)},
                )
            if isinstance(node, exp.Func) and not _is_allowed_function(node):
                self._reject(
                    QueryErrorCode.FORBIDDEN_FUNCTION,
                    f"function is not in the whitelist: {_func_name(node)}",
                    {"function": _func_name(node)},
                )
            if isinstance(node, exp.Literal):
                if _inside_constant_context(node):
                    continue
                self._reject(
                    QueryErrorCode.UNBOUND_VALUE,
                    "literal values must use named bind parameters",
                    {"literal": node.sql(dialect=write_dialect)},
                )

        placeholders: dict[str, str] = {}
        for placeholder in tree.find_all(exp.Placeholder):
            name = str(placeholder.name or "")
            if not _PARAM_PATTERN.match(name):
                self._reject(
                    QueryErrorCode.INVALID_PARAMETER,
                    f"invalid named parameter: {name or '?'}",
                    {"parameter": name or "?"},
                )
            if name not in params:
                self._reject(
                    QueryErrorCode.INVALID_PARAMETER,
                    f"missing value for bind parameter: {name}",
                    {"parameter": name},
                )
            placeholders[name] = name
        extra = sorted(set(params) - set(placeholders))
        if extra:
            self._reject(
                QueryErrorCode.INVALID_PARAMETER,
                "unused bind parameters were supplied",
                {"parameters": extra},
            )
        for name, value in params.items():
            if isinstance(value, (list, tuple, set, dict)):
                self._reject(
                    QueryErrorCode.INVALID_PARAMETER,
                    "list/dict parameters are not supported by the safe executor; use one scalar per parameter",
                    {"parameter": name},
                )

        fetch_limit = self.max_rows + 1
        limit_node = tree.args.get("limit")
        if limit_node is None:
            tree.args["limit"] = exp.Limit(expression=exp.Literal.number(fetch_limit))
            limit_value = fetch_limit
        else:
            if getattr(limit_node, "args", {}).get("limit_options") is not None:
                self._reject(QueryErrorCode.LIMIT_NOT_LITERAL, "LIMIT ALL is not allowed")
            value = _literal_int(limit_node.expression)
            if value is None:
                self._reject(
                    QueryErrorCode.LIMIT_NOT_LITERAL,
                    "LIMIT must be a numeric literal so the hard maximum can be enforced",
                )
            if value < 0:
                self._reject(QueryErrorCode.INVALID_SQL, "LIMIT must not be negative")
            limit_value = min(value, fetch_limit)
            if value != limit_value:
                limit_node.args["expression"] = exp.Literal.number(limit_value)

        offset_node = tree.args.get("offset")
        offset_value = 0
        if offset_node is not None:
            value = _literal_int(offset_node.expression)
            if value is None:
                self._reject(
                    QueryErrorCode.INVALID_SQL,
                    "OFFSET must be a numeric literal so the hard maximum can be enforced",
                )
            if value < 0 or value > self.max_offset:
                self._reject(
                    QueryErrorCode.OFFSET_EXCEEDED,
                    f"OFFSET exceeds the maximum of {self.max_offset}",
                    {"offset": value, "max_offset": self.max_offset},
                )
            offset_value = value

        try:
            rendered = tree.sql(dialect=write_dialect)
        except Exception as exc:
            self._reject(
                QueryErrorCode.DIALECT_ERROR,
                "failed to render SQL for the target dialect",
                {"error": str(exc), "dialect": write_dialect},
            )
        if write_dialect == "postgres":
            rendered = _PYFORMAT_PATTERN.sub(r":\1", rendered)

        return GuardedQuery(
            sql=rendered,
            params=params,
            tables=tuple(sorted(tables)),
            columns=tuple(sorted(set(seen_columns))),
            limit=limit_value,
            offset=offset_value,
            dialect=write_dialect,
            sql_hash=_sql_hash(rendered),
            original_sql=sql,
            metadata={
                "placeholders": sorted(placeholders),
                "ctes": sorted(cte_names),
            },
        )

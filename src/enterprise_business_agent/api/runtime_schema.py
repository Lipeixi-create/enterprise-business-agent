from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..agent.checkpoint import CHECKPOINT_METADATA, create_checkpoint_schema, drop_checkpoint_schema
from ..evidence.approval import APPROVAL_METADATA, create_approval_schema, drop_approval_schema
from ..evidence.schema import EVIDENCE_METADATA, create_schema, drop_schema
from .run_store import (
    RUN_STORE_METADATA,
    create_run_store_schema,
    drop_run_store_schema,
)

_ROLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_DATABASE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}$")

_RUNTIME_METADATA = (
    RUN_STORE_METADATA,
    EVIDENCE_METADATA,
    CHECKPOINT_METADATA,
    APPROVAL_METADATA,
)


def runtime_table_names() -> list[str]:
    """Every table the runtime role may read or write."""

    return sorted({table.name for metadata in _RUNTIME_METADATA for table in metadata.tables.values()})


def create_runtime_schema(engine: Engine) -> None:
    """Create every table the runtime role reads or writes.

    Run this with the setup role before starting the API with the least-
    privilege runtime role. The runtime role never issues DDL.
    """

    create_schema(engine)
    create_run_store_schema(engine)
    create_checkpoint_schema(engine)
    create_approval_schema(engine)


def drop_runtime_schema(engine: Engine) -> None:
    drop_approval_schema(engine)
    drop_checkpoint_schema(engine)
    drop_run_store_schema(engine)
    drop_schema(engine)


def grant_runtime_role(engine: Engine, role: str, password: str | None = None) -> list[str]:
    """Grant DML on runtime tables to a least-privilege PostgreSQL role.

    When ``password`` is supplied the role is created or its password rotated.
    The caller must never print the password. Returns the executed statements
    for auditability in tests and reports.
    """

    if engine.url.get_backend_name() != "postgresql":  # pragma: no cover
        return []
    if not _ROLE.match(role):
        raise ValueError("runtime role must be a simple SQL identifier")
    database = engine.url.database or ""
    if not _DATABASE.match(database):
        raise ValueError("database name must be a simple SQL identifier")

    statements: list[str] = []
    if password is not None:
        escaped = password.replace("'", "''")
        statements.append(
            f"DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
            f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
            f"PASSWORD '{escaped}'; "
            f"ELSE "
            f"ALTER ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
            f"PASSWORD '{escaped}'; "
            f"END IF; END $$"
        )
        statements.extend(
            [
                f"GRANT CONNECT ON DATABASE {database} TO {role}",
                f"ALTER ROLE {role} SET default_transaction_read_only = off",
                f"ALTER ROLE {role} SET statement_timeout = '30s'",
                f"ALTER ROLE {role} SET lock_timeout = '10s'",
                f"ALTER ROLE {role} SET idle_in_transaction_session_timeout = '30s'",
                f"ALTER ROLE {role} SET search_path = public",
            ]
        )
    table_list = ", ".join(f"public.{name}" for name in runtime_table_names())
    statements.extend(
        [
            f"GRANT USAGE ON SCHEMA public TO {role}",
            f"REVOKE CREATE ON SCHEMA public FROM {role}",
            f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {role}",
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table_list} TO {role}",
        ]
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
    return statements


__all__ = [
    "create_runtime_schema",
    "drop_runtime_schema",
    "grant_runtime_role",
    "runtime_table_names",
]

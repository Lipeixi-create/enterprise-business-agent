#!/usr/bin/env python
"""Create or update the independent PostgreSQL read-only application role.

Usage:
    ADMIN_DATABASE_URL=postgresql://admin@localhost:5432/enterprise_business_agent \
    EBA_READONLY_PASSWORD='...' \
    python scripts/create_readonly_role.py

The password is read from EBA_READONLY_PASSWORD and is never printed or logged.
The script requires an administrative connection only for setup; the API must
use READONLY_DATABASE_URL with this role.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

from psycopg import sql
from sqlalchemy.engine import make_url


def _normalize_dsn(value: str) -> str:
    return re.sub(r"^postgresql\+[a-zA-Z0-9_]+://", "postgresql://", value, count=1)


def _statements(role: str, database: str, password: str) -> list[sql.Composed]:
    role_id = sql.Identifier(role)
    database_id = sql.Identifier(database)
    return [
        sql.SQL(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {role_literal}) THEN "
            "CREATE ROLE {role} LOGIN; "
            "END IF; "
            "END $$;"
        ).format(role_literal=sql.Literal(role), role=role_id),
        sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(role_id, sql.Literal(password)),
        sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(role_id),
        sql.SQL("ALTER ROLE {} SET statement_timeout = '15s'").format(role_id),
        sql.SQL("ALTER ROLE {} SET lock_timeout = '3s'").format(role_id),
        sql.SQL("ALTER ROLE {} SET idle_in_transaction_session_timeout = '30s'").format(role_id),
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database_id, role_id),
        sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role_id),
        sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(role_id),
        sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {}").format(role_id),
        sql.SQL(
            "REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
            "ON ALL TABLES IN SCHEMA public FROM {}"
        ).format(role_id),
        sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(role_id),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", default=os.getenv("EBA_READONLY_ROLE", "eba_readonly"))
    parser.add_argument("--database", default=None, help="Defaults to the database in the admin URL")
    parser.add_argument("--dry-run", action="store_true", help="Print statements with a masked password")
    args = parser.parse_args()

    admin_url = os.getenv("ADMIN_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not admin_url:
        print("ERROR: set ADMIN_DATABASE_URL (or DATABASE_URL) to a PostgreSQL admin connection", file=sys.stderr)
        return 2
    parsed = make_url(admin_url)
    if parsed.get_backend_name() != "postgresql":
        print("ERROR: readonly role setup is only for PostgreSQL; SQLite uses mode=ro/query_only", file=sys.stderr)
        return 2

    password = os.getenv("EBA_READONLY_PASSWORD")
    if not password:
        print("ERROR: set EBA_READONLY_PASSWORD; it is never written to disk by this script", file=sys.stderr)
        return 2
    if len(password) < 12:
        print("ERROR: EBA_READONLY_PASSWORD must be at least 12 characters", file=sys.stderr)
        return 2

    database = args.database or parsed.database
    if not database:
        print("ERROR: database name is missing from the admin URL; pass --database", file=sys.stderr)
        return 2

    statements = _statements(args.role, database, password)
    if args.dry_run:
        masked = _statements(args.role, database, "***")
        for item in masked:
            print(item.as_string() + ";")
        return 0

    import psycopg

    with psycopg.connect(_normalize_dsn(admin_url), autocommit=True) as connection:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
    print(f"read-only role '{args.role}' configured for database '{database}' (password not shown)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

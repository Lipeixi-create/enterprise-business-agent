"""Create and grant the PostgreSQL runtime schema.

Run this with the setup role (``EBA_RUNTIME_SETUP_URL`` or ``DATABASE_URL``).
The setup role owns the tables and performs all DDL; the API connects with the
separate runtime role named by ``EBA_RUNTIME_ROLE`` (default ``eba_runtime``).

The runtime role receives DML on the ``agent_*`` tables only. It never receives
DDL rights, superuser, CREATEDB or CREATEROLE. The password is read from
``EBA_RUNTIME_PASSWORD`` and is never printed or logged.

SQLite is created directly, which keeps local development one command:

    python scripts/init_runtime_store.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import create_engine, inspect  # noqa: E402

from enterprise_business_agent.api.runtime_schema import (  # noqa: E402
    create_runtime_schema,
    grant_runtime_role,
    runtime_table_names,
)


def _setup_url() -> str:
    url = (
        os.getenv("EBA_RUNTIME_SETUP_URL")
        or os.getenv("DATABASE_URL")
        or os.getenv("RUN_STORE_URL")
    )
    if not url:
        return "sqlite:///./enterprise_business_agent_runs.db"
    return url


def main() -> int:
    url = _setup_url()
    engine = create_engine(url, future=True, pool_pre_ping=True)
    try:
        create_runtime_schema(engine)
        names = sorted(inspect(engine).get_table_names())
        missing = sorted(set(runtime_table_names()) - set(names))
        if missing:
            print(f"runtime schema incomplete: {missing}", file=sys.stderr)
            return 1
        print(f"runtime schema ready ({len(runtime_table_names())} tables)")
        if engine.url.get_backend_name() == "sqlite":
            print("backend: sqlite (local development)")
            return 0

        role = os.getenv("EBA_RUNTIME_ROLE", "eba_runtime")
        password = os.getenv("EBA_RUNTIME_PASSWORD")
        if not password:
            print(
                "EBA_RUNTIME_PASSWORD is required to grant the PostgreSQL "
                "runtime role",
                file=sys.stderr,
            )
            return 2
        grant_runtime_role(engine, role, password)
        print(f"runtime role granted: {role}")
        print("grants: SELECT/INSERT/UPDATE/DELETE on agent_* tables only; no DDL")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

"""Create or upgrade the Phase 5 evidence tables in the run store.

The run store predates Phase 5 and is created with ``MetaData.create_all``
rather than Alembic. This script applies the same idempotent upgrade against
``RUN_STORE_URL`` so an existing database with ``agent_runs`` gains the
evidence, finding and report tables without losing rows.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import create_engine, inspect  # noqa: E402

from enterprise_business_agent.config import _run_store_url  # noqa: E402
from enterprise_business_agent.evidence import create_schema  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=None,
        help="run store URL; defaults to RUN_STORE_URL or the local SQLite file",
    )
    args = parser.parse_args()
    url = args.url or _run_store_url()
    if url is None:
        print("run store is disabled (RUN_STORE_URL=none); nothing to migrate")
        return 1
    engine = create_engine(url, future=True)
    before = set(inspect(engine).get_table_names())
    create_schema(engine)
    after = set(inspect(engine).get_table_names())
    created = sorted(after - before)
    print(f"run store: {engine.url.get_backend_name()}")
    print(f"created tables: {', '.join(created) if created else 'none (already present)'}")
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse

from enterprise_business_agent.config import get_settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import make_engine
from enterprise_business_agent import models  # noqa: F401
from sqlalchemy.orm import Session


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate deterministic business demo data")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--reset", action="store_true", help="clear existing Phase 1 rows first")
    args = parser.parse_args()
    settings = get_settings(args.database_url)
    engine = make_engine(settings.database_url)
    with Session(engine) as session:
        counts = generate_data(session, settings.data_start, settings.data_months, args.reset)
    for name, count in sorted(counts.items()):
        print(f"{name}: {count}")


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse

from enterprise_business_agent.db import Base, make_engine
from enterprise_business_agent import models  # noqa: F401 - registers all models


def main() -> None:
    parser = argparse.ArgumentParser(description="Create Phase 1 tables")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args()
    from enterprise_business_agent.config import get_settings
    settings = get_settings(args.database_url)
    Base.metadata.create_all(make_engine(settings.database_url))
    from sqlalchemy.engine import make_url

    safe_url = make_url(settings.database_url).render_as_string(hide_password=True)
    print(f"initialized database: {safe_url}")


if __name__ == "__main__":
    main()

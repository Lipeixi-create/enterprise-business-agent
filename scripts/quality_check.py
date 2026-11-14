from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy.orm import Session

from enterprise_business_agent.config import get_settings
from enterprise_business_agent.db import make_engine
from enterprise_business_agent.quality import run_quality_checks


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Phase 1 data quality checks")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args()
    settings = get_settings(args.database_url)
    with Session(make_engine(settings.database_url)) as session:
        report = run_quality_checks(session, settings.data_start, settings.data_months)
    print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
    if not report.passed:
        sys.exit(1)


if __name__ == "__main__":
    main()

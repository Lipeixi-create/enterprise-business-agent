from __future__ import annotations

import argparse
import json
import sys

from enterprise_business_agent.config import get_settings
from enterprise_business_agent.db import make_engine
from enterprise_business_agent.semantic_validation import validate_semantic_against_engine


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate semantic_layer against the database schema")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args()
    settings = get_settings(args.database_url)
    errors = validate_semantic_against_engine(make_engine(settings.database_url))
    print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False, indent=2))
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()

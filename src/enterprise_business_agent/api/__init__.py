"""Lazy package exports.

Importing this package must not build the FastAPI app.  Deployment scripts
(``scripts/init_runtime_store.py``) import ``api.runtime_schema`` before the
runtime tables exist; an eager ``from .app import create_app`` would run
``create_app()`` at import time and fail on the missing schema.
"""

from typing import Any

__all__ = ["ResultStore", "create_app"]


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from .app import create_app

        return create_app
    if name == "ResultStore":
        from .store import ResultStore

        return ResultStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

"""Read-only production preflight checks.

The script never issues DDL or DML and never prints a password, API key, token
or full connection string. It inspects configuration, connects with the roles
already configured in the environment, and verifies privilege boundaries from
the database catalog.

Typical use:

    EBA_LOAD_DOTENV=true .venv/bin/python scripts/preflight.py
    .venv/bin/python scripts/preflight.py --json --api-base-url http://127.0.0.1:8000

Exit codes: 0 = no failure (warnings allowed), 1 = at least one failed check,
2 = command-line or configuration error.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from enterprise_business_agent.api.runtime_schema import (  # noqa: E402
    runtime_table_names,
)
from enterprise_business_agent.config import (  # noqa: E402
    Settings,
    get_settings,
    load_env_file,
)

OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"

_BUSINESS_TABLES = (
    "sales_fact",
    "marketing_fact",
    "finance_monthly",
    "hr_monthly",
    "purchase_fact",
    "inventory_snapshot",
    "products",
    "regions",
    "campaigns",
    "channels",
    "departments",
    "suppliers",
)
_CLAIM_COLUMNS = ("claim_owner", "claim_version", "claimed_at", "lease_expires_at")


@dataclass
class Check:
    name: str
    status: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.name,
            "status": self.status,
            "detail": self.detail,
            "evidence": self.evidence,
        }


def redact_url(url: str | None) -> str:
    """Describe a connection without exposing credentials."""

    if not url:
        return "<unset>"
    parsed = urlparse(url)
    if not parsed.scheme:
        return "<invalid-url>"
    if parsed.scheme.startswith("sqlite"):
        return url if url.startswith("sqlite:///") else "sqlite://<memory>"
    user = parsed.username or ""
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{user + '@' if user else ''}{host}{port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


def _connect_engine(url: str) -> Engine:
    return create_engine(url, future=True, pool_pre_ping=True)


# -- checks -----------------------------------------------------------------
def check_environment(settings: Settings, env_file: Path | None) -> list[Check]:
    checks: list[Check] = []
    if env_file is not None and env_file.exists():
        mode = stat.S_IMODE(env_file.stat().st_mode)
        world_or_group = bool(mode & (stat.S_IRGRP | stat.S_IROTH))
        checks.append(
            Check(
                "env.file_permissions",
                WARN if world_or_group else OK,
                (
                    f"{env_file.name} mode is {oct(mode)}; restrict to 0600"
                    if world_or_group
                    else f"{env_file.name} permissions are restrictive"
                ),
                {"mode": oct(mode)},
            )
        )
    else:
        checks.append(
            Check(
                "env.file_permissions",
                WARN,
                "no .env file inspected; environment variables are supplied externally",
            )
        )

    checks.append(
        Check(
            "env.anonymous_api",
            OK if not settings.allow_anonymous_api else FAIL,
            (
                "anonymous API access is disabled"
                if not settings.allow_anonymous_api
                else "EBA_ALLOW_ANONYMOUS_API is enabled; production must disable it"
            ),
        )
    )
    checks.append(
        Check(
            "env.api_keys",
            OK if settings.api_keys else FAIL,
            (
                f"{len(settings.api_keys)} API key(s) configured (values not inspected)"
                if settings.api_keys
                else "EBA_API_KEYS is empty; no caller can authenticate"
            ),
            {"count": len(settings.api_keys)},
        )
    )
    checks.append(
        Check(
            "env.llm_key",
            OK if settings.llm_api_key else FAIL,
            (
                f"LLM provider {settings.llm_provider!r} has a key configured (value not inspected)"
                if settings.llm_api_key
                else "no LLM API key is configured; agent runs return llm_unavailable"
            ),
        )
    )
    endpoint_issues = settings.llm_endpoint_issues()
    checks.append(
        Check(
            "env.llm_endpoint",
            OK if not endpoint_issues else FAIL,
            "; ".join(endpoint_issues) or "LLM base URL matches the provider protocol",
        )
    )
    return checks


def check_database(url: str | None, *, label: str) -> list[Check]:
    checks: list[Check] = []
    if not url:
        checks.append(Check(f"{label}.connection", FAIL, f"{label} URL is not configured"))
        return checks
    if url.lower().startswith("sqlite"):
        checks.append(
            Check(
                f"{label}.connection",
                WARN,
                "SQLite is a local development backend, not a production database",
                {"url": redact_url(url)},
            )
        )
        return checks

    engine = _connect_engine(url)
    try:
        started = time.monotonic()
        try:
            with engine.connect() as connection:
                version = connection.execute(text("SELECT version()")).scalar_one()
                connection.rollback()
        except Exception as exc:  # noqa: BLE001 - preflight reports, never raises
            checks.append(
                Check(
                    f"{label}.connection",
                    FAIL,
                    f"connection failed: {type(exc).__name__}",
                    {"url": redact_url(url)},
                )
            )
            return checks
        checks.append(
            Check(
                f"{label}.connection",
                OK,
                f"connected in {(time.monotonic() - started) * 1000:.0f} ms",
                {"url": redact_url(url), "server_version": str(version)[:80]},
            )
        )

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT current_user, rolsuper, rolcreatedb, rolcreaterole, "
                    "rolbypassrls FROM pg_roles WHERE rolname = current_user"
                )
            ).one()
            role = str(row[0])
            elevated = [
                name for name, value in zip(("superuser", "createdb", "createrole", "bypassrls"), row[1:]) if value
            ]
            checks.append(
                Check(
                    f"{label}.role_attributes",
                    FAIL if elevated else OK,
                    (
                        f"role {role!r} has elevated attributes: {', '.join(elevated)}"
                        if elevated
                        else f"role {role!r} is a plain login role"
                    ),
                    {"role": role, "elevated": elevated},
                )
            )
            schema_create = connection.execute(
                text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
            ).scalar_one()
            checks.append(
                Check(
                    f"{label}.no_ddl",
                    FAIL if schema_create else OK,
                    (
                        "role can create objects in schema public"
                        if schema_create
                        else "role cannot create objects in schema public"
                    ),
                    {"role": role},
                )
            )
            read_only = str(connection.execute(text("SHOW transaction_read_only")).scalar_one())
            checks.append(
                Check(
                    f"{label}.transaction_mode",
                    OK if read_only.lower() == "on" else WARN,
                    (
                        "sessions default to read-only transactions"
                        if read_only.lower() == "on"
                        else "sessions are read-write by default; the API still sets "
                        "a read-only transaction before every query"
                    ),
                )
            )
            connection.rollback()
        return checks
    finally:
        engine.dispose()


def check_readonly_privileges(url: str | None) -> list[Check]:
    """The API business role must not be able to modify business tables."""

    if not url or url.lower().startswith("sqlite"):
        return [
            Check(
                "database.readonly_grants",
                SKIP,
                "privilege catalog checks require a PostgreSQL connection",
            )
        ]
    engine = _connect_engine(url)
    try:
        with engine.connect() as connection:
            existing = set(inspect(connection).get_table_names())
            violations: dict[str, list[str]] = {}
            for table in _BUSINESS_TABLES:
                if table not in existing:
                    continue
                for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                    allowed = connection.execute(
                        text("SELECT has_table_privilege(current_user, :table, :privilege)"),
                        {"table": table, "privilege": privilege},
                    ).scalar_one()
                    if allowed:
                        violations.setdefault(table, []).append(privilege)
            missing = sorted(set(_BUSINESS_TABLES) - existing)
            checks = [
                Check(
                    "database.readonly_grants",
                    FAIL if violations else OK,
                    (
                        f"read-only role can write: {violations}"
                        if violations
                        else "no INSERT/UPDATE/DELETE/TRUNCATE privilege on business tables"
                    ),
                    {"violations": violations, "tables_checked": len(existing)},
                ),
                Check(
                    "database.business_tables",
                    WARN if missing else OK,
                    (
                        f"missing business tables: {missing}"
                        if missing
                        else f"{len(_BUSINESS_TABLES)} business tables present"
                    ),
                    {"missing": missing},
                ),
            ]
            connection.rollback()
            return checks
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return [
            Check(
                "database.readonly_grants",
                FAIL,
                f"privilege inspection failed: {type(exc).__name__}",
            )
        ]
    finally:
        engine.dispose()


def check_runtime_store(url: str | None, settings: Settings) -> list[Check]:
    checks: list[Check] = []
    if not url:
        checks.append(
            Check(
                "run_store.configured",
                FAIL,
                "RUN_STORE_URL is disabled; durable runs, checkpoints and approvals will not survive a restart",
            )
        )
        return checks
    if url.lower().startswith("sqlite") and ":memory:" not in url and url != "sqlite://":
        checks.append(
            Check(
                "run_store.backend",
                WARN,
                "SQLite run store; a single-process local deployment only",
                {"url": redact_url(url)},
            )
        )
    elif url == "sqlite://":
        checks.append(
            Check(
                "run_store.backend",
                FAIL,
                "in-memory run store; every restart loses runs, checkpoints and approvals",
            )
        )
    else:
        checks.append(
            Check(
                "run_store.backend",
                OK,
                "PostgreSQL run store",
                {"url": redact_url(url)},
            )
        )

    expected = runtime_table_names()
    if not url.lower().startswith("sqlite"):
        engine = _connect_engine(url)
        try:
            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT current_user, rolsuper, rolcreatedb, rolcreaterole "
                        "FROM pg_roles WHERE rolname = current_user"
                    )
                ).one()
                role = str(row[0])
                elevated = [name for name, value in zip(("superuser", "createdb", "createrole"), row[1:]) if value]
                checks.append(
                    Check(
                        "run_store.role_attributes",
                        FAIL if elevated else OK,
                        (
                            f"runtime role {role!r} has elevated attributes: {', '.join(elevated)}"
                            if elevated
                            else f"runtime role {role!r} is a plain login role"
                        ),
                        {"role": role},
                    )
                )
                schema_create = connection.execute(
                    text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
                ).scalar_one()
                checks.append(
                    Check(
                        "run_store.no_ddl",
                        FAIL if schema_create else OK,
                        (
                            "runtime role can create objects in schema public"
                            if schema_create
                            else "runtime role cannot create objects in schema public"
                        ),
                    )
                )
                connection.rollback()
        except Exception as exc:  # noqa: BLE001
            checks.append(
                Check(
                    "run_store.catalog",
                    FAIL,
                    f"runtime store inspection failed: {type(exc).__name__}",
                )
            )
        finally:
            engine.dispose()

    engine = _connect_engine(url)
    try:
        inspector = inspect(engine)
        names = set(inspector.get_table_names())
        missing = sorted(set(expected) - names)
        checks.append(
            Check(
                "run_store.schema",
                FAIL if missing else OK,
                (f"missing runtime tables: {missing}" if missing else f"{len(expected)} runtime tables present"),
                {"missing": missing},
            )
        )
        columns = (
            {str(item["name"]) for item in inspector.get_columns("agent_runs")} if "agent_runs" in names else set()
        )
        missing_claims = [name for name in _CLAIM_COLUMNS if name not in columns]
        checks.append(
            Check(
                "run_store.claim_columns",
                FAIL if missing_claims else OK,
                (
                    f"agent_runs is missing claim columns: {missing_claims}"
                    if missing_claims
                    else "agent_runs has claim/lease/fencing columns"
                ),
                {"missing": missing_claims},
            )
        )
        checkpoint_columns = (
            {str(item["name"]) for item in inspector.get_columns("agent_checkpoints")}
            if "agent_checkpoints" in names
            else set()
        )
        checks.append(
            Check(
                "run_store.checkpoint",
                OK if {"thread_id", "checkpoint_id", "checkpoint_value"} <= checkpoint_columns else FAIL,
                (
                    "checkpoint tables can persist and restore LangGraph state"
                    if {"thread_id", "checkpoint_id", "checkpoint_value"} <= checkpoint_columns
                    else "agent_checkpoints is missing required columns"
                ),
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(
            Check(
                "run_store.schema",
                FAIL,
                f"runtime schema inspection failed: {type(exc).__name__}",
            )
        )
    finally:
        engine.dispose()

    checks.append(
        Check(
            "run_store.cleanup_policy",
            WARN if settings.run_cleanup_dry_run else OK,
            (
                "RUN_CLEANUP_DRY_RUN is enabled; expired runs are reported, not deleted"
                if settings.run_cleanup_dry_run
                else "cleanup deletes expired runs per status TTL"
            ),
            {
                "retention_seconds": {
                    "completed": settings.run_retention_completed_seconds,
                    "failed": settings.run_retention_failed_seconds,
                    "cancelled": settings.run_retention_cancelled_seconds,
                    "unfinished": settings.run_retention_unfinished_seconds,
                },
                "batch_limit": settings.run_cleanup_batch_limit,
            },
        )
    )
    checks.append(
        Check(
            "run_store.multi_process",
            OK if not url.lower().startswith("sqlite") else WARN,
            (
                "database claim/lease/fencing supports multiple API processes"
                if not url.lower().startswith("sqlite")
                else "SQLite is not a valid multi-process backend; use PostgreSQL"
            ),
            {"lease_seconds": settings.run_lease_seconds},
        )
    )
    return checks


def check_transport_and_keys(settings: Settings, database_url: str | None) -> list[Check]:
    checks: list[Check] = []
    if database_url and not database_url.lower().startswith("sqlite"):
        query = urlparse(database_url).query.lower()
        sslmode = ""
        for part in query.split("&"):
            if part.startswith("sslmode="):
                sslmode = part.split("=", 1)[1]
        if sslmode in {"verify-full", "verify-ca", "require"}:
            checks.append(
                Check(
                    "security.tls",
                    OK if sslmode in {"verify-full", "verify-ca"} else WARN,
                    f"database TLS mode is {sslmode}"
                    + ("" if sslmode in {"verify-full", "verify-ca"} else "; certificate verification is not enabled"),
                )
            )
        else:
            checks.append(
                Check(
                    "security.tls",
                    WARN,
                    "no sslmode=verify-full/verify-ca in the connection URL; traffic may be unencrypted or unverified",
                )
            )
    checks.append(
        Check(
            "security.key_management",
            WARN,
            "secrets are read from environment variables; production should inject "
            "them from a managed secret store and rotate the LLM key as a separate "
            "manual step",
        )
    )
    checks.append(
        Check(
            "ops.logging",
            OK if settings.log_level.upper() in {"INFO", "WARNING", "ERROR"} else WARN,
            f"LOG_LEVEL={settings.log_level}; structured JSON logs with request_id",
        )
    )
    return checks


def check_observability(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    sdk = True
    exporter = True
    try:
        import opentelemetry.sdk  # noqa: F401
    except ImportError:
        sdk = False
    try:
        import opentelemetry.exporter.otlp.proto.http  # noqa: F401
    except ImportError:
        exporter = False

    if not settings.otel_enabled:
        checks.append(
            Check(
                "observability.tracing",
                WARN,
                "EBA_OTEL_ENABLED is false; no span is created or exported",
                {"sdk_installed": sdk, "otlp_exporter_installed": exporter},
            )
        )
        return checks
    endpoint = settings.otel_exporter_endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    checks.append(
        Check(
            "observability.tracing",
            OK if sdk and exporter and endpoint else FAIL,
            (
                "tracing is enabled with an OTLP endpoint configured"
                if sdk and exporter and endpoint
                else "tracing is enabled but the SDK, OTLP exporter or endpoint is missing"
            ),
            {
                "sdk_installed": sdk,
                "otlp_exporter_installed": exporter,
                "endpoint_configured": bool(endpoint),
            },
        )
    )
    checks.append(
        Check(
            "observability.export_verification",
            WARN,
            "a real export can only be confirmed by running the service against an "
            "OTLP backend and reading observability.exported_spans from /api/v1/metrics",
        )
    )
    return checks


def check_api(base_url: str | None, *, timeout: float) -> list[Check]:
    if not base_url:
        return [
            Check(
                "api.reachable",
                SKIP,
                "no --api-base-url supplied; health, metrics and approval endpoints were not probed",
            )
        ]
    import httpx

    checks: list[Check] = []
    probes = (
        ("api.health", "/health", {200}),
        ("api.metrics", "/api/v1/metrics", {200, 401, 403}),
        ("api.approvals", "/api/v1/agent/approvals", {200, 401, 403}),
    )
    with httpx.Client(base_url=base_url, timeout=timeout, trust_env=False) as client:
        for name, path, allowed in probes:
            try:
                response = client.get(path)
            except Exception as exc:  # noqa: BLE001
                checks.append(Check(name, FAIL, f"{path} unreachable: {type(exc).__name__}"))
                continue
            if response.status_code in allowed:
                checks.append(
                    Check(
                        name,
                        OK,
                        f"{path} responded {response.status_code}",
                        {"status_code": response.status_code},
                    )
                )
            else:
                checks.append(
                    Check(
                        name,
                        FAIL,
                        f"{path} responded {response.status_code}, expected one of {sorted(allowed)}",
                        {"status_code": response.status_code},
                    )
                )
            if name == "api.metrics" and response.status_code == 200:
                try:
                    observability = response.json().get("observability", {})
                except ValueError:
                    observability = {}
                checks.append(
                    Check(
                        "api.metrics_observability",
                        OK,
                        "metrics endpoint exposes observability status",
                        {"observability": observability},
                    )
                )
    return checks


# -- reporting --------------------------------------------------------------
def summarize(checks: list[Check]) -> dict[str, int]:
    counts = {OK: 0, WARN: 0, FAIL: 0, SKIP: 0}
    for item in checks:
        counts[item.status] = counts.get(item.status, 0) + 1
    return counts


def _print_human(checks: list[Check], counts: dict[str, int]) -> None:
    width = max(len(item.name) for item in checks) if checks else 10
    symbols = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL", SKIP: "SKIP"}
    for item in checks:
        print(f"[{symbols.get(item.status, item.status)}] {item.name.ljust(width)}  {item.detail}")
    print(
        f"\n{counts.get(OK, 0)} ok, {counts.get(WARN, 0)} warn, "
        f"{counts.get(FAIL, 0)} fail, {counts.get(SKIP, 0)} skipped"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only production preflight. Never writes to the database.")
    parser.add_argument(
        "--env-file",
        default=None,
        help="optional dotenv file to load; its contents are never printed",
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--runtime-url", default=None)
    parser.add_argument("--api-base-url", default=None)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    env_file = Path(args.env_file).expanduser() if args.env_file else None
    if env_file is not None:
        if not env_file.exists():
            print(f"ERROR: env file not found: {env_file}", file=sys.stderr)
            return 2
        load_env_file(env_file)

    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: settings are invalid: {type(exc).__name__}", file=sys.stderr)
        return 2

    database_url = args.database_url or settings.application_database_url
    runtime_url = args.runtime_url or settings.run_store_url

    checks: list[Check] = []
    checks.extend(check_environment(settings, env_file))
    checks.extend(check_database(database_url, label="database"))
    checks.extend(check_readonly_privileges(database_url))
    checks.extend(check_runtime_store(runtime_url, settings))
    checks.extend(check_transport_and_keys(settings, database_url))
    checks.extend(check_observability(settings))
    checks.extend(check_api(args.api_base_url, timeout=args.timeout))

    counts = summarize(checks)
    if args.json:
        print(
            json.dumps(
                {
                    "read_only": True,
                    "database": redact_url(database_url),
                    "runtime_store": redact_url(runtime_url),
                    "summary": counts,
                    "checks": [item.as_dict() for item in checks],
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    else:
        _print_human(checks, counts)

    return 1 if counts.get(FAIL) else 0


if __name__ == "__main__":
    raise SystemExit(main())

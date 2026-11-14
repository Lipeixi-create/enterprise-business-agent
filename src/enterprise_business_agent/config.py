from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from .analysis.time_ranges import shift_month


DEFAULT_DATABASE_URL = "sqlite:///./enterprise_business_agent.db"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_GLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
SUPPORTED_LLM_PROVIDERS = frozenset({"anthropic", "glm"})


def load_env_file(path: str | Path = ".env", *, override: bool = False) -> bool:
    """Load a local dotenv file without ever logging its contents.

    The API process deliberately does not load ``.env`` implicitly: tests and
    deployments should pass environment variables explicitly. Live scripts call
    this helper, and deployments can opt in with ``EBA_LOAD_DOTENV=true``.
    """

    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    return bool(load_dotenv(dotenv_path=path, override=override))


def llm_api_key_env_names(provider: str) -> list[str]:
    """Environment variables consulted for ``provider``'s key, in priority order.

    Names only: the evaluation preflight needs to tell an operator which
    variable to set, and it must never touch the value.
    """

    names = ["LLM_API_KEY"]
    if provider.strip().lower() == "glm":
        names.extend(["GLM_API_KEY", "ZHIPU_API_KEY"])
    else:
        names.append("ANTHROPIC_API_KEY")
    return names


def dotenv_llm_declarations(path: str | Path) -> dict[str, Any]:
    """Non-secret LLM declarations from a dotenv file.

    Returns the declared provider and model plus the *names* of key variables
    that are set to a non-empty value. Key values are never read back out: the
    only thing reported about them is that a name exists. Used by the live
    evaluation preflight to explain, safely, why a run sees no key.
    """

    declared: dict[str, str] = {}
    present: set[str] = set()
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {"provider": None, "model": None, "key_names": []}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export ") :].strip()
        value = value.strip().strip("'\"")
        if not value:
            continue
        present.add(name)
        if name in {"LLM_PROVIDER", "LLM_MODEL"}:
            declared[name] = value
    provider = declared.get("LLM_PROVIDER", "").strip().lower() or "anthropic"
    return {
        "provider": provider,
        "model": declared.get("LLM_MODEL"),
        "key_names": [name for name in llm_api_key_env_names(provider) if name in present],
    }


class Settings(BaseModel):
    database_url: str = Field(default=DEFAULT_DATABASE_URL)
    readonly_database_url: str | None = Field(default=None)
    data_start: date = Field(default=date(2024, 1, 1))
    data_months: int = Field(default=24, ge=1, le=120)

    query_timeout_ms: int = Field(default=15_000, ge=100, le=120_000)
    query_max_rows: int = Field(default=200, ge=1, le=1_000)
    sql_max_offset: int = Field(default=10_000, ge=0)
    api_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    log_level: str = Field(default="INFO")

    api_keys: dict[str, str] = Field(default_factory=dict, repr=False, exclude=True)
    allow_anonymous_api: bool = True
    authz_policy: dict[str, dict[str, Any]] = Field(default_factory=dict)

    # MCP is an external tool transport, not a second execution path. When it
    # is enabled every tool call still goes through ToolRegistry,
    # AuthorizationPolicy and AnalysisGateway. It is off by default so an
    # existing deployment never exposes a new endpoint by upgrading.
    mcp_enabled: bool = False
    mcp_require_api_key: bool = True
    mcp_server_name: str = Field(default="enterprise-business-agent")

    agent_max_tool_calls: int = Field(default=6, ge=1, le=20)
    agent_max_seconds: float = Field(default=300.0, gt=0, le=300)
    agent_tool_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    agent_tool_max_retries: int = Field(default=1, ge=0, le=3)
    agent_max_consecutive_failures: int = Field(default=2, ge=1, le=5)
    agent_max_total_tokens: int = Field(default=120_000, ge=1_000, le=1_000_000)
    agent_invalid_action_max_retries: int = Field(default=1, ge=0, le=3)
    agent_llm_max_retries: int = Field(default=1, ge=0, le=3)
    agent_run_max_lifecycle_seconds: float = Field(
        default=3_600.0, gt=0, le=86_400
    )

    run_store_url: str | None = Field(default=None)
    run_store_ttl_seconds: float = Field(default=86_400.0, gt=0)
    run_lease_seconds: float = Field(default=60.0, ge=1, le=3_600)
    run_retention_completed_seconds: float = Field(
        default=2_592_000.0, ge=0
    )
    run_retention_failed_seconds: float = Field(default=1_209_600.0, ge=0)
    run_retention_cancelled_seconds: float = Field(default=604_800.0, ge=0)
    run_retention_unfinished_seconds: float = Field(default=259_200.0, ge=0)
    run_cleanup_dry_run: bool = Field(default=False)
    run_cleanup_batch_limit: int = Field(default=1_000, ge=1, le=10_000)

    phase5_enabled: bool = Field(default=True)
    phase5_narrative_enabled: bool = Field(default=True)
    phase5_max_fix_attempts: int = Field(default=1, ge=0, le=3)
    phase5_narrative_max_tokens: int = Field(default=800, ge=128, le=4_096)
    agent_final_continuation_max_tokens: int = Field(
        default=800, ge=64, le=4_096
    )

    otel_enabled: bool = False
    otel_service_name: str = Field(default="enterprise-business-agent")
    # otlp: send to a collector; console: print finished spans; file: append
    # JSONL under otel_trace_file; none: initialise without an exporter.
    otel_exporter: str = Field(default="otlp")
    otel_exporter_endpoint: str | None = Field(default=None)
    otel_trace_file: str = Field(default="data/traces/eba-spans.jsonl")
    otel_sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)

    alert_failure_rate: float = Field(default=0.25, ge=0.0, le=1.0)
    alert_p95_seconds: float = Field(default=300.0, gt=0)
    alert_pending_approvals: int = Field(default=10, ge=0)
    alert_min_runs: int = Field(default=5, ge=1)

    # Cost is only estimated when both prices are configured. The source label
    # is shown verbatim by /api/v1/metrics so nobody mistakes a default for a
    # contracted price.
    model_input_price_per_million: float | None = Field(default=None, ge=0)
    model_output_price_per_million: float | None = Field(default=None, ge=0)
    model_price_source: str = Field(default="unconfigured")
    model_price_currency: str = Field(default="CNY")

    llm_provider: str = Field(default="anthropic")
    llm_model: str = Field(default="claude-sonnet-5-5")
    llm_api_key: str | None = Field(default=None, repr=False, exclude=True)
    llm_base_url: str = Field(default=DEFAULT_ANTHROPIC_BASE_URL)
    llm_timeout_seconds: float = Field(default=120.0, gt=0, le=120)
    llm_connect_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    llm_read_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    llm_max_retries: int = Field(default=1, ge=0, le=5)
    llm_retry_backoff_base_seconds: float = Field(default=0.5, ge=0.0, le=30.0)
    llm_retry_backoff_max_seconds: float = Field(default=8.0, ge=0.0, le=120.0)
    llm_retry_after_max_seconds: float = Field(default=30.0, ge=0.0, le=300.0)
    llm_circuit_failure_threshold: int = Field(default=3, ge=0, le=50)
    llm_circuit_reset_seconds: float = Field(default=30.0, gt=0, le=600.0)
    llm_max_tokens: int = Field(default=4_000, ge=64, le=8_192)
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_seed: int | None = Field(default=None, ge=0)

    @property
    def data_end(self) -> date:
        return shift_month(self.data_start, self.data_months - 1)

    @property
    def application_database_url(self) -> str:
        """Connection used by application queries.

        Production must point this at a read-only role. Falling back to the
        setup URL keeps local SQLite development friction low, while the
        read-only engine and SQL Guard still enforce query-only execution.
        """
        return self.readonly_database_url or self.database_url

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key)

    def llm_endpoint_issues(self) -> list[str]:
        """Return blocking mismatches for a real provider acceptance check.

        Anthropic uses the Messages API; GLM uses its OpenAI-compatible Chat
        Completions API. The checks are protocol checks, not an attempt to
        validate model quality.
        """

        issues: list[str] = []
        provider = self.llm_provider.strip().lower()
        if provider not in SUPPORTED_LLM_PROVIDERS:
            issues.append(
                f"LLM_PROVIDER={self.llm_provider!r}; supported providers are "
                f"{sorted(SUPPORTED_LLM_PROVIDERS)}"
            )
            return issues
        parsed = urlparse(self.llm_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            issues.append("LLM_BASE_URL must be an absolute http(s) URL")
        if provider == "anthropic":
            if not self.llm_model.lower().startswith("claude"):
                issues.append(
                    f"LLM_MODEL={self.llm_model!r} is not an Anthropic Claude model id"
                )
            if not parsed.path.rstrip("/").endswith("/v1/messages"):
                issues.append("LLM_BASE_URL path must end with /v1/messages")
        else:
            if not self.llm_model.lower().startswith("glm"):
                issues.append(
                    f"LLM_MODEL={self.llm_model!r} is not a GLM model id"
                )
            if "/chat/completions" in parsed.path:
                issues.append(
                    "LLM_BASE_URL must be the API base URL, not the "
                    "/chat/completions endpoint"
                )
            elif "bigmodel.cn" in (parsed.hostname or "") and not parsed.path.rstrip(
                "/"
            ).endswith("/api/paas/v4"):
                issues.append("GLM LLM_BASE_URL path must end with /api/paas/v4")
        return issues


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_optional_float(name: str) -> float | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _env_json_dict(name: str) -> dict[str, Any]:
    value = os.getenv(name)
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} must be a JSON object")
    return parsed


def _load_api_keys() -> dict[str, str]:
    keys = _env_json_dict("EBA_API_KEYS")
    single = os.getenv("EBA_API_KEY")
    if single:
        keys[single] = os.getenv("EBA_API_ROLE", "analyst")
    if not all(isinstance(key, str) and isinstance(role, str) for key, role in keys.items()):
        raise ValueError("EBA_API_KEYS must map API keys to role names")
    return keys


def _default_llm_model(provider: str) -> str:
    return "glm-5.3-flash" if provider == "glm" else "claude-sonnet-5-5"


def _default_llm_base_url(provider: str) -> str:
    return DEFAULT_GLM_BASE_URL if provider == "glm" else DEFAULT_ANTHROPIC_BASE_URL


def _run_store_url() -> str | None:
    """Durable run/audit store.

    Defaults to a local SQLite file so a process restart does not lose run
    status and audit records. ``RUN_STORE_URL=memory`` keeps tests in memory,
    and ``RUN_STORE_URL=none`` disables the persistent store entirely.
    """

    value = os.getenv("RUN_STORE_URL")
    if value is None:
        return "sqlite:///./enterprise_business_agent_runs.db"
    value = value.strip()
    lowered = value.lower()
    if lowered in {"none", "off", ""}:
        return None
    if lowered == "memory":
        return "sqlite://"
    return value


def _llm_api_key(provider: str) -> str | None:
    for name in llm_api_key_env_names(provider):
        value = os.getenv(name)
        if value:
            return value
    return None


def get_settings(
    database_url: str | None = None,
    readonly_database_url: str | None = None,
    *,
    load_dotenv: bool = False,
    dotenv_path: str | Path = ".env",
) -> Settings:
    """Build settings from the current environment.

    ``.env`` is not read unless the caller opts in (``load_dotenv=True`` from a
    live script, or ``EBA_LOAD_DOTENV=true`` in the environment). That opt-in is
    what lets ``--preflight-only`` show exactly what a live run would use.
    """

    if load_dotenv or _env_bool("EBA_LOAD_DOTENV", False):
        load_env_file(dotenv_path, override=False)
    provider = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
    return Settings(
        database_url=database_url or os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL),
        readonly_database_url=readonly_database_url or os.getenv("READONLY_DATABASE_URL") or None,
        data_start=date.fromisoformat(os.getenv("DATA_START", "2024-01-01")),
        data_months=int(os.getenv("DATA_MONTHS", "24")),
        query_timeout_ms=int(os.getenv("QUERY_TIMEOUT_MS", "15000")),
        query_max_rows=int(os.getenv("QUERY_MAX_ROWS", "200")),
        sql_max_offset=int(os.getenv("SQL_MAX_OFFSET", "10000")),
        api_timeout_seconds=float(os.getenv("API_TIMEOUT_SECONDS", "30")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        api_keys=_load_api_keys(),
        allow_anonymous_api=_env_bool("EBA_ALLOW_ANONYMOUS_API", True),
        authz_policy=_env_json_dict("EBA_AUTHZ_POLICY"),
        mcp_enabled=_env_bool("EBA_MCP_ENABLED", False),
        mcp_require_api_key=_env_bool("EBA_MCP_REQUIRE_API_KEY", True),
        mcp_server_name=os.getenv("EBA_MCP_SERVER_NAME", "enterprise-business-agent"),
        agent_max_tool_calls=int(os.getenv("AGENT_MAX_TOOL_CALLS", "6")),
        agent_max_seconds=float(os.getenv("AGENT_MAX_SECONDS", "300")),
        agent_tool_timeout_seconds=float(os.getenv("AGENT_TOOL_TIMEOUT_SECONDS", "20")),
        agent_tool_max_retries=int(os.getenv("AGENT_TOOL_MAX_RETRIES", "1")),
        agent_max_consecutive_failures=int(os.getenv("AGENT_MAX_CONSECUTIVE_FAILURES", "2")),
        agent_max_total_tokens=int(os.getenv("AGENT_MAX_TOTAL_TOKENS", "120000")),
        agent_invalid_action_max_retries=int(
            os.getenv("AGENT_INVALID_ACTION_MAX_RETRIES", "1")
        ),
        agent_llm_max_retries=int(os.getenv("AGENT_LLM_MAX_RETRIES", "1")),
        agent_run_max_lifecycle_seconds=float(
            os.getenv("AGENT_RUN_MAX_LIFECYCLE_SECONDS", "3600")
        ),
        run_store_url=_run_store_url(),
        run_store_ttl_seconds=float(os.getenv("RUN_STORE_TTL_SECONDS", "86400")),
        run_lease_seconds=float(os.getenv("RUN_LEASE_SECONDS", "60")),
        run_retention_completed_seconds=float(
            os.getenv("RUN_RETENTION_COMPLETED_SECONDS", "2592000")
        ),
        run_retention_failed_seconds=float(
            os.getenv("RUN_RETENTION_FAILED_SECONDS", "1209600")
        ),
        run_retention_cancelled_seconds=float(
            os.getenv("RUN_RETENTION_CANCELLED_SECONDS", "604800")
        ),
        run_retention_unfinished_seconds=float(
            os.getenv("RUN_RETENTION_UNFINISHED_SECONDS", "259200")
        ),
        run_cleanup_dry_run=_env_bool("RUN_CLEANUP_DRY_RUN", False),
        run_cleanup_batch_limit=int(os.getenv("RUN_CLEANUP_BATCH_LIMIT", "1000")),
        phase5_enabled=_env_bool("EBA_PHASE5_ENABLED", True),
        phase5_narrative_enabled=_env_bool("EBA_PHASE5_LLM_NARRATIVE", True),
        phase5_max_fix_attempts=int(os.getenv("EBA_PHASE5_MAX_FIX_ATTEMPTS", "1")),
        phase5_narrative_max_tokens=int(
            os.getenv("EBA_PHASE5_NARRATIVE_MAX_TOKENS", "800")
        ),
        agent_final_continuation_max_tokens=int(
            os.getenv("AGENT_FINAL_CONTINUATION_MAX_TOKENS", "800")
        ),
        otel_enabled=_env_bool("EBA_OTEL_ENABLED", False),
        otel_service_name=os.getenv(
            "EBA_OTEL_SERVICE_NAME", "enterprise-business-agent"
        ),
        otel_exporter=os.getenv(
            "EBA_OTEL_EXPORTER",
            "otlp" if (
                os.getenv("EBA_OTEL_EXPORTER_ENDPOINT")
                or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
            ) else "otlp",
        ),
        otel_exporter_endpoint=os.getenv("EBA_OTEL_EXPORTER_ENDPOINT")
        or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        or None,
        otel_trace_file=os.getenv(
            "EBA_OTEL_TRACE_FILE", "data/traces/eba-spans.jsonl"
        ),
        otel_sample_ratio=float(os.getenv("EBA_OTEL_SAMPLE_RATIO", "1.0")),
        alert_failure_rate=float(os.getenv("EBA_ALERT_FAILURE_RATE", "0.25")),
        alert_p95_seconds=float(os.getenv("EBA_ALERT_P95_SECONDS", "300")),
        alert_pending_approvals=int(os.getenv("EBA_ALERT_PENDING_APPROVALS", "10")),
        alert_min_runs=int(os.getenv("EBA_ALERT_MIN_RUNS", "5")),
        model_input_price_per_million=_env_optional_float(
            "EBA_MODEL_INPUT_PRICE_PER_MTOKENS"
        ),
        model_output_price_per_million=_env_optional_float(
            "EBA_MODEL_OUTPUT_PRICE_PER_MTOKENS"
        ),
        model_price_source=os.getenv("EBA_MODEL_PRICE_SOURCE", "unconfigured"),
        model_price_currency=os.getenv("EBA_MODEL_PRICE_CURRENCY", "CNY"),
        llm_provider=provider,
        llm_model=os.getenv("LLM_MODEL", _default_llm_model(provider)),
        llm_api_key=_llm_api_key(provider),
        llm_base_url=os.getenv("LLM_BASE_URL", _default_llm_base_url(provider)),
        llm_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "120")),
        llm_connect_timeout_seconds=float(
            os.getenv("LLM_CONNECT_TIMEOUT_SECONDS", "10")
        ),
        llm_read_timeout_seconds=float(os.getenv("LLM_READ_TIMEOUT_SECONDS", "60")),
        llm_max_retries=int(os.getenv("LLM_MAX_RETRIES", "1")),
        llm_retry_backoff_base_seconds=float(
            os.getenv("LLM_RETRY_BACKOFF_BASE_SECONDS", "0.5")
        ),
        llm_retry_backoff_max_seconds=float(
            os.getenv("LLM_RETRY_BACKOFF_MAX_SECONDS", "8")
        ),
        llm_retry_after_max_seconds=float(
            os.getenv("LLM_RETRY_AFTER_MAX_SECONDS", "30")
        ),
        llm_circuit_failure_threshold=int(
            os.getenv("LLM_CIRCUIT_FAILURE_THRESHOLD", "3")
        ),
        llm_circuit_reset_seconds=float(
            os.getenv("LLM_CIRCUIT_RESET_SECONDS", "30")
        ),
        llm_max_tokens=int(os.getenv("LLM_MAX_TOKENS", "4000")),
        llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
        llm_seed=(
            int(os.environ["LLM_SEED"])
            if os.getenv("LLM_SEED", "").strip()
            else None
        ),
    )

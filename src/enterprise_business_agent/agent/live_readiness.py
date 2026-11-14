"""Guard rails for a real-model evaluation run.

Everything in this module is offline. The API key is only inspected for
presence, never read into a report, and no request is made. That lets the
evaluation runner fail before spending budget, and lets tests exercise the
guard rails without an authorization to call a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from ..config import (
    SUPPORTED_LLM_PROVIDERS,
    Settings,
    dotenv_llm_declarations,
    llm_api_key_env_names,
)
from .graph import AGENT_PROMPT_VERSION

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

# A first live round should be small enough to review by hand. These 20 cases
# cover every business domain, the multi-tool investigations, and the four
# failure modes that can silently produce a plausible but wrong answer.
FIRST_ROUND_IDS: tuple[str, ...] = (
    "phase7-001",  # Sales, single metric
    "phase7-008",  # Sales, trend direction (no numeric target)
    "phase7-048",  # anomaly scan
    "phase7-010",  # Marketing, marketing spend
    "phase7-015",  # Marketing, conversion rate
    "phase7-019",  # Finance, net profit
    "phase7-020",  # Finance, net margin
    "phase7-050",  # contribution decomposition
    "phase7-026",  # Supply Chain, inventory days
    "phase7-029",  # Supply Chain, top_n stockout rate
    "phase7-034",  # HR, headcount
    "phase7-039",  # HR, labor cost change
    "phase7-040",  # Cross-domain, two tool calls
    "phase7-044",  # Cross-domain, margin vs stockout
    "phase7-051",  # metric ambiguity, no numeric target
    "phase7-053",  # metric ambiguity, conversion rate
    "phase7-054",  # missing data, must refuse
    "phase7-057",  # permission denial
    "phase7-060",  # prompt injection, write request
    "phase7-063",  # predictive question, must refuse
)

FIRST_ROUND_DOMAINS = frozenset(
    {"Sales", "Marketing", "Finance", "Supply Chain", "HR", "Cross-domain", "Safety"}
)
FIRST_ROUND_CATEGORIES = frozenset(
    {
        "metric_query",
        "top_n",
        "trend",
        "contribution",
        "anomaly",
        "cross_metric",
        "metric_ambiguity",
        "missing_data",
        "unauthorized",
        "prompt_injection",
        "predictive",
    }
)

# Above this a run is no longer "a first live round"; require a separate review.
HARD_MAX_MODEL_CALLS = 2_000
MIN_MODEL_CALLS_PER_CASE = 1


@dataclass(frozen=True)
class LiveCheck:
    name: str
    status: str
    message: str
    # A gate the operator has not opened yet (authorization, budget). It blocks
    # the run exactly like an environment defect, but it is not one: the
    # environment may be perfectly fine. Reporting them apart stops an operator
    # from "fixing" a working configuration.
    operator_gate: bool = False


@dataclass
class LiveReadiness:
    checks: list[LiveCheck] = field(default_factory=list)

    @property
    def failed(self) -> list[LiveCheck]:
        return [item for item in self.checks if item.status == FAIL]

    @property
    def warned(self) -> list[LiveCheck]:
        return [item for item in self.checks if item.status == WARN]

    @property
    def operator_gated(self) -> list[LiveCheck]:
        return [item for item in self.failed if item.operator_gate]

    @property
    def environment_blockers(self) -> list[LiveCheck]:
        return [item for item in self.failed if not item.operator_gate]

    @property
    def blocking(self) -> bool:
        return bool(self.failed)

    def counts(self) -> dict[str, int]:
        # ``total`` is emitted so the three status buckets can be checked
        # against the number of checks actually run; the two used to disagree.
        return {
            PASS: sum(1 for item in self.checks if item.status == PASS),
            WARN: len(self.warned),
            FAIL: len(self.failed),
            "total": len(self.checks),
        }


def _host(url: str) -> str:
    return urlparse(url).hostname or "<invalid>"


def first_round_coverage(cases: list) -> dict[str, list[str]]:
    """Report which first-round dimensions the selected cases do not cover."""

    ids = {case.id for case in cases}
    domains = {case.domain for case in cases if case.domain}
    categories = {case.category for case in cases}
    return {
        "missing_ids": sorted(set(FIRST_ROUND_IDS) - ids),
        "missing_domains": sorted(FIRST_ROUND_DOMAINS - domains),
        "missing_categories": sorted(FIRST_ROUND_CATEGORIES - categories),
    }


def config_snapshot(
    settings: Settings,
    cases: list,
    *,
    preset: str | None = None,
    dotenv_loaded: bool = False,
) -> dict:
    """Everything needed to reproduce a run, with no secret in it.

    ``api_key_configured`` is a boolean on purpose: the archive records that a
    key existed, never its value or a prefix of it.
    """

    prices = (
        settings.model_input_price_per_million is not None
        and settings.model_output_price_per_million is not None
    )
    return {
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "base_url_host": _host(settings.llm_base_url),
        "prompt_version": AGENT_PROMPT_VERSION,
        "api_key_configured": settings.llm_enabled,
        "api_key_env_names": llm_api_key_env_names(settings.llm_provider),
        "dotenv_loaded": dotenv_loaded,
        "preset": preset,
        "selected_cases": len(cases),
        "case_ids": [case.id for case in cases],
        "llm": {
            "timeout_seconds": settings.llm_timeout_seconds,
            "max_retries": settings.llm_max_retries,
            "max_tokens": settings.llm_max_tokens,
        },
        "agent": {
            "max_tool_calls": settings.agent_max_tool_calls,
            "max_seconds": settings.agent_max_seconds,
            "max_total_tokens": settings.agent_max_total_tokens,
        },
        "cost": {
            "prices_configured": prices,
            "price_source": settings.model_price_source,
            "currency": settings.model_price_currency,
        },
    }


def live_preflight(
    settings: Settings,
    cases: list,
    *,
    allow_live_model: bool,
    max_model_calls: int,
    preset: str | None = None,
    output_path: str | Path | None = None,
    dotenv_path: str | Path | None = None,
    dotenv_loaded: bool = False,
) -> LiveReadiness:
    """Check everything that must hold before the first request is sent."""

    readiness = LiveReadiness()
    add = readiness.checks.append

    add(
        LiveCheck(
            "authorization.live_model",
            PASS if allow_live_model else FAIL,
            (
                "已显式授权真实模型调用"
                if allow_live_model
                else "缺少 --allow-live-model；不会发送任何请求"
            ),
            operator_gate=not allow_live_model,
        )
    )

    if max_model_calls <= 0:
        budget_status = FAIL
        budget_message = "缺少 --max-model-calls，调用预算未确认"
    elif max_model_calls < len(cases) * MIN_MODEL_CALLS_PER_CASE:
        budget_status = FAIL
        budget_message = (
            f"调用上限 {max_model_calls} 小于用例数 {len(cases)}"
            "（每条至少需要一次规划调用）"
        )
    elif max_model_calls > HARD_MAX_MODEL_CALLS:
        budget_status = FAIL
        budget_message = f"调用上限 {max_model_calls} 超过硬上限 {HARD_MAX_MODEL_CALLS}"
    else:
        budget_status = PASS
        budget_message = (
            f"调用上限 {max_model_calls}；每条用例运行时还会受 "
            f"agent_max_tool_calls={settings.agent_max_tool_calls} 和 "
            f"agent_max_total_tokens={settings.agent_max_total_tokens} 约束"
        )
    add(
        LiveCheck(
            "authorization.budget",
            budget_status,
            budget_message,
            operator_gate=budget_status == FAIL,
        )
    )

    ids = [case.id for case in cases]
    duplicate_ids = sorted({item for item in ids if ids.count(item) > 1})
    if not cases:
        add(LiveCheck("corpus.selected", FAIL, "没有选中任何用例"))
    elif duplicate_ids:
        add(LiveCheck("corpus.selected", FAIL, f"用例 ID 重复：{duplicate_ids}"))
    else:
        add(LiveCheck("corpus.selected", PASS, f"选中 {len(cases)} 条唯一用例"))

    if preset == "first-round-20":
        coverage = first_round_coverage(cases)
        missing = (
            coverage["missing_ids"]
            + coverage["missing_domains"]
            + coverage["missing_categories"]
        )
        add(
            LiveCheck(
                "corpus.first_round_coverage",
                FAIL if missing else PASS,
                "首轮覆盖完整" if not missing else f"首轮用例缺失：{missing}",
            )
        )

    provider = settings.llm_provider.strip().lower()
    add(
        LiveCheck(
            "provider.name",
            PASS if provider in SUPPORTED_LLM_PROVIDERS else FAIL,
            f"provider={settings.llm_provider!r}",
        )
    )

    issues = settings.llm_endpoint_issues()
    add(
        LiveCheck(
            "provider.endpoint",
            FAIL if issues else PASS,
            "base_url 与 model 名称和协议匹配"
            if not issues
            else "；".join(issues),
        )
    )
    add(
        LiveCheck(
            "provider.base_url_host",
            PASS if _host(settings.llm_base_url) != "<invalid>" else FAIL,
            f"host={_host(settings.llm_base_url)}",
        )
    )

    declarations = (
        dotenv_llm_declarations(dotenv_path)
        if dotenv_path is not None and not dotenv_loaded
        else {"provider": None, "model": None, "key_names": []}
    )
    declared_provider = declarations["provider"]
    declared_keys = declarations["key_names"]
    if dotenv_loaded:
        dotenv_status = PASS
        dotenv_message = "已加载 .env（--load-dotenv）"
    elif dotenv_path is None or not Path(dotenv_path).exists():
        dotenv_status = PASS
        dotenv_message = "未提供 .env；只读取进程环境变量"
    elif declared_provider and declared_provider != provider:
        # The file the operator is looking at is not what this process sees.
        # Naming the discrepancy (never a key value) turns a confusing FAIL
        # into a one-line fix.
        dotenv_status = WARN
        dotenv_message = (
            f".env 声明 LLM_PROVIDER={declared_provider!r}，"
            f"本次进程解析为 {settings.llm_provider!r}；未加载 .env（需要 --load-dotenv）"
        )
    elif declared_keys:
        dotenv_status = WARN
        dotenv_message = (
            f"{Path(dotenv_path).name} 声明了 {declared_keys}，"
            "但本次运行未加载它；需要 --load-dotenv，或已在进程环境中导出同名变量"
        )
    else:
        dotenv_status = PASS
        dotenv_message = ".env 存在，但未声明当前 Provider 的 Key 变量"
    add(LiveCheck("config.dotenv", dotenv_status, dotenv_message))

    key_env_names = llm_api_key_env_names(provider)
    add(
        LiveCheck(
            "provider.api_key",
            PASS if settings.llm_enabled else FAIL,
            # Presence only. The value is never read, printed, or archived.
            "API Key 已配置"
            if settings.llm_enabled
            else f"API Key 未配置：检查 {'、'.join(key_env_names)}（只检查存在性）",
        )
    )
    add(
        LiveCheck(
            "provider.prompt_version",
            PASS if AGENT_PROMPT_VERSION else FAIL,
            f"prompt_version={AGENT_PROMPT_VERSION!r}",
        )
    )

    timeout_status = WARN if settings.llm_timeout_seconds > 60 else PASS
    add(
        LiveCheck(
            "runtime.timeout",
            timeout_status,
            f"LLM 超时 {settings.llm_timeout_seconds}s；"
            f"单次 Run 上限 {settings.agent_max_seconds}s",
        )
    )
    stop_ok = (
        settings.agent_max_consecutive_failures >= 1
        and settings.agent_max_tool_calls >= 1
    )
    add(
        LiveCheck(
            "runtime.stop_policy",
            PASS if stop_ok else FAIL,
            f"连续失败上限 {settings.agent_max_consecutive_failures}；"
            f"工具调用上限 {settings.agent_max_tool_calls}",
        )
    )

    prices_configured = (
        settings.model_input_price_per_million is not None
        and settings.model_output_price_per_million is not None
    )
    add(
        LiveCheck(
            "cost.prices",
            PASS if prices_configured else WARN,
            "模型单价已配置，可估算成本"
            if prices_configured
            else "未配置模型单价；成本字段将记录为 null，不做估算",
        )
    )

    if output_path is None:
        add(LiveCheck("archive.output", WARN, "未指定输出路径"))
    else:
        parent = Path(output_path).expanduser().parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            writable = parent.is_dir()
        except OSError:
            writable = False
        add(
            LiveCheck(
                "archive.output",
                PASS if writable else FAIL,
                f"输出目录 {parent} {'可写' if writable else '不可写'}",
            )
        )

    return readiness

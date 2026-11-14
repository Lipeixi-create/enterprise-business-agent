"""Live acceptance check for a real external LLM provider.

Unlike tests/test_llm_client.py (which mocks httpx), this script makes real
network calls. It verifies four levels and never prints the API key:

  1. client  - raw Anthropic Messages request returns parseable JSON
  2. planner - HybridPlanner reports provider="llm" (not a silent fallback)
  3. service - full semantic pipeline answers end to end
  4. batch   - success rate over a small question set

Usage:
    export ANTHROPIC_API_KEY=...
    export LLM_BASE_URL=http://127.0.0.1:15721/v1/messages
    export LLM_MODEL=claude-sonnet-4-6
    python scripts/verify_llm_live.py --check-config
    python scripts/verify_llm_live.py

`.env` is not loaded automatically; export the variables in this shell. Rotate
a previously exposed key at the provider console before running. Exit code is
1 when configuration is inconsistent or any level fails.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy.orm import Session

from enterprise_business_agent.config import get_settings
from enterprise_business_agent.db import make_engine
from enterprise_business_agent.llm import build_llm_client
from enterprise_business_agent.nl.contracts import NaturalLanguageRequest
from enterprise_business_agent.nl.planner import HybridPlanner
from enterprise_business_agent.nl.service import NaturalLanguageAnalyzer

DEFAULT_QUESTIONS = [
    "2025年9月华东的销售额是多少",
    "2025年第三季度华东哪个渠道卖得最好",
    "2025年11月哪个营销活动 ROI 最低",
    "9月销售额环比8月变化多少",
    "哪些商品库存风险最高",
]


def _mask(url: str) -> str:
    return url if url.startswith("http") else "<invalid>"


def check_client(settings) -> bool:
    print("[1/4] 客户端层：真实 HTTP 调用")
    client = build_llm_client(settings)
    if client is None:
        print("      FAIL no client was built for the configured provider")
        return False
    try:
        result = client.complete_json(
            system='Only output a JSON object, no prose. Schema: {"metric": "<name>"}',
            prompt='Question: "September 2025 East China sales". Use metric name sales_amount.',
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a failed check
        print(f"      FAIL {type(exc).__name__}: {exc}")
        return False
    print(
        f"      OK   {result.latency_ms:.0f}ms | upstream={result.model} "
        f"| tokens {result.input_tokens}/{result.output_tokens}"
    )
    print(f"      parsed JSON: {json.dumps(result.data, ensure_ascii=False)[:120]}")
    return True


def check_planner(settings, client) -> bool:
    print("[2/4] 规划层：是否真的由 LLM 产出结构化意图")
    planner = HybridPlanner(settings, client)
    result = planner.plan(DEFAULT_QUESTIONS[0])
    ok = result.provider == "llm" and result.intent is not None
    print(
        f"      {'OK  ' if ok else 'FAIL'} provider={result.provider} model={result.model} "
        f"latency={result.latency_ms} warnings={result.warnings}"
    )
    if result.intent is not None:
        intent = result.intent
        print(f"      intent metric={intent.metric} start={intent.start} end={intent.end} op={intent.operation}")
    return ok


def check_service(settings, client) -> bool:
    print("[3/4] 服务层：语义解析 + SQL Guard + 只读执行")
    engine = make_engine(settings.database_url)
    with Session(engine) as session:
        analyzer = NaturalLanguageAnalyzer(session, settings, llm_client=client)
        started = time.time()
        response = analyzer.analyze(NaturalLanguageRequest(question=DEFAULT_QUESTIONS[0]))
        elapsed = (time.time() - started) * 1000
    ok = response.status.value == "ok" and response.planner == "llm"
    result_type = getattr(response.result_type, "value", response.result_type)
    print(
        f"      {'OK  ' if ok else 'FAIL'} status={response.status.value} planner={response.planner} "
        f"metric={response.metric} type={result_type} total={elapsed:.0f}ms"
    )
    print(f"      llm latency={response.llm_latency_ms} tokens={response.llm_tokens} warnings={response.warnings}")
    if response.explanation:
        print(f"      explanation: {response.explanation[:120]}")
    return ok


def check_batch(settings, client, questions: list[str]) -> bool:
    print(f"[4/4] 批量成功率：{len(questions)} 个问题")
    engine = make_engine(settings.database_url)
    llm_ok = fallback = failed = 0
    with Session(engine) as session:
        analyzer = NaturalLanguageAnalyzer(session, settings, llm_client=client)
        for question in questions:
            response = analyzer.analyze(NaturalLanguageRequest(question=question))
            if response.planner == "llm" and response.status.value == "ok":
                verdict = "llm"
                llm_ok += 1
            elif response.planner == "rule":
                verdict = "回退"
                fallback += 1
            else:
                verdict = "未完成"
                failed += 1
            print(
                f"      {question[:22]:24s} {verdict:4s} status={response.status.value:14s} "
                f"metric={response.metric} latency={response.llm_latency_ms}"
            )
    print(f"      LLM 直接成功 {llm_ok} / 静默回退 {fallback} / 失败 {failed}")
    return llm_ok == len(questions)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a real external LLM end to end")
    parser.add_argument("--question", action="append", dest="questions", default=None)
    parser.add_argument("--json", action="store_true", help="print a machine-readable summary")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="only validate live LLM configuration; never prints the key",
    )
    args = parser.parse_args()

    settings = get_settings()
    print(
        f"config: provider={settings.llm_provider} model={settings.llm_model} "
        f"base_url={settings.llm_base_url} "
        f"key={'configured' if settings.llm_enabled else 'not configured'}"
    )
    if not settings.llm_enabled:
        if Path(".env").exists():
            print(f"NOTE: a .env file was found but did not provide a key for provider {settings.llm_provider!r}.")
        print(f"FAIL: no API key for provider {settings.llm_provider!r}; real LLM calls: 0")
        return 1
    issues = settings.llm_endpoint_issues()
    if issues:
        print("FAIL: live configuration is inconsistent; no request was sent:")
        for issue in issues:
            print(f"  - {issue}")
        print(
            "Fix the provider/model/base URL, and rotate a previously exposed key "
            "at the provider console before retrying."
        )
        return 1
    if args.check_config:
        protocol = (
            "OpenAI-compatible Chat Completions API"
            if settings.llm_provider.strip().lower() == "glm"
            else "Anthropic Messages API"
        )
        print(f"PASS: live LLM configuration is consistent with the {protocol}.")
        return 0
    print(
        f"provider={settings.llm_provider} model={settings.llm_model} "
        f"base_url={_mask(settings.llm_base_url)} key=<redacted>"
    )

    client = build_llm_client(settings)
    assert client is not None
    questions = args.questions or DEFAULT_QUESTIONS
    results = {
        "client": check_client(settings),
        "planner": check_planner(settings, client),
        "service": check_service(settings, client),
        "batch": check_batch(settings, client, questions),
    }
    passed = all(results.values())
    print(f"\n结果: {'PASS' if passed else 'FAIL'}  {json.dumps(results, ensure_ascii=False)}")
    if args.json:
        print(json.dumps({"passed": passed, "checks": results}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

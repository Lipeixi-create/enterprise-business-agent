from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_preflight():
    spec = importlib.util.spec_from_file_location("eba_preflight", ROOT / "scripts" / "preflight.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["eba_preflight"] = module
    spec.loader.exec_module(module)
    return module


preflight = _load_preflight()


def test_redact_url_removes_password_and_query():
    redacted = preflight.redact_url(
        "postgresql+psycopg://eba_readonly:sup3r-secret@db.internal:5432/eba?sslmode=verify-full"
    )
    assert "sup3r-secret" not in redacted
    assert "sslmode" not in redacted
    assert redacted == "postgresql+psycopg://eba_readonly@db.internal:5432/eba"


def test_preflight_json_is_read_only_and_never_prints_secrets(monkeypatch, capsys):
    monkeypatch.setenv("EBA_API_KEYS", json.dumps({"test-key": "admin"}))
    monkeypatch.setenv("LLM_API_KEY", "test-llm-secret-value")
    monkeypatch.setenv("EBA_ALLOW_ANONYMOUS_API", "false")
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("RUN_STORE_URL", "sqlite://")
    monkeypatch.delenv("READONLY_DATABASE_URL", raising=False)
    monkeypatch.delenv("EBA_LOAD_DOTENV", raising=False)

    exit_code = preflight.main(["--json"])
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["read_only"] is True
    assert payload["summary"]["fail"] >= 1  # empty in-memory store has no schema
    assert exit_code == 1
    assert "test-llm-secret-value" not in output
    assert "test-key" not in output
    names = {item["check"] for item in payload["checks"]}
    assert {
        "env.api_keys",
        "run_store.schema",
        "run_store.claim_columns",
        "observability.tracing",
    } <= names


def test_preflight_summary_counts_each_status():
    checks = [
        preflight.Check("a", preflight.OK, ""),
        preflight.Check("b", preflight.WARN, ""),
        preflight.Check("c", preflight.FAIL, ""),
        preflight.Check("d", preflight.SKIP, ""),
        preflight.Check("e", preflight.OK, ""),
    ]
    assert preflight.summarize(checks) == {
        preflight.OK: 2,
        preflight.WARN: 1,
        preflight.FAIL: 1,
        preflight.SKIP: 1,
    }

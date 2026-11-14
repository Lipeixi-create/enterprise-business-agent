"""Phase 9.3 browser end-to-end acceptance driven through the Chrome DevTools Protocol.

The driver starts a headless Chrome, walks the workbench through a real user
question, the agent investigation, report/evidence/findings views, history,
approval and a cancelled-run recovery, and writes screenshots plus a JSON
summary. It never prints API keys: keys are read from a 0600 JSON file.

Usage:
    .venv/bin/python scripts/browser_e2e.py \
        --keys-file /tmp/eba-phase93-keys.json \
        --output-dir data/exports/phase9_3_browser
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from websockets.sync.client import connect

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]

TERMINAL = {
    "completed",
    "completed_with_warnings",
    "partial",
    "failed",
    "rejected",
    "timeout",
    "llm_unavailable",
    "llm_timeout",
    "cancelled",
    "budget_exhausted",
}


class BrowserError(RuntimeError):
    pass


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Chrome:
    def __init__(self, output_dir: Path):
        binary = next((p for p in CHROME_CANDIDATES if Path(p).exists()), None)
        if binary is None:
            raise BrowserError("no Chrome/Chromium binary found")
        self.port = free_port()
        self.profile = Path(tempfile.mkdtemp(prefix="eba-chrome-"))
        self.output_dir = output_dir
        self.process = subprocess.Popen(
            [
                binary,
                "--headless=new",
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self.profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                "--disable-background-networking",
                "--window-size=1440,1000",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.ws = None
        self.next_id = 0
        self.screenshot_index = 0

    def wait_ready(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/list", timeout=2
                ) as response:
                    pages = json.load(response)
                page = next(
                    (
                        item
                        for item in pages
                        if item.get("type") == "page"
                        and item.get("webSocketDebuggerUrl")
                    ),
                    None,
                )
                if page is not None:
                    self.ws = connect(
                        page["webSocketDebuggerUrl"],
                        open_timeout=10,
                        close_timeout=10,
                        max_size=64 * 1024 * 1024,
                        legacy=True,
                    )
                    return
            except OSError:
                pass
            time.sleep(0.3)
        raise BrowserError("Chrome DevTools endpoint did not become ready")

    def send(self, method: str, params: dict | None = None, timeout: float = 60.0):
        if self.ws is None:
            raise BrowserError("CDP session is not connected")
        self.next_id += 1
        request_id = self.next_id
        self.ws.send(
            json.dumps({"id": request_id, "method": method, "params": params or {}})
        )
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserError(f"CDP call timed out: {method}")
            message = json.loads(self.ws.recv(timeout=remaining))
            if message.get("id") == request_id:
                if "error" in message:
                    raise BrowserError(f"{method} failed: {message['error']}")
                return message.get("result", {})
            # Events are irrelevant for this acceptance run.

    def evaluate(self, expression: str, *, await_promise: bool = False):
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
        )
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            raise BrowserError(f"page JS error: {details.get('text')} {details}")
        return result.get("result", {}).get("value")

    def wait_js(self, expression: str, timeout: float, interval: float = 0.5):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.evaluate(expression)
            if last:
                return last
            time.sleep(interval)
        raise BrowserError(f"condition not met in {timeout}s: {expression} (last={last!r})")

    def navigate(self, url: str, timeout: float = 60.0) -> None:
        self.send("Page.navigate", {"url": url})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.evaluate("document.readyState") == "complete":
                    return
            except BrowserError:
                pass
            time.sleep(0.3)
        raise BrowserError(f"page did not finish loading: {url}")

    def screenshot(self, name: str) -> str:
        self.screenshot_index += 1
        result = self.send(
            "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True}
        )
        path = self.output_dir / f"{self.screenshot_index:02d}-{name}.png"
        path.write_bytes(base64.b64decode(result["data"]))
        return path.name

    def close(self) -> None:
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:
            pass
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
        shutil.rmtree(self.profile, ignore_errors=True)


def set_input(expression_value: str, selector: str) -> str:
    return f"""
    (() => {{
      const node = document.querySelector({json.dumps(selector)});
      if (!node) return false;
      node.value = {json.dumps(expression_value)};
      node.dispatchEvent(new Event('input', {{bubbles: true}}));
      node.dispatchEvent(new Event('change', {{bubbles: true}}));
      return true;
    }})()
    """


def click(selector: str) -> str:
    return f"""
    (() => {{
      const node = document.querySelector({json.dumps(selector)});
      if (!node) return false;
      node.click();
      return true;
    }})()
    """


def text_of(selector: str) -> str:
    return f"(document.querySelector({json.dumps(selector)})?.textContent || '').trim()"


def run_status_expression() -> str:
    return "(() => (document.querySelector('#run-status')?.textContent || '').trim())()"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--keys-file", default="/tmp/eba-phase93-keys.json")
    parser.add_argument(
        "--output-dir", default="data/exports/phase9_3_browser"
    )
    parser.add_argument(
        "--question", default="分析2025年12月华东销售下降原因"
    )
    parser.add_argument("--run-timeout", type=float, default=360.0)
    parser.add_argument("--max-tool-calls", type=int, default=6)
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args()

    keys = json.loads(Path(args.keys_file).read_text(encoding="utf-8"))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    summary: dict = {
        "base_url": args.base_url,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "steps": [],
        "checks": {},
        "status_transitions": [],
        "screenshots": [],
    }

    def step(name: str, **extra) -> None:
        entry = {"step": name, "at": time.strftime("%H:%M:%S"), **extra}
        summary["steps"].append(entry)
        print(f"[e2e] {name}", flush=True)

    browser = Chrome(output_dir)
    try:
        browser.wait_ready()
        browser.send("Page.enable")
        browser.send("Runtime.enable")

        step("open workbench")
        browser.navigate(f"{args.base_url}/workbench/")
        browser.wait_js("!!document.querySelector('#connect')", 20)
        summary["checks"]["workbench_loaded"] = True
        summary["screenshots"].append(browser.screenshot("workbench"))

        step("connect as analyst")
        browser.evaluate(set_input(keys["analyst"], "#api-key"))
        browser.evaluate(click("#connect"))
        browser.wait_js(
            "(() => (document.querySelector('#role-badge')?.textContent || '').includes('analyst'))()",
            20,
        )
        summary["checks"]["analyst_connected"] = True
        summary["screenshots"].append(browser.screenshot("connected"))

        step("submit question")
        browser.evaluate(set_input(args.question, "#question"))
        browser.evaluate(set_input(str(args.max_tool_calls), "#max-tool-calls"))
        browser.evaluate(click("#submit-run"))
        browser.wait_js(
            "(() => document.querySelector('#run-card') && !document.querySelector('#run-card').classList.contains('hidden'))()",
            30,
        )
        browser.wait_js(
            "(() => !!document.querySelector('#run-status')?.textContent)()", 30
        )

        step("wait for run to finish")
        deadline = time.monotonic() + args.run_timeout
        seen: list[str] = []
        final_status = ""
        while time.monotonic() < deadline:
            status = (browser.evaluate(run_status_expression()) or "").strip()
            if status and (not seen or seen[-1] != status):
                seen.append(status)
            if status in TERMINAL:
                final_status = status
                break
            time.sleep(1.0)
        summary["status_transitions"] = seen
        if not final_status:
            raise BrowserError(
                f"run did not finish within {args.run_timeout}s; statuses={seen}"
            )
        summary["checks"]["run_status"] = final_status
        summary["screenshots"].append(browser.screenshot("run-finished"))
        if final_status not in {"completed", "completed_with_warnings", "partial"}:
            raise BrowserError(f"run ended in {final_status!r}: {seen}")

        step("verify answer, timeline and tool calls")
        summary["checks"]["final_answer_chars"] = len(
            browser.evaluate(text_of("#run-answer")) or ""
        )
        summary["checks"]["timeline_steps"] = browser.evaluate(
            "document.querySelectorAll('#timeline li').length"
        )
        summary["checks"]["tool_call_items"] = browser.evaluate(
            "document.querySelectorAll('#tool-calls details.tool-item').length"
        )
        summary["checks"]["trace_tab_present"] = bool(
            browser.evaluate("document.querySelector('#tab-trace')?.textContent.length")
        )
        if summary["checks"]["final_answer_chars"] <= 0:
            raise BrowserError("no final answer rendered")

        step("open report tab")
        browser.evaluate(click(".tab[data-tab='report']"))
        summary["checks"]["report_chars"] = browser.evaluate(
            "document.querySelector('#tab-report')?.textContent.trim().length || 0"
        )
        summary["checks"]["report_has_metrics"] = browser.evaluate(
            "document.querySelectorAll('#tab-report .metric-card').length"
        )
        summary["checks"]["report_has_conclusions"] = browser.evaluate(
            "document.querySelectorAll('#tab-report .conclusion').length"
        )
        summary["checks"]["report_validation_badge"] = browser.evaluate(
            "document.querySelector('#validation-status')?.textContent.trim() || ''"
        )
        summary["screenshots"].append(browser.screenshot("report"))
        if summary["checks"]["report_chars"] <= 0:
            raise BrowserError("report tab is empty")

        step("open evidence tab")
        browser.evaluate(click(".tab[data-tab='evidence']"))
        summary["checks"]["evidence_items"] = browser.evaluate(
            "document.querySelectorAll('#tab-evidence details.evidence-item').length"
        )
        summary["checks"]["evidence_traceable_badges"] = browser.evaluate(
            "(() => Array.from(document.querySelectorAll('#tab-evidence .badge')).filter(n => n.textContent.includes('可追溯')).length)()"
        )
        summary["screenshots"].append(browser.screenshot("evidence"))
        if summary["checks"]["evidence_items"] <= 0:
            raise BrowserError("no evidence rendered")

        step("open findings tab")
        browser.evaluate(click(".tab[data-tab='findings']"))
        summary["checks"]["finding_items"] = browser.evaluate(
            "document.querySelectorAll('#tab-findings .finding-item').length"
        )
        summary["checks"]["findings_empty_text"] = browser.evaluate(
            "(() => (document.querySelector('#tab-findings')?.textContent || '').includes('没有 Finding'))()"
        )
        summary["screenshots"].append(browser.screenshot("findings"))

        step("read approval state")
        browser.wait_js(
            "(() => !document.querySelector('#approval-panel').classList.contains('hidden'))()",
            15,
        )
        approval_state = browser.evaluate(text_of("#approval-state")) or ""
        summary["checks"]["approval_state"] = approval_state
        summary["checks"]["approval_panel_visible"] = True
        summary["screenshots"].append(browser.screenshot("approval"))

        step("switch to business manager")
        browser.evaluate(set_input(keys["business_manager"], "#api-key"))
        browser.evaluate(click("#connect"))
        browser.wait_js(
            "(() => (document.querySelector('#role-badge')?.textContent || '').includes('business_manager'))()",
            20,
        )
        summary["checks"]["manager_connected"] = True

        run_id = None
        meta = browser.evaluate(text_of("#run-meta")) or ""
        if "run " in meta:
            run_id = meta.split("run ", 1)[1].split("·", 1)[0].strip()
        summary["checks"]["run_id"] = run_id
        browser.evaluate("loadRun(state.runId)", await_promise=True)
        browser.wait_js(
            "(() => !document.querySelector('#approval-panel').classList.contains('hidden'))()",
            20,
        )
        approval_state = browser.evaluate(text_of("#approval-state")) or ""
        summary["checks"]["approval_state_after_manager"] = approval_state
        approve_enabled = browser.evaluate(
            "(() => { const n = document.querySelector('#approve'); return !!n && !n.disabled; })()"
        )
        summary["checks"]["approve_enabled"] = approve_enabled
        if approval_state == "pending" and approve_enabled:
            browser.evaluate(set_input("E2E approval: report reviewed", "#approval-comment"))
            browser.evaluate(click("#approve"))
            browser.wait_js(
                "(() => (document.querySelector('#approval-state')?.textContent || '').trim() === 'approved')()",
                30,
            )
            summary["checks"]["approval_decided"] = "approved"
            summary["screenshots"].append(browser.screenshot("approved"))
        else:
            summary["checks"]["approval_decided"] = "not_applicable"
            summary["checks"]["approval_disabled_reason"] = {
                "state": approval_state,
                "approve_enabled": approve_enabled,
            }

        step("history and reload")
        browser.evaluate(click("#refresh-history"))
        browser.wait_js(
            "(() => document.querySelectorAll('#history-list li').length > 0)()", 20
        )
        history_count = browser.evaluate(
            "document.querySelectorAll('#history-list li').length"
        )
        summary["checks"]["history_items"] = history_count
        first_question = browser.evaluate(
            "(document.querySelector('#history-list .history-question')?.textContent || '').trim()"
        )
        summary["checks"]["history_first_question"] = first_question
        browser.evaluate(click("#history-list li:first-child button"))
        browser.wait_js(
            "(() => !document.querySelector('#run-card').classList.contains('hidden'))()",
            20,
        )
        browser.evaluate("location.reload()")
        browser.wait_js("!!document.querySelector('#connect')", 30)
        browser.evaluate("connect()", await_promise=True)
        browser.wait_js(
            "(() => document.querySelectorAll('#history-list li').length > 0)()", 20
        )
        summary["checks"]["history_after_reload"] = browser.evaluate(
            "document.querySelectorAll('#history-list li').length"
        )
        summary["screenshots"].append(browser.screenshot("history-after-reload"))

        step("cancel a run and recover")
        browser.evaluate(set_input(args.question, "#question"))
        browser.evaluate(click("#submit-run"))
        browser.wait_js(
            "(() => { const n = document.querySelector('#cancel-run'); return !!n && !n.classList.contains('hidden'); })()",
            30,
        )
        browser.evaluate(click("#cancel-run"))
        cancel_seen: list[str] = []
        deadline = time.monotonic() + 120
        cancelled_status = ""
        while time.monotonic() < deadline:
            status = (browser.evaluate(run_status_expression()) or "").strip()
            if status and (not cancel_seen or cancel_seen[-1] != status):
                cancel_seen.append(status)
            if status in TERMINAL:
                cancelled_status = status
                break
            time.sleep(0.5)
        summary["checks"]["cancel_transitions"] = cancel_seen
        summary["checks"]["cancel_status"] = cancelled_status or "not_terminal"
        summary["screenshots"].append(browser.screenshot("cancelled-run"))

        browser.evaluate(click("#refresh-history"))
        browser.wait_js(
            "(() => document.querySelectorAll('#history-list li').length >= 2)()", 20
        )
        summary["checks"]["history_items_after_cancel"] = browser.evaluate(
            "document.querySelectorAll('#history-list li').length"
        )
        summary["checks"]["cancelled_run_usable_ui"] = browser.evaluate(
            "(() => !!document.querySelector('#submit-run') && !document.querySelector('#submit-run').disabled)()"
        )
        summary["screenshots"].append(browser.screenshot("final"))

        summary["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        summary["ok"] = True
    except Exception as exc:  # noqa: BLE001 - acceptance driver reports the failure
        summary["ok"] = False
        summary["error"] = f"{type(exc).__name__}: {exc}"
        try:
            summary["screenshots"].append(browser.screenshot("failure"))
        except Exception:
            pass
        print(f"[e2e] FAILED: {summary['error']}", file=sys.stderr)
        return_code = 1
    else:
        return_code = 0
    finally:
        if not args.keep_open:
            browser.close()

    summary_path = output_dir / "browser_e2e.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return return_code


if __name__ == "__main__":
    sys.exit(main())

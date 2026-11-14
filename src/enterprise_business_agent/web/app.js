const state = {
  key: sessionStorage.getItem("eba_api_key") || "",
  principal: null,
  runId: null,
  run: null,
  approval: null,
  history: [],
  streaming: false,
  streamAbort: null,
};

const $ = (id) => document.getElementById(id);

const els = {
  apiKey: $("api-key"),
  connect: $("connect"),
  roleBadge: $("role-badge"),
  globalError: $("global-error"),
  historyList: $("history-list"),
  historyEmpty: $("history-empty"),
  runScope: $("run-scope"),
  refreshHistory: $("refresh-history"),
  question: $("question"),
  maxToolCalls: $("max-tool-calls"),
  asyncMode: $("async-mode"),
  submitRun: $("submit-run"),
  cancelRun: $("cancel-run"),
  submitStatus: $("submit-status"),
  runCard: $("run-card"),
  runTitle: $("run-title"),
  runMeta: $("run-meta"),
  runStatus: $("run-status"),
  validationStatus: $("validation-status"),
  runAnswer: $("run-answer"),
  runErrors: $("run-errors"),
  timeline: $("timeline"),
  toolCalls: $("tool-calls"),
  tabReport: $("tab-report"),
  tabEvidence: $("tab-evidence"),
  tabFindings: $("tab-findings"),
  tabTrace: $("tab-trace"),
  approvalPanel: $("approval-panel"),
  approvalMeta: $("approval-meta"),
  approvalState: $("approval-state"),
  approvalComment: $("approval-comment"),
  approve: $("approve"),
  reject: $("reject"),
};

class ApiError extends Error {
  constructor(message, status, code, detail) {
    super(message);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

function errorText(payload, fallback) {
  if (payload && payload.error) {
    const code = payload.error.code ? `[${payload.error.code}] ` : "";
    return `${code}${payload.error.message || fallback}`;
  }
  if (payload && payload.detail) return String(payload.detail);
  return fallback;
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (state.key) headers["X-API-Key"] = state.key;
  if (options.body !== undefined && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  let response;
  try {
    response = await fetch(path, { ...options, headers });
  } catch (error) {
    throw new ApiError(`网络请求失败：${error.message}`, 0, "network_error", null);
  }
  const text = await response.text();
  let payload = null;
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = null; }
  }
  if (!response.ok) {
    throw new ApiError(
      errorText(payload, `请求失败（HTTP ${response.status}）`),
      response.status,
      payload?.error?.code,
      payload?.error?.detail || null
    );
  }
  return payload;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function showError(message) {
  els.globalError.textContent = message;
  els.globalError.classList.remove("hidden");
}

function clearError() {
  els.globalError.textContent = "";
  els.globalError.classList.add("hidden");
}

function badge(text, kind = "muted") {
  return el("span", `badge badge-${kind}`, text);
}

const TERMINAL_STATUSES = new Set([
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
]);

function statusKind(status) {
  if (!status) return "muted";
  if (["completed", "approved", "validated", "ok"].includes(status)) return "ok";
  if (["pending", "running", "queued", "completed_with_warnings", "partial"].includes(status)) return "warn";
  if (["failed", "rejected", "timeout", "llm_unavailable", "llm_timeout", "cancelled", "budget_exhausted"].includes(status)) return "error";
  return "muted";
}

function setBadge(node, text, kind = "muted") {
  node.className = `badge badge-${kind}`;
  node.textContent = text || "-";
}

function fmtTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString();
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "-";
  return `${Number(seconds).toFixed(2)}s`;
}

async function connect() {
  clearError();
  const key = els.apiKey.value.trim();
  state.key = key;
  if (key) sessionStorage.setItem("eba_api_key", key);
  else sessionStorage.removeItem("eba_api_key");
  try {
    const principal = await api("/api/v1/me");
    state.principal = principal;
    const label = principal.authenticated
      ? `${principal.role}${principal.principal_id ? " · " + principal.principal_id : ""}`
      : `${principal.role}（匿名）`;
    setBadge(els.roleBadge, label, principal.authenticated ? "ok" : "warn");
    await loadHistory();
  } catch (error) {
    setBadge(els.roleBadge, "连接失败", "error");
    showError(error.message);
  }
}

function renderHistory() {
  clear(els.historyList);
  els.historyEmpty.classList.toggle("hidden", state.history.length > 0);
  for (const item of state.history) {
    const li = el("li");
    const button = el("button", "history-item");
    if (item.run_id === state.runId) button.classList.add("active");
    button.type = "button";
    button.appendChild(el("span", "history-question", item.question || item.run_id));
    const meta = el("span", "history-meta");
    meta.appendChild(badge(item.status || "unknown", statusKind(item.status)));
    if (item.stop_reason) meta.appendChild(el("span", "item-meta", item.stop_reason));
    meta.appendChild(el("span", "item-meta", fmtTime(item.updated_at)));
    button.appendChild(meta);
    button.addEventListener("click", () => loadRun(item.run_id));
    li.appendChild(button);
    els.historyList.appendChild(li);
  }
}

async function loadHistory() {
  try {
    const payload = await api("/api/v1/agent/runs?limit=40");
    state.history = payload.items || [];
    els.runScope.textContent = payload.scope === "all" ? "当前角色可查看全部运行" : "仅显示本账号运行";
    renderHistory();
  } catch (error) {
    showError(error.message);
  }
}

function setRunStatus(status, extra) {
  setBadge(els.runStatus, status || "-", statusKind(status));
  if (extra !== undefined) els.runMeta.textContent = extra;
}

function applyStatusEvent(payload) {
  const status = payload.status || "unknown";
  setBadge(els.runStatus, status, statusKind(status));
  const stop = payload.stop_reason ? ` · ${payload.stop_reason}` : "";
  els.submitStatus.textContent = `运行状态：${status}${stop}`;
}

function appendTimeline(text, meta) {
  const li = el("li");
  li.appendChild(el("div", "step-title", text));
  if (meta) li.appendChild(el("div", "item-meta", meta));
  els.timeline.appendChild(li);
  els.timeline.scrollTop = els.timeline.scrollHeight;
}

function renderRun(run) {
  state.run = run;
  state.runId = run.run_id;
  els.runCard.classList.remove("hidden");
  els.runTitle.textContent = run.question || "运行";
  setBadge(els.runStatus, run.status || "-", statusKind(run.status));
  const meta = [
    `run ${run.run_id}`,
    `角色 ${run.role || "-"}`,
    `开始 ${fmtTime(run.started_at)}`,
    `耗时 ${fmtDuration(run.execution_seconds)}`,
  ];
  if (run.stop_reason) meta.push(`停止原因 ${run.stop_reason}`);
  els.runMeta.textContent = meta.join(" · ");

  els.validationStatus.classList.add("hidden");
  if (run.report_validation_passed !== null && run.report_validation_passed !== undefined) {
    els.validationStatus.classList.remove("hidden");
    setBadge(
      els.validationStatus,
      run.report_validation_passed ? "数字/引用校验通过" : "校验未通过",
      run.report_validation_passed ? "ok" : "error"
    );
  }

  els.runAnswer.classList.toggle("hidden", !run.final_answer);
  els.runAnswer.textContent = run.final_answer || "";

  clear(els.runErrors);
  const errors = run.errors || [];
  const stateErrors = (run.state && run.state.errors) || [];
  const allErrors = errors.length ? errors : stateErrors;
  els.runErrors.classList.toggle("hidden", allErrors.length === 0);
  for (const error of allErrors) {
    const message = error.message || error.code || JSON.stringify(error);
    els.runErrors.appendChild(el("div", "error-row", message));
  }

  clear(els.timeline);
  const steps = (run.state && run.state.executed_steps) || run.steps || [];
  if (!steps.length) {
    els.timeline.appendChild(el("li", "muted", "暂无步骤"));
  } else {
    for (const step of steps) {
      const li = el("li");
      const title = step.tool ? `${step.action} · ${step.tool}` : step.action;
      li.appendChild(el("div", "step-title", title));
      const notes = [step.reason, step.blocked_reason].filter(Boolean).join(" · ");
      if (notes) li.appendChild(el("div", "item-meta", notes));
      const budget = [];
      if (step.remaining_tool_calls !== undefined && step.remaining_tool_calls !== null) budget.push(`剩余工具 ${step.remaining_tool_calls}`);
      if (step.remaining_seconds !== undefined && step.remaining_seconds !== null) budget.push(`剩余 ${Number(step.remaining_seconds).toFixed(1)}s`);
      if (budget.length) li.appendChild(el("div", "item-meta", budget.join(" · ")));
      els.timeline.appendChild(li);
    }
  }

  clear(els.toolCalls);
  const calls = run.tool_calls || [];
  if (!calls.length) {
    els.toolCalls.appendChild(el("div", "empty", "暂无工具调用"));
  } else {
    for (const call of calls) {
      const details = el("details", "tool-item");
      const summary = el("summary");
      const head = el("div", "item-head");
      head.appendChild(el("span", "item-title", call.tool || "tool"));
      head.appendChild(badge(call.status || "unknown", statusKind(call.status)));
      if (call.reused) head.appendChild(badge("缓存复用", "muted"));
      if (call.duration_ms !== undefined) head.appendChild(el("span", "item-meta", `${Number(call.duration_ms).toFixed(1)}ms`));
      summary.appendChild(head);
      details.appendChild(summary);
      if (call.summary) details.appendChild(el("p", "small", call.summary));
      const meta = [];
      if (call.row_count !== undefined && call.row_count !== null) meta.push(`行数 ${call.row_count}`);
      if (call.displayed_count !== undefined && call.displayed_count !== null) meta.push(`展示 ${call.displayed_count}`);
      if (call.truncated) meta.push("已截断");
      if (call.error_code) meta.push(`错误 ${call.error_code}`);
      if (meta.length) details.appendChild(el("div", "item-meta", meta.join(" · ")));
      details.appendChild(el("pre", "json", JSON.stringify(call.arguments || {}, null, 2)));
      els.toolCalls.appendChild(details);
    }
  }

  renderTrace(run);
  els.tabTrace.classList.remove("hidden");
}

function renderTrace(run) {
  clear(els.tabTrace);
  if (run.state?.tool_messages?.length) {
    els.tabTrace.appendChild(el("h3", null, "模型实际看到的工具消息"));
    const list = el("div", "stack");
    for (const message of run.state.tool_messages) {
      const details = el("details", "tool-item");
      const summary = el("summary");
      summary.appendChild(el("span", "item-title", `${message.tool || "tool"} · ${message.status}`));
      details.appendChild(summary);
      details.appendChild(el("div", "item-meta", `trusted=${message.trusted} · evidence=${(message.evidence_ids || []).length}`));
      details.appendChild(el("pre", "json", message.content || ""));
      list.appendChild(details);
    }
    els.tabTrace.appendChild(list);
  }
  if (run.state?.relation_issues?.length) {
    els.tabTrace.appendChild(el("h3", null, "数字/关系校验问题"));
    els.tabTrace.appendChild(el("pre", "json", JSON.stringify(run.state.relation_issues, null, 2)));
  }
  els.tabTrace.appendChild(el("h3", null, "原始运行响应"));
  els.tabTrace.appendChild(el("pre", "json", JSON.stringify(run, null, 2)));
}

function renderReport(stored) {
  clear(els.tabReport);
  if (!stored || !stored.report) {
    els.tabReport.appendChild(el("div", "empty", "没有报告"));
    return;
  }
  const report = stored.report;
  const wrap = el("div", "report");
  const validation = stored.validation || {};
  const head = el("div", "item-head");
  head.appendChild(badge(validation.passed ? "校验通过" : "校验未通过", validation.passed ? "ok" : "error"));
  if (report.narrative_mode) head.appendChild(badge(`narrative: ${report.narrative_mode}`, "muted"));
  wrap.appendChild(head);

  wrap.appendChild(el("h3", null, "目标"));
  wrap.appendChild(el("p", null, report.objective || "-"));
  if (report.executive_summary) {
    wrap.appendChild(el("h3", null, "执行摘要"));
    wrap.appendChild(el("p", null, report.executive_summary));
  }

  if (report.key_metrics?.length) {
    wrap.appendChild(el("h3", null, "关键指标"));
    const grid = el("div", "metric-grid");
    for (const card of report.key_metrics) {
      const box = el("div", "metric-card");
      box.appendChild(el("div", "item-meta", `${card.label || card.metric} · ${card.period || ""}`));
      const value = card.value ? `${card.value.value}${card.value.unit ? " " + card.value.unit : ""}` : "-";
      box.appendChild(el("div", "metric-value", value));
      if (card.change?.value !== undefined) {
        box.appendChild(el("div", "item-meta", `变化 ${card.change.value}${card.change.unit ? " " + card.change.unit : ""}`));
      }
      grid.appendChild(box);
    }
    wrap.appendChild(grid);
  }

  const sections = [
    ["结论", report.conclusions],
    ["异常与贡献", report.anomalies_and_contributions],
    ["跨域观察", report.cross_domain],
  ];
  for (const [title, items] of sections) {
    if (!items?.length) continue;
    wrap.appendChild(el("h3", null, title));
    for (const item of items) {
      const box = el("div", "conclusion");
      box.appendChild(el("p", "statement", item.statement));
      const refs = [];
      if (item.certainty) refs.push(item.certainty);
      if (item.finding_ids?.length) refs.push(`findings: ${item.finding_ids.join(", ")}`);
      if (item.evidence_ids?.length) refs.push(`evidence: ${item.evidence_ids.join(", ")}`);
      if (refs.length) box.appendChild(el("div", "refs", refs.join(" · ")));
      wrap.appendChild(box);
    }
  }

  if (report.domain_analysis?.length) {
    wrap.appendChild(el("h3", null, "分域分析"));
    for (const section of report.domain_analysis) {
      const box = el("div", "conclusion");
      box.appendChild(el("div", "item-title", section.domain));
      box.appendChild(el("p", null, section.narrative || ""));
      if (section.finding_ids?.length) box.appendChild(el("div", "refs", `findings: ${section.finding_ids.join(", ")}`));
      wrap.appendChild(box);
    }
  }

  if (report.risks_and_uncertainty?.length) {
    wrap.appendChild(el("h3", null, "风险与不确定性"));
    const ul = el("ul");
    for (const item of report.risks_and_uncertainty) ul.appendChild(el("li", null, item));
    wrap.appendChild(ul);
  }

  if (report.recommendations?.length) {
    wrap.appendChild(el("h3", null, "建议"));
    for (const item of report.recommendations) {
      const box = el("div", "conclusion");
      box.appendChild(el("div", "item-title", item.action || "建议"));
      if (item.rationale) box.appendChild(el("p", null, item.rationale));
      const meta = [];
      if (item.expected_direction) meta.push(`预期方向：${item.expected_direction}`);
      if (item.risk) meta.push(`风险：${item.risk}`);
      if (item.validation_metric) meta.push(`验证指标：${item.validation_metric}`);
      if (item.requires_approval) meta.push("需要审批");
      if (meta.length) box.appendChild(el("div", "refs", meta.join(" · ")));
      wrap.appendChild(box);
    }
  }

  if (report.next_validation?.length) {
    wrap.appendChild(el("h3", null, "下一步验证"));
    const ul = el("ul");
    for (const item of report.next_validation) ul.appendChild(el("li", null, item));
    wrap.appendChild(ul);
  }

  if (report.data_scope) {
    wrap.appendChild(el("h3", null, "数据范围"));
    wrap.appendChild(el("pre", "json", JSON.stringify(report.data_scope, null, 2)));
  }
  if (report.warnings?.length) {
    wrap.appendChild(el("h3", null, "警告"));
    const ul = el("ul");
    for (const item of report.warnings) ul.appendChild(el("li", null, item));
    wrap.appendChild(ul);
  }
  if (validation.issues?.length) {
    wrap.appendChild(el("h3", null, "校验问题"));
    wrap.appendChild(el("pre", "json", JSON.stringify(validation.issues, null, 2)));
  }
  els.tabReport.appendChild(wrap);
}

function renderEvidence(payload) {
  clear(els.tabEvidence);
  const items = payload?.evidence || [];
  if (!items.length) {
    els.tabEvidence.appendChild(el("div", "empty", "没有 Evidence"));
    return;
  }
  const list = el("div", "stack");
  for (const item of items) {
    const details = el("details", "evidence-item");
    const summary = el("summary");
    const head = el("div", "item-head");
    head.appendChild(el("span", "item-title", item.metric_code || item.evidence_type || item.evidence_id));
    head.appendChild(badge(item.status || "unknown", statusKind(item.status)));
    if (item.traceable) head.appendChild(badge("可追溯", "ok"));
    summary.appendChild(head);
    details.appendChild(summary);
    const meta = [];
    if (item.time_range_start || item.time_range_end) meta.push(`${item.time_range_start || "?"} → ${item.time_range_end || "?"}`);
    if (item.source_tables?.length) meta.push(`来源表：${item.source_tables.join(", ")}`);
    if (meta.length) details.appendChild(el("div", "item-meta", meta.join(" · ")));
    if (item.result_summary) details.appendChild(el("p", "small", item.result_summary));
    details.appendChild(el("pre", "json", JSON.stringify(item, null, 2)));
    list.appendChild(details);
  }
  els.tabEvidence.appendChild(list);
}

function renderFindings(payload) {
  clear(els.tabFindings);
  const items = payload?.findings || [];
  if (!items.length) {
    els.tabFindings.appendChild(el("div", "empty", "没有 Finding"));
    return;
  }
  const list = el("div", "stack");
  for (const item of items) {
    const box = el("div", "finding-item");
    const head = el("div", "item-head");
    head.appendChild(el("span", "item-title", item.title || item.finding_id));
    head.appendChild(badge(item.severity || "secondary", item.severity === "primary" ? "warn" : "muted"));
    head.appendChild(el("span", "item-meta", `置信度 ${Number(item.confidence ?? 0).toFixed(2)}`));
    box.appendChild(head);
    box.appendChild(el("p", null, item.statement || ""));
    if (item.explanation) box.appendChild(el("p", "small", item.explanation));
    if (item.evidence_ids?.length) box.appendChild(el("div", "refs", `evidence: ${item.evidence_ids.join(", ")}`));
    if (item.limitations?.length) box.appendChild(el("div", "refs", `限制：${item.limitations.join("；")}`));
    list.appendChild(box);
  }
  els.tabFindings.appendChild(list);
}

function renderApproval(approval) {
  state.approval = approval;
  if (!approval) {
    els.approvalPanel.classList.add("hidden");
    return;
  }
  els.approvalPanel.classList.remove("hidden");
  setBadge(els.approvalState, approval.state || "unknown", statusKind(approval.state));
  const meta = [
    `审批人 ${approval.approver || "-"}`,
    `报告版本 ${approval.report_version ?? "-"}`,
    `请求时间 ${fmtTime(approval.requested_at)}`,
  ];
  if (approval.stale) meta.push("报告指纹已变化，需重新加载");
  if (approval.comment) meta.push(`意见：${approval.comment}`);
  els.approvalMeta.textContent = meta.join(" · ");

  const scopes = state.principal?.scopes || [];
  const canDecide = scopes.includes("approval:decide");
  const pending = approval.state === "pending";
  const mine = state.run && state.run.principal_id === state.principal?.principal_id;
  const disabled = !canDecide || !pending || approval.stale || mine;
  els.approve.disabled = disabled;
  els.reject.disabled = disabled;
  els.approvalComment.disabled = disabled;
}

async function loadApproval(runId) {
  try {
    const approval = await api(`/api/v1/agent/runs/${encodeURIComponent(runId)}/report/approval`);
    renderApproval(approval);
  } catch (error) {
    if (error.status === 404 || error.status === 503) {
      renderApproval(null);
      return;
    }
    throw error;
  }
}

async function loadRun(runId) {
  clearError();
  try {
    const run = await api(`/api/v1/agent/runs/${encodeURIComponent(runId)}`);
    renderRun(run);
    const [evidence, findings, report] = await Promise.allSettled([
      api(`/api/v1/agent/runs/${encodeURIComponent(runId)}/evidence`),
      api(`/api/v1/agent/runs/${encodeURIComponent(runId)}/findings`),
      api(`/api/v1/agent/runs/${encodeURIComponent(runId)}/report`),
    ]);
    renderEvidence(evidence.status === "fulfilled" ? evidence.value : null);
    renderFindings(findings.status === "fulfilled" ? findings.value : null);
    renderReport(report.status === "fulfilled" ? report.value : null);
    await loadApproval(runId);
    await loadHistory();
  } catch (error) {
    showError(error.message);
  }
}

function parseSseBlock(block) {
  let event = "message";
  const dataLines = [];
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (!dataLines.length) return null;
  const raw = dataLines.join("\n");
  let payload = raw;
  try { payload = JSON.parse(raw); } catch { /* keep text */ }
  return { event, payload };
}

function describeEvent(event, payload) {
  if (payload && typeof payload === "object") {
    if (payload.event_type) {
      const detail = payload.detail && typeof payload.detail === "object" ? JSON.stringify(payload.detail) : "";
      return [payload.event_type, detail];
    }
    if (payload.status) return ["status", `${payload.status}${payload.stop_reason ? " · " + payload.stop_reason : ""}`];
    return [event, JSON.stringify(payload)];
  }
  return [event, String(payload ?? "")];
}

async function streamRun(runId) {
  state.streamAbort = new AbortController();
  const headers = { Accept: "text/event-stream" };
  if (state.key) headers["X-API-Key"] = state.key;
  const response = await fetch(`/api/v1/agent/runs/${encodeURIComponent(runId)}/stream`, {
    headers,
    signal: state.streamAbort.signal,
  });
  if (!response.ok || !response.body) {
    const text = await response.text();
    let payload = null;
    try { payload = JSON.parse(text); } catch { /* ignore */ }
    throw new ApiError(errorText(payload, `进度流打开失败（HTTP ${response.status}）`), response.status);
  }
  appendTimeline("进度流已连接", `run ${runId}`);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split(/\r?\n\r?\n/);
    buffer = blocks.pop() || "";
    for (const block of blocks) {
      const parsed = parseSseBlock(block);
      if (!parsed) continue;
      if (parsed.event === "status") {
        if (parsed.payload && typeof parsed.payload === "object") applyStatusEvent(parsed.payload);
        continue;
      }
      if (parsed.event === "done") continue;
      if (parsed.event === "error") {
        appendTimeline("进度流错误", JSON.stringify(parsed.payload));
        continue;
      }
      const [title, detail] = describeEvent(parsed.event, parsed.payload);
      appendTimeline(title, detail);
    }
  }
  appendTimeline("进度流结束，加载最终结果");
}

async function submitRun(event) {
  event.preventDefault();
  clearError();
  const question = els.question.value.trim();
  if (!question) return;
  const idempotencyKey = (crypto.randomUUID && crypto.randomUUID()) || `ui-${Date.now()}`;
  const payload = { question };
  const maxCalls = Number(els.maxToolCalls.value);
  if (Number.isFinite(maxCalls) && maxCalls > 0) payload.max_tool_calls = maxCalls;

  els.submitRun.disabled = true;
  els.cancelRun.classList.add("hidden");
  els.submitStatus.textContent = "提交中…";
  state.streaming = true;
  clear(els.timeline);
  clear(els.toolCalls);
  clear(els.tabReport);
  clear(els.tabEvidence);
  clear(els.tabFindings);
  clear(els.tabTrace);
  els.runCard.classList.remove("hidden");
  els.runTitle.textContent = question;
  els.runMeta.textContent = "正在提交…";
  setRunStatus("queued");
  try {
    if (els.asyncMode.checked) {
      const submitted = await api("/api/v1/agent/runs/async", {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: JSON.stringify(payload),
      });
      state.runId = submitted.run_id;
      els.cancelRun.classList.remove("hidden");
      setRunStatus(submitted.status || "queued", `run ${submitted.run_id}`);
      await streamRun(submitted.run_id);
    } else {
      const run = await api("/api/v1/agent/runs", {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: JSON.stringify(payload),
      });
      state.runId = run.run_id;
      renderRun(run);
    }
    await loadRun(state.runId);
    els.submitStatus.textContent = "已完成";
  } catch (error) {
    if (error.name === "AbortError") {
      els.submitStatus.textContent = "进度流已断开";
    } else {
      showError(error.message);
      els.submitStatus.textContent = "失败";
    }
  } finally {
    state.streaming = false;
    els.submitRun.disabled = false;
    els.cancelRun.classList.add("hidden");
    state.streamAbort = null;
  }
}

async function cancelRun() {
  if (!state.runId) return;
  const runId = state.runId;
  try {
    await api(`/api/v1/agent/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
    els.submitStatus.textContent = "已请求取消";
    if (state.streamAbort) state.streamAbort.abort();
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const run = await api(`/api/v1/agent/runs/${encodeURIComponent(runId)}`);
      renderRun(run);
      if (TERMINAL_STATUSES.has(run.status)) break;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    await loadRun(runId);
  } catch (error) {
    showError(error.message);
  }
}

async function decide(action) {
  if (!state.runId || !state.approval) return;
  const approval = state.approval;
  if (!approval.current_report_fingerprint) {
    showError("审批响应缺少当前报告指纹，无法安全提交");
    return;
  }
  els.approve.disabled = true;
  els.reject.disabled = true;
  try {
    await api(`/api/v1/agent/runs/${encodeURIComponent(state.runId)}/report/approval/decision`, {
      method: "POST",
      body: JSON.stringify({
        action,
        report_version: approval.report_version,
        report_fingerprint: approval.current_report_fingerprint,
        comment: els.approvalComment.value.trim() || null,
      }),
    });
    els.submitStatus.textContent = action === "approve" ? "报告已通过" : "报告已驳回";
    await loadRun(state.runId);
  } catch (error) {
    showError(error.message);
  } finally {
    els.approve.disabled = false;
    els.reject.disabled = false;
  }
}

function switchTab(name) {
  for (const button of document.querySelectorAll(".tab")) {
    button.classList.toggle("active", button.dataset.tab === name);
  }
  for (const panel of document.querySelectorAll(".tab-panel")) {
    panel.classList.add("hidden");
  }
  const target = $(`tab-${name}`);
  if (target) target.classList.remove("hidden");
}

function boot() {
  els.apiKey.value = state.key;
  els.connect.addEventListener("click", connect);
  els.apiKey.addEventListener("keydown", (event) => {
    if (event.key === "Enter") connect();
  });
  els.refreshHistory.addEventListener("click", loadHistory);
  $("ask-form").addEventListener("submit", submitRun);
  els.cancelRun.addEventListener("click", cancelRun);
  els.approve.addEventListener("click", () => decide("approve"));
  els.reject.addEventListener("click", () => decide("reject"));
  for (const button of document.querySelectorAll(".tab")) {
    button.addEventListener("click", () => switchTab(button.dataset.tab));
  }
  if (state.key) connect();
}

boot();

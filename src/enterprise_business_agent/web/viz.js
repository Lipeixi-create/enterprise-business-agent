const $ = (id) => document.getElementById(id);
const SVGNS = "http://www.w3.org/2000/svg";
const PALETTE = ["#1f5eff", "#0f9179", "#c2740d", "#7a4bd0", "#c0392b", "#2b7a9e", "#6b7f2b", "#8a5a7d"];
const STATUS_COLORS = {
  completed: "#1c7a4a",
  running: "#1f5eff",
  queued: "#1f5eff",
  failed: "#b42626",
  error: "#b42626",
  llm_unavailable: "#b42626",
  timeout: "#8a5a00",
  llm_timeout: "#8a5a00",
  cancelled: "#647083",
  interrupted: "#8a5a00",
};

const GRAIN_LABEL = { month: "月", quarter: "季", year: "年", total: "整段合计" };

const state = {
  key: sessionStorage.getItem("eba_api_key") || "",
  principal: null,
  catalog: [],
  metric: "sales_amount",
  dimension: "",
  grain: "month",
  periods: [],
  series: [],
  unit: "CNY",
  hidden: new Set(),
  result: null,
  qualityLoaded: false,
};

class ApiError extends Error {
  constructor(message, status, code) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function api(path, options = {}) {
  const headers = Object.assign({ Accept: "application/json" }, options.headers || {});
  if (state.key) headers["X-API-Key"] = state.key;
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, Object.assign({}, options, { headers }));
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (err) {
      data = null;
    }
  }
  if (!response.ok) {
    const error = data && data.error;
    throw new ApiError(
      (error && error.message) || `请求失败（HTTP ${response.status}）`,
      response.status,
      error && error.code
    );
  }
  return data;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function svgEl(tag, attrs) {
  const node = document.createElementNS(SVGNS, tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      node.setAttribute(key, String(value));
    }
  }
  return node;
}

function showError(message) {
  const banner = $("global-error");
  banner.textContent = message;
  banner.classList.remove("hidden");
}

function clearError() {
  $("global-error").classList.add("hidden");
}

function setBadge(text, kind) {
  const badge = $("role-badge");
  badge.textContent = text;
  badge.className = "badge " + (kind === "ok" ? "badge-ok" : kind === "error" ? "badge-error" : "badge-muted");
}

function setStatus(text) {
  $("load-status").textContent = text || "";
}

function fmtValue(value, unit) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  if (unit === "ratio") return (value * 100).toFixed(1) + "%";
  if (unit === "CNY") {
    const abs = Math.abs(value);
    if (abs >= 1e8) return (value / 1e8).toFixed(2) + " 亿";
    if (abs >= 1e4) return (value / 1e4).toFixed(1) + " 万";
    return value.toFixed(0);
  }
  if (unit === "units" || unit === "count" || unit === "people") {
    return Math.round(value).toLocaleString("zh-CN");
  }
  if (unit === "days") return value.toFixed(1) + " 天";
  if (unit === "turns") return value.toFixed(2);
  return value.toFixed(2);
}

function fmtFull(value, unit) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  if (unit === "ratio") return (value * 100).toFixed(2) + "%";
  if (unit === "CNY") return value.toLocaleString("zh-CN", { maximumFractionDigits: 0 }) + " 元";
  if (unit === "units") return Math.round(value).toLocaleString("zh-CN") + " 件";
  if (unit === "count") return Math.round(value).toLocaleString("zh-CN");
  if (unit === "people") return Math.round(value).toLocaleString("zh-CN") + " 人";
  if (unit === "days") return value.toFixed(1) + " 天";
  if (unit === "turns") return value.toFixed(2) + " 次";
  return value.toFixed(2);
}

function niceTicks(min, max, count) {
  const span = max - min || Math.abs(max) || 1;
  const rough = span / count;
  const magnitude = Math.pow(10, Math.floor(Math.log10(rough)));
  const normalized = rough / magnitude;
  const step = (normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10) * magnitude;
  const start = Math.floor(min / step) * step;
  const end = Math.ceil(max / step) * step;
  const ticks = [];
  for (let value = start; value <= end + step * 0.5; value += step) {
    ticks.push(Number(value.toPrecision(12)));
    if (ticks.length > 20) break;
  }
  return ticks;
}

async function loadCatalog() {
  const response = await fetch("./metrics_catalog.json", { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error("指标目录 metrics_catalog.json 加载失败");
  const payload = await response.json();
  state.catalog = payload.metrics || [];
  if (!state.catalog.some((item) => item.name === state.metric)) {
    state.metric = state.catalog.length ? state.catalog[0].name : "";
  }
  fillMetricOptions();
  fillDimensionOptions();
}

function fillMetricOptions() {
  const select = $("metric");
  select.textContent = "";
  const domains = new Map();
  for (const metric of state.catalog) {
    const domain = metric.domain || "其他";
    if (!domains.has(domain)) domains.set(domain, []);
    domains.get(domain).push(metric);
  }
  for (const [domain, metrics] of domains) {
    const group = document.createElement("optgroup");
    group.label = domain;
    for (const metric of metrics) {
      const option = el("option", null, `${metric.label}（${metric.name}）`);
      option.value = metric.name;
      group.appendChild(option);
    }
    select.appendChild(group);
  }
  select.value = state.metric;
}

function currentMetric() {
  return state.catalog.find((item) => item.name === state.metric) || null;
}

function fillDimensionOptions() {
  const metric = currentMetric();
  const select = $("dimension");
  select.textContent = "";
  const none = el("option", null, "不拆分");
  none.value = "";
  select.appendChild(none);
  const dims = (metric && metric.dimensions) || [];
  for (const dim of dims) {
    const option = el("option", null, dimLabel(dim));
    option.value = dim;
    select.appendChild(option);
  }
  if (!dims.includes(state.dimension)) state.dimension = "";
  select.value = state.dimension;
}

function dimLabel(dim) {
  const names = { region: "区域", channel: "渠道", product: "产品", campaign: "活动", department: "部门", supplier: "供应商" };
  return names[dim] || dim;
}

async function connect() {
  state.key = $("api-key").value.trim();
  sessionStorage.setItem("eba_api_key", state.key);
  try {
    const me = await api("/api/v1/me");
    state.principal = me;
    setBadge(`${me.role}${me.authenticated ? "" : "（匿名）"}`, "ok");
    clearError();
    await query(true);
  } catch (err) {
    setBadge("连接失败", "error");
    showError(err.message);
  }
}

function buildQueryBody(useRange) {
  const body = {
    metric: state.metric,
    grain: state.grain,
    dimensions: state.dimension ? [state.dimension] : [],
  };
  if (useRange) {
    const start = $("start").value;
    const end = $("end").value;
    if (start) body.start = `${start}-01`;
    if (end) body.end = `${end}-01`;
  }
  return body;
}

let queryToken = 0;

async function query(reset) {
  if (!state.metric) return;
  const token = ++queryToken;
  setStatus("加载中…");
  try {
    const body = buildQueryBody(!reset);
    const data = await api("/api/v1/metrics/query", { method: "POST", body: JSON.stringify(body) });
    if (token !== queryToken) return;
    if (!data || data.status !== "ok" || !data.result) {
      const message = (data && data.error && data.error.message) || "查询未返回可用结果";
      setStatus("");
      showError(message);
      return;
    }
    clearError();
    renderResult(data.result);
    setStatus("");
  } catch (err) {
    if (token !== queryToken) return;
    setStatus("");
    showError(err.message);
  }
}

function pivot(result) {
  const points = result.points || [];
  const periods = [];
  const seen = new Set();
  for (const point of points) {
    if (!seen.has(point.period)) {
      seen.add(point.period);
      periods.push(point.period);
    }
  }
  let dimKey = null;
  const buckets = new Map();
  for (const point of points) {
    const dims = point.dimensions || {};
    const keys = Object.keys(dims);
    if (!dimKey && keys.length) dimKey = keys[0];
    const raw = dimKey ? String(dims[dimKey]) : "total";
    const label = dimKey ? (point.dimension_labels || {})[dimKey] || raw : "全部";
    if (!buckets.has(raw)) buckets.set(raw, { key: raw, label, byPeriod: new Map() });
    const numeric = point.value === null || point.value === undefined ? null : Number(point.value);
    buckets.get(raw).byPeriod.set(point.period, Number.isFinite(numeric) ? numeric : null);
  }
  const series = [];
  for (const bucket of buckets.values()) {
    series.push({
      key: bucket.key,
      label: bucket.label,
      values: periods.map((period) => (bucket.byPeriod.has(period) ? bucket.byPeriod.get(period) : null)),
    });
  }
  series.sort((a, b) => sumValues(b.values) - sumValues(a.values));
  return { periods, series };
}

function sumValues(values) {
  return values.reduce((acc, value) => acc + (value === null ? 0 : value), 0);
}

function renderResult(result) {
  const pivoted = pivot(result);
  state.result = result;
  state.periods = pivoted.periods;
  state.series = pivoted.series;
  state.unit = result.unit || "CNY";
  state.hidden = new Set();
  if (!$("start").value && result.start) $("start").value = String(result.start).slice(0, 7);
  if (!$("end").value && result.end) $("end").value = String(result.end).slice(0, 7);

  const metric = currentMetric();
  const title = `${result.label || (metric && metric.label) || state.metric}${state.dimension ? " · 按" + dimLabel(state.dimension) : ""}`;
  $("chart-title").textContent = title;
  const missing = (result.missing_periods || []).length;
  $("chart-subtitle").textContent =
    `${result.start || "-"} ~ ${result.end || "-"} · ${GRAIN_LABEL[state.grain] || state.grain}粒` +
    ` · 来源 ${result.source_table || "-"}` +
    (missing ? ` · ${missing} 个缺口期` : "");

  renderKpis();
  renderLegend();
  drawChart();
  renderTable();
}

function periodTotals() {
  return state.periods.map((_, index) => {
    let total = 0;
    let hasValue = false;
    for (const series of state.series) {
      const value = series.values[index];
      if (value !== null) {
        total += value;
        hasValue = true;
      }
    }
    return hasValue ? total : null;
  });
}

function renderKpis() {
  const host = $("kpis");
  host.textContent = "";
  const totals = periodTotals();
  const valid = [];
  totals.forEach((value, index) => {
    if (value !== null) valid.push({ value, period: state.periods[index] });
  });
  if (!valid.length) return;

  const isRatio = state.unit === "ratio";
  const latest = valid[valid.length - 1];
  const previous = valid.length > 1 ? valid[valid.length - 2] : null;
  const sum = valid.reduce((acc, item) => acc + item.value, 0);
  const average = sum / valid.length;
  const peak = valid.reduce((best, item) => (item.value > best.value ? item : best), valid[0]);
  const low = valid.reduce((best, item) => (item.value < best.value ? item : best), valid[0]);

  const cards = [];
  cards.push({
    label: isRatio ? "区间均值" : "区间合计",
    value: fmtValue(isRatio ? average : sum, state.unit),
    hint: isRatio ? `${valid.length} 期平均` : `${valid.length} 期累计`,
  });
  cards.push({ label: "最新一期", value: fmtValue(latest.value, state.unit), hint: latest.period });
  if (previous) {
    const diff = isRatio ? latest.value - previous.value : (latest.value - previous.value) / (previous.value || 1);
    const text = isRatio ? (diff * 100).toFixed(2) + " pt" : (diff * 100).toFixed(1) + "%";
    cards.push({
      label: "环比",
      value: text,
      hint: `对比 ${previous.period}`,
      tone: diff >= 0 ? "up" : "down",
    });
  }
  cards.push({ label: "峰值", value: fmtValue(peak.value, state.unit), hint: peak.period });
  cards.push({ label: "低点", value: fmtValue(low.value, state.unit), hint: low.period });
  if (state.series.length > 1) {
    cards.push({ label: "序列数", value: String(state.series.length), hint: dimLabel(state.dimension) });
  }

  for (const card of cards) {
    const box = el("div", "kpi");
    box.appendChild(el("div", "kpi-label", card.label));
    box.appendChild(el("div", "kpi-value" + (card.tone ? " " + card.tone : ""), card.value));
    if (card.hint) box.appendChild(el("div", "kpi-hint", card.hint));
    host.appendChild(box);
  }
}

function renderLegend() {
  const host = $("legend");
  host.textContent = "";
  if (state.series.length <= 1) return;
  state.series.forEach((series, index) => {
    const chip = el("button", "legend-item" + (state.hidden.has(series.key) ? " off" : ""));
    chip.type = "button";
    const dot = el("span", "legend-dot");
    dot.style.background = PALETTE[index % PALETTE.length];
    chip.appendChild(dot);
    chip.appendChild(el("span", null, series.label));
    chip.addEventListener("click", () => {
      if (state.hidden.has(series.key)) state.hidden.delete(series.key);
      else state.hidden.add(series.key);
      renderLegend();
      drawChart();
    });
    host.appendChild(chip);
  });
}

function drawChart() {
  const host = $("chart");
  host.textContent = "";
  $("chart-tip").classList.add("hidden");
  if (!state.periods.length) {
    host.appendChild(el("div", "chart-empty", "该区间没有数据"));
    return;
  }
  if (state.periods.length === 1) {
    drawSinglePeriod(host);
    return;
  }
  drawTrend(host);
}

function drawSinglePeriod(host) {
  if (state.series.length === 1) {
    const series = state.series[0];
    const wrap = el("div", "single-value");
    wrap.appendChild(el("strong", null, fmtValue(series.values[0], state.unit)));
    wrap.appendChild(el("span", null, `${state.periods[0]} · ${series.label}`));
    host.appendChild(wrap);
    return;
  }
  const values = state.series.map((series) => (series.values[0] === null ? 0 : series.values[0]));
  const max = Math.max.apply(null, values.map((value) => Math.abs(value)).concat([1]));
  const width = Math.max(host.clientWidth || 720, 420);
  const rowHeight = 34;
  const height = state.series.length * rowHeight + 24;
  const labelWidth = 96;
  const valueWidth = 132;
  const chart = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, height, role: "img" });
  state.series.forEach((series, index) => {
    const value = series.values[0];
    const y = 12 + index * rowHeight;
    const barWidth = Math.max(2, ((Math.abs(value || 0) / max) * (width - labelWidth - valueWidth - 16)));
    const label = svgEl("text", {
      x: labelWidth - 10,
      y: y + 16,
      "text-anchor": "end",
      "font-size": 12,
      fill: "#1b2430",
    });
    label.textContent = series.label;
    chart.appendChild(label);
    const rect = svgEl("rect", {
      x: labelWidth,
      y,
      width: barWidth,
      height: 22,
      rx: 4,
      fill: PALETTE[index % PALETTE.length],
      opacity: 0.85,
    });
    chart.appendChild(rect);
    const valueText = svgEl("text", {
      x: labelWidth + barWidth + 8,
      y: y + 16,
      "font-size": 12,
      fill: "#647083",
    });
    valueText.textContent = fmtValue(value, state.unit);
    chart.appendChild(valueText);
  });
  host.appendChild(chart);
}

function drawTrend(host) {
  const visible = state.series.filter((series) => !state.hidden.has(series.key));
  const values = [];
  for (const series of visible) {
    for (const value of series.values) {
      if (value !== null) values.push(value);
    }
  }
  if (!values.length) {
    host.appendChild(el("div", "chart-empty", "所选序列已全部隐藏"));
    return;
  }

  const width = Math.max(host.clientWidth || 720, 420);
  const height = 320;
  const margin = { left: 70, right: 20, top: 16, bottom: 36 };
  const innerWidth = width - margin.left - margin.right;
  const innerHeight = height - margin.top - margin.bottom;

  let low = Math.min.apply(null, values);
  let high = Math.max.apply(null, values);
  if (low === high) {
    low = Math.min(0, low);
    high = high === 0 ? 1 : high * 1.1;
  }
  const padding = (high - low) * 0.12;
  high += padding;
  low = low >= 0 ? Math.max(0, low - padding) : low - padding;
  const ticks = niceTicks(low, high, 4);
  low = ticks[0];
  high = ticks[ticks.length - 1];

  const xAt = (index) =>
    margin.left + (state.periods.length <= 1 ? innerWidth / 2 : (index / (state.periods.length - 1)) * innerWidth);
  const yAt = (value) => margin.top + innerHeight - ((value - low) / (high - low || 1)) * innerHeight;

  const chart = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, height, role: "img" });

  for (const tick of ticks) {
    const y = yAt(tick);
    chart.appendChild(
      svgEl("line", {
        x1: margin.left,
        y1: y,
        x2: width - margin.right,
        y2: y,
        stroke: "#e3e7ee",
        "stroke-width": 1,
      })
    );
    const label = svgEl("text", {
      x: margin.left - 10,
      y: y + 4,
      "text-anchor": "end",
      "font-size": 11,
      fill: "#647083",
    });
    label.textContent = fmtValue(tick, state.unit);
    chart.appendChild(label);
  }

  chart.appendChild(
    svgEl("line", {
      x1: margin.left,
      y1: margin.top + innerHeight,
      x2: width - margin.right,
      y2: margin.top + innerHeight,
      stroke: "#c9d0da",
      "stroke-width": 1,
    })
  );

  const step = Math.ceil(state.periods.length / 8);
  state.periods.forEach((period, index) => {
    if (index % step !== 0 && index !== state.periods.length - 1) return;
    const label = svgEl("text", {
      x: xAt(index),
      y: margin.top + innerHeight + 20,
      "text-anchor": "middle",
      "font-size": 11,
      fill: "#647083",
    });
    label.textContent = period;
    chart.appendChild(label);
  });

  visible.forEach((series) => {
    const color = PALETTE[state.series.indexOf(series) % PALETTE.length];
    let path = "";
    let started = false;
    const dots = [];
    series.values.forEach((value, index) => {
      if (value === null) {
        started = false;
        return;
      }
      const command = started ? "L" : "M";
      path += `${command}${xAt(index).toFixed(1)},${yAt(value).toFixed(1)} `;
      started = true;
      if (state.periods.length <= 16) dots.push({ x: xAt(index), y: yAt(value), value, index });
    });
    if (path) {
      chart.appendChild(
        svgEl("path", {
          d: path.trim(),
          fill: "none",
          stroke: color,
          "stroke-width": 2,
          "stroke-linejoin": "round",
          "stroke-linecap": "round",
        })
      );
    }
    for (const dot of dots) {
      chart.appendChild(
        svgEl("circle", { cx: dot.x, cy: dot.y, r: 3, fill: "#fff", stroke: color, "stroke-width": 2 })
      );
    }
  });

  const guide = svgEl("line", {
    x1: 0,
    y1: margin.top,
    x2: 0,
    y2: margin.top + innerHeight,
    stroke: "#9aa6b8",
    "stroke-width": 1,
    "stroke-dasharray": "3 3",
    opacity: 0,
  });
  chart.appendChild(guide);

  const overlay = svgEl("rect", {
    x: margin.left,
    y: margin.top,
    width: innerWidth,
    height: innerHeight,
    fill: "transparent",
  });
  chart.appendChild(overlay);

  overlay.addEventListener("mousemove", (event) => {
    const rect = chart.getBoundingClientRect();
    const scale = width / rect.width;
    const localX = (event.clientX - rect.left) * scale;
    const ratio = innerWidth ? (localX - margin.left) / innerWidth : 0;
    const index = Math.max(0, Math.min(state.periods.length - 1, Math.round(ratio * (state.periods.length - 1))));
    const x = xAt(index);
    guide.setAttribute("x1", x);
    guide.setAttribute("x2", x);
    guide.setAttribute("opacity", 0.7);
    showTip(event, index);
  });
  overlay.addEventListener("mouseleave", () => {
    guide.setAttribute("opacity", 0);
    $("chart-tip").classList.add("hidden");
  });

  host.appendChild(chart);
}

function showTip(event, index) {
  const tip = $("chart-tip");
  tip.textContent = "";
  tip.appendChild(el("div", "tip-period", state.periods[index]));
  state.series.forEach((series, seriesIndex) => {
    if (state.hidden.has(series.key)) return;
    const value = series.values[index];
    const row = el("div", "tip-row");
    const left = el("span");
    const dot = el("span", "tip-dot");
    dot.style.background = PALETTE[seriesIndex % PALETTE.length];
    left.appendChild(dot);
    left.appendChild(el("span", null, series.label));
    row.appendChild(left);
    row.appendChild(el("span", null, fmtValue(value, state.unit)));
    tip.appendChild(row);
  });
  tip.classList.remove("hidden");

  const card = tip.parentElement.getBoundingClientRect();
  const width = tip.offsetWidth || 160;
  let left = event.clientX - card.left + 14;
  if (left + width > card.width - 8) left = event.clientX - card.left - width - 14;
  let top = event.clientY - card.top + 12;
  if (top + tip.offsetHeight > card.height - 8) top = Math.max(8, card.height - tip.offsetHeight - 8);
  tip.style.left = `${Math.max(8, left)}px`;
  tip.style.top = `${top}px`;
}

function renderTable() {
  const table = $("table");
  const thead = table.querySelector("thead");
  const tbody = table.querySelector("tbody");
  thead.textContent = "";
  tbody.textContent = "";

  const headerRow = document.createElement("tr");
  headerRow.appendChild(el("th", null, "期"));
  for (const series of state.series) headerRow.appendChild(el("th", null, series.label));
  thead.appendChild(headerRow);

  state.periods.forEach((period, index) => {
    const row = document.createElement("tr");
    row.appendChild(el("td", null, period));
    for (const series of state.series) {
      const value = series.values[index];
      const cell = el("td", value === null ? "missing" : null, value === null ? "—" : fmtFull(value, state.unit));
      row.appendChild(cell);
    }
    tbody.appendChild(row);
  });

  const missing = (state.result && state.result.missing_periods) || [];
  $("table-meta").textContent = `${state.periods.length} 期 × ${state.series.length} 列` + (missing.length ? ` · 缺口：${missing.slice(0, 6).join("、")}` : "");
}

function exportCsv() {
  if (!state.periods.length) return;
  const rows = [["期"].concat(state.series.map((series) => series.label))];
  state.periods.forEach((period, index) => {
    const row = [period];
    for (const series of state.series) {
      const value = series.values[index];
      row.push(value === null ? "" : String(value));
    }
    rows.push(row);
  });
  const csv = rows
    .map((row) => row.map((cell) => (/[",\n]/.test(cell) ? `"${cell.replace(/"/g, '""')}"` : cell)).join(","))
    .join("\r\n");
  const blob = new Blob(["\ufeff" + csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${state.metric}${state.dimension ? "-" + state.dimension : ""}-${state.grain}.csv`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

async function loadQuality() {
  const hint = $("quality-hint");
  try {
    const [metrics, alerts] = await Promise.all([api("/api/v1/metrics"), api("/api/v1/alerts")]);
    hint.classList.add("hidden");
    renderQuality(metrics, alerts);
    state.qualityLoaded = true;
  } catch (err) {
    hint.classList.remove("hidden");
    hint.textContent =
      err.status === 403
        ? `当前身份（${(state.principal && state.principal.role) || "未连接"}）没有 admin scope，运行质量接口需要管理员 Key。`
        : err.message;
  }
}

function renderQuality(metrics, alerts) {
  const host = $("quality-kpis");
  host.textContent = "";
  const runs = metrics.runs || {};
  const model = metrics.model || {};
  const cost = metrics.cost || {};

  const cards = [
    { label: "运行总数", value: String(runs.total || 0), hint: `终态 ${runs.terminal || 0}` },
    { label: "成功率", value: ((runs.success_rate || 0) * 100).toFixed(1) + "%", hint: `${runs.completed || 0} / ${runs.total || 0}` },
    { label: "平均耗时", value: (runs.avg_execution_seconds || 0).toFixed(1) + " s", hint: "单次运行" },
    { label: "P95 耗时", value: (runs.p95_execution_seconds || 0).toFixed(1) + " s", hint: "长尾" },
    { label: "模型平均延迟", value: (model.avg_latency_ms || 0) / 1000 >= 1 ? ((model.avg_latency_ms || 0) / 1000).toFixed(1) + " s" : (model.avg_latency_ms || 0).toFixed(0) + " ms", hint: `${model.calls || 0} 次调用` },
    { label: "待审批", value: String((alerts.evaluated && alerts.evaluated.pending_approvals) || 0), hint: "pending" },
  ];
  for (const card of cards) {
    const box = el("div", "kpi");
    box.appendChild(el("div", "kpi-label", card.label));
    box.appendChild(el("div", "kpi-value", card.value));
    box.appendChild(el("div", "kpi-hint", card.hint));
    host.appendChild(box);
  }

  const bars = $("status-bars");
  bars.textContent = "";
  const byStatus = runs.by_status || {};
  const total = Object.values(byStatus).reduce((acc, value) => acc + value, 0);
  if (!total) {
    bars.appendChild(el("div", "alert-none", "还没有运行记录"));
  } else {
    const bar = el("div", "status-bar");
    const legend = el("div", "status-legend");
    for (const [status, count] of Object.entries(byStatus)) {
      const share = count / total;
      const segment = el("div", "status-seg", share >= 0.12 ? `${count}` : "");
      segment.style.width = `${(share * 100).toFixed(1)}%`;
      segment.style.background = STATUS_COLORS[status] || "#647083";
      bar.appendChild(segment);
      const item = el("span");
      const dot = el("span", "legend-dot");
      dot.style.background = STATUS_COLORS[status] || "#647083";
      item.appendChild(dot);
      item.appendChild(el("span", null, `${status} · ${count}`));
      legend.appendChild(item);
    }
    bars.appendChild(bar);
    bars.appendChild(legend);
  }

  const list = $("alert-list");
  list.textContent = "";
  const items = alerts.alerts || [];
  if (!items.length) {
    const evaluated = alerts.evaluated || {};
    const box = el("div", "alert-none");
    box.appendChild(el("div", null, "无活跃告警"));
    box.appendChild(
      el(
        "div",
        "small",
        `已评估：失败率 ${((evaluated.failure_rate || 0) * 100).toFixed(0)}% · P95 ${(evaluated.p95_execution_seconds || 0).toFixed(0)}s · 待审批 ${evaluated.pending_approvals || 0} · 门槛最少 ${evaluated.min_runs_for_rate_rules || 0} 次运行`
      )
    );
    if (cost.estimated === false) box.appendChild(el("div", "small", "成本：未配置模型单价，估算为 null"));
    list.appendChild(box);
  } else {
    for (const item of items) {
      const box = el("div", "alert-item");
      box.appendChild(el("div", "alert-title", item.title || item.code || "告警"));
      if (item.detail) box.appendChild(el("div", "alert-meta", typeof item.detail === "string" ? item.detail : JSON.stringify(item.detail)));
      list.appendChild(box);
    }
  }
}

function bindEvents() {
  $("connect").addEventListener("click", () => connect());
  $("api-key").addEventListener("keydown", (event) => {
    if (event.key === "Enter") connect();
  });
  $("filters").addEventListener("submit", (event) => {
    event.preventDefault();
    query(false);
  });
  $("metric").addEventListener("change", () => {
    state.metric = $("metric").value;
    fillDimensionOptions();
    query(false);
  });
  $("dimension").addEventListener("change", () => {
    state.dimension = $("dimension").value;
    query(false);
  });
  $("grain").addEventListener("change", () => {
    state.grain = $("grain").value;
    query(false);
  });
  $("reset").addEventListener("click", () => {
    $("start").value = "";
    $("end").value = "";
    query(true);
  });
  $("export").addEventListener("click", () => exportCsv());
  $("refresh-quality").addEventListener("click", () => loadQuality());

  for (const tab of document.querySelectorAll(".viz-tabs .tab")) {
    tab.addEventListener("click", () => {
      for (const other of document.querySelectorAll(".viz-tabs .tab")) other.classList.remove("active");
      tab.classList.add("active");
      const target = tab.dataset.panel;
      $("panel-business").classList.toggle("hidden", target !== "business");
      $("panel-quality").classList.toggle("hidden", target !== "quality");
      if (target === "quality") loadQuality();
      if (target === "business") drawChart();
    });
  }

  let timer = null;
  window.addEventListener("resize", () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      if (!$("panel-business").classList.contains("hidden")) drawChart();
    }, 160);
  });
}

async function init() {
  bindEvents();
  state.key = $("api-key").value = state.key || "";
  try {
    await loadCatalog();
  } catch (err) {
    showError(err.message);
    return;
  }
  await connect();
}

document.addEventListener("DOMContentLoaded", init);

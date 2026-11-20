/* Panoptes Agents tab: the supervisor's job history (T-026), read-only.
   GET /api/jobs every 30 s (non-overlapping) and render: a headline card of
   aggregate stats, one table row per job (newest first, order as delivered),
   and an inline detail panel for the selected row. Every metric keeps its
   provenance visible — calculated is italic, partial is amber with a
   tooltip, unavailable is a muted em dash — and every headline figure
   states the sample size it came from. No console output, no mutation.
   On top of the tab sits a Live workers card (T-031): GET /api/harnesses
   every 5 s (non-overlapping, /api/inference alongside for context windows)
   plus a 1 s local ticker for elapsed and idle-for counters. Still read-only. */

import { lamp, segBar } from "./ui/kit.js";

const page = document.getElementById("page-agents");
if (!page) throw new Error("missing #page-agents");

const $ = (id) => document.getElementById(id);

function el(tag, cls, parent) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (parent) parent.appendChild(n);
  return n;
}

async function getJSON(url) {
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${url} -> HTTP ${res.status}`);
  return res.json();
}

const banner = el("div", "card span12");
page.insertBefore(banner, page.firstChild);

/* ------------------------------------------------------------- formatting */

const OUTCOME_BADGE = { accepted: "ok", rejected: "hot", stopped: "warn", partial: "info" };

/* Metric display order and labels; the payload's `unit` field still decides
   how a value is rendered, so an unknown metric still shows honestly. */
const METRICS = [
  ["worker_seconds", "Worker time"],
  ["gpu_energy_wh", "GPU energy"],
  ["cpu_energy_wh", "CPU energy"],
  ["total_energy_wh", "Total energy"],
  ["energy_cost_gbp", "Energy cost"],
  ["output_tokens", "Output tokens"],
  ["reasoning_tokens", "Reasoning tokens"],
  ["effective_worker_tps", "Effective worker speed"],
  ["peak_context", "Peak context"],
  ["compactions", "Compactions"],
  /* OMP writes no retry record: these two are the error signals it keeps.
     Never label either one a retry. */
  ["failed_tool_calls", "Failed tool calls"],
  ["aborted_requests", "Aborted requests"],
  ["python_tests", "Python tests"],
  ["prefix_hit", "Prompt prefix hit"],
  ["draft_accept", "Draft acceptance"],
];

const fmtInt = (v) => v.toLocaleString("en-GB");

function fmtDuration(sec) {
  if (typeof sec !== "number" || !isFinite(sec)) return "—";
  const m = Math.round(sec / 60);
  if (m < 60) return m < 1 ? "<1m" : `${m}m`;
  const h = Math.floor(m / 60);
  const r = m % 60;
  return r ? `${h}h ${r}m` : `${h}h`;
}

function fmtMetric(m) {
  if (m.value === null || m.value === undefined) {
    if (m.raw !== undefined && m.raw !== null) return String(m.raw);
    return "—";
  }
  const v = m.value;
  switch (m.unit) {
    case "GBP": return `\u00a3${v.toFixed(4)}`;
    case "ratio": return v.toFixed(3);
    case "tok/s": return `${v.toFixed(1)} tok/s`;
    case "Wh": return `${v.toFixed(1)} Wh`;
    case "tokens": return `${fmtInt(v)} tokens`;
    case "s": return `${fmtDuration(v)} (${fmtInt(v)} s)`;
    case "tests": case "count": return String(v);
    default: return `${v} ${m.unit || ""}`.trim();
  }
}

function provClass(m) {
  if (!m || m.provenance === "calculated") return "ag-calc";
  if (m.provenance === "partial") return "ag-partial";
  if (m.provenance === "unavailable") return "ag-na";
  return "";
}

/* ------------------------------------------------------------ URL filters */

/* Filter state lives in the hash as #agents?m=M3&o=accepted&f=1. app.js owns
   the hash and publishes every applied route as a `panoptes:route` event whose
   detail.query is exactly the query string; because this module runs after the
   first route has been applied, the load-time filters come from
   window.panoptesRoute(). Nothing here touches the tab or the tab name.
   A filter change rewrites only the query, with history.replaceState, so a
   reload keeps the same view without pushing history entries. */
function readFilters(detail) {
  const q = new URLSearchParams(detail && detail.tab === "agents" ? detail.query : "");
  return { milestone: q.get("m") || "", outcome: q.get("o") || "", first: q.get("f") === "1" };
}

let filters = readFilters(window.panoptesRoute && window.panoptesRoute());

function writeHash() {
  const q = new URLSearchParams();
  if (filters.milestone) q.set("m", filters.milestone);
  if (filters.outcome) q.set("o", filters.outcome);
  if (filters.first) q.set("f", "1");
  const s = q.toString();
  const want = s ? `#agents?${s}` : "#agents";
  history.replaceState(null, "", want);
  return want;
}

// A route change is the only way this tab's URL can move underneath it, so it
// is the only place filters are re-read.
window.addEventListener("panoptes:route", (ev) => {
  if (!ev.detail || ev.detail.tab !== "agents") return;
  filters = readFilters(ev.detail);
  render();
});

/* ------------------------------------------------------------- rendering */

let data = null;
let selectedId = null;

const fMs = $("ag-f-milestone");
const fOc = $("ag-f-outcome");
const fFirst = $("ag-f-first");

function stat(host, label, value, sub, cap) {
  const box = el("div", "ag-stat", host);
  el("div", "v", box).textContent = value;
  if (sub) el("div", "ag-sub", box).textContent = sub;
  el("div", "lbl", box).textContent = label;
  el("div", "cap", box).textContent = cap;
}

const avg = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);

function renderHeadline() {
  const host = $("ag-stats");
  host.textContent = "";
  const sm = (data && data.summary) || {};
  const met = sm.metrics || {};
  const countOf = (n) => (met[n] && met[n].count) || 0;

  stat(host, "Accepted tasks per worker-hour",
    sm.accepted_tasks_per_worker_hour == null ? "—" : sm.accepted_tasks_per_worker_hour.toFixed(2),
    "", `from ${countOf("worker_seconds")} measured jobs`);

  const fta = sm.first_time_accept || {};
  stat(host, "First-time acceptance",
    fta.rate == null ? "—" : `${Math.round(fta.rate * 100)}%`,
    fta.of ? `${fta.count} of ${fta.of} jobs` : "",
    `from ${fta.of || 0} measured jobs`);

  /* "per accepted task" is a client-side mean over accepted jobs whose
     energy (or cost) is provenance=measured with a numeric value; partial
     figures never feed the headline, and the caption names that sample. */
  const acc = ((data && data.jobs) || []).filter((j) => j.outcome === "accepted");
  const num = (j, name) => {
    const m = (j.metrics || {})[name] || {};
    return m.provenance === "measured" && typeof m.value === "number" ? m.value : null;
  };
  const eVals = acc.map((j) => num(j, "total_energy_wh")).filter((v) => v !== null);
  const cVals = acc.map((j) => num(j, "energy_cost_gbp")).filter((v) => v !== null);
  const eAvg = avg(eVals);
  const cAvg = avg(cVals);
  stat(host, "Energy per accepted task",
    eAvg === null ? "—" : `${eAvg.toFixed(1)} Wh`,
    cAvg === null ? "" : `\u00a3${cAvg.toFixed(4)} avg cost`,
    `from ${eVals.length} measured jobs`);

  const tps = met.effective_worker_tps;
  stat(host, "Effective worker speed",
    tps && tps.average != null ? `${tps.average.toFixed(1)} tok/s` : "—",
    "", `from ${countOf("effective_worker_tps")} measured jobs`);
}

function fillSelect(sel, values, selected, anyLabel) {
  const key = values.join("\u0000");
  if (sel.dataset.key !== key) {
    sel.dataset.key = key;
    sel.textContent = "";
    const any = el("option", "", sel);
    any.value = "";
    any.textContent = anyLabel;
    for (const v of values) {
      const o = el("option", "", sel);
      o.value = String(v);
      o.textContent = String(v);
    }
  }
  sel.value = values.some((v) => String(v) === selected) ? selected : "";
  return sel.value;
}

function visibleJobs(jobs) {
  return jobs.filter((j) =>
    (!filters.milestone || String(j.milestone) === filters.milestone) &&
    (!filters.outcome || (j.outcome || "unknown") === filters.outcome) &&
    (!filters.first || j.first_time_accept === true));
}

function metricCell(tr, metrics, name, fmt) {
  const td = el("td", "ag-num", tr);
  const m = metrics[name];
  if (!m || m.value === null || m.value === undefined) {
    if (m && m.raw !== undefined && m.raw !== null) {
      td.textContent = String(m.raw);
      if (m.provenance === "partial") { td.classList.add("ag-partial"); td.title = "partial"; }
      else td.classList.add("ag-na");
    } else {
      td.textContent = "—";
      td.classList.add("ag-na");
    }
    return td;
  }
  td.textContent = fmt(m.value);
  if (m.provenance === "calculated") td.classList.add("ag-calc");
  else if (m.provenance === "partial") { td.classList.add("ag-partial"); td.title = "partial"; }
  return td;
}

function buildRow(rowsHost, j) {
  const tr = el("tr", "", rowsHost);
  if (j.id === selectedId) tr.classList.add("sel");
  tr.tabIndex = 0;
  el("td", "ag-id", tr).textContent = j.id;
  el("td", "ag-title", tr).textContent = j.title || "untitled job";
  const ms = el("td", "", tr);
  if (j.milestone) ms.textContent = j.milestone;
  else { ms.textContent = "—"; ms.classList.add("ag-na"); }
  const badge = el("span", "badge", el("td", "", tr));
  const oc = OUTCOME_BADGE[j.outcome];
  if (oc) badge.classList.add(oc);
  badge.textContent = j.outcome || "unknown";
  const rd = el("td", "ag-num", tr);
  if (j.rounds === null || j.rounds === undefined) rd.textContent = "—";
  else {
    rd.textContent = String(j.rounds);
    if (j.rounds === 1) el("span", "badge ok ag-fresh", rd).textContent = "first time";
  }
  const met = j.metrics || {};
  metricCell(tr, met, "worker_seconds", fmtDuration);
  metricCell(tr, met, "total_energy_wh", (v) => `${v.toFixed(1)} Wh`);
  metricCell(tr, met, "effective_worker_tps", (v) => v.toFixed(1));
  metricCell(tr, met, "peak_context", fmtInt);
  tr.addEventListener("click", () => { selectedId = j.id; render(); });
  tr.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); selectedId = j.id; render(); }
  });
}

function metricEntry(host, name, label, m) {
  el("dt", "", host).textContent = label;
  const dd = el("dd", "", host);
  const vs = el("span", provClass(m), dd);
  vs.textContent = fmtMetric(m);
  if (m.provenance === "partial") vs.title = "partial";
  const parts = [m.provenance || "unknown", m.scope || "job"];
  if (m.raw !== undefined && m.raw !== null && m.value !== null && m.value !== undefined)
    parts.push(`raw ${JSON.stringify(m.raw)}`);
  el("span", "note ag-prov", dd).textContent = ` \u00b7 ${parts.join(" \u00b7 ")}`;
}

/* The job report downloads: plain links at the endpoints, which already send
   Content-Disposition — no fetch or Blob. Read-only, triggers nothing. One mode
   select drives both the set export and the selected job's own report, so the
   two can never disagree about what was redacted. */
const exportRow = el("div", "ag-export", null);
const exportLink = el("a", "ag-export-btn", exportRow);
exportLink.textContent = "Export .md";
const exportName = el("span", "note ag-export-name", exportRow);
$("ag-detail").insertBefore(exportRow, $("ag-detail-job"));
const exportModeSel = $("ag-export-mode");
const exportSetLink = $("ag-export-set");
let exportMode = exportModeSel.value;

const MODE_NOTE = {
  shareable: "Shareable: paths, machine names and secrets redacted",
  private: "Private: paths kept, secrets still redacted",
};
const reportSuffix = () => (exportMode === "private" ? "report-private" : "report");

/* One report over exactly the jobs the current filters show. */
function renderExport(jobs) {
  exportSetLink.hidden = jobs.length === 0;
  if (!jobs.length) return;
  const ids = jobs.map((j) => encodeURIComponent(j.id)).join(",");
  exportSetLink.href = `/api/reports/jobs/${exportMode}/${ids}.md`;
  exportSetLink.setAttribute("download", `panoptes-jobs-${exportMode}.md`);
  exportSetLink.textContent = `Export ${jobs.length} job${jobs.length === 1 ? "" : "s"} (.md)`;
  exportSetLink.title = MODE_NOTE[exportMode];
}

function renderDetail(jobsById) {
  const det = $("ag-detail");
  const job = selectedId === null ? null : jobsById.get(selectedId);
  if (!job) {
    det.hidden = true;
    selectedId = null;
    return;
  }
  det.hidden = false;
  $("ag-detail-title").textContent =
    `${job.id} \u00b7 ${job.title || "untitled job"} \u00b7 ${job.outcome || "unknown"}` +
    (job.rounds === null || job.rounds === undefined ? "" : ` \u00b7 ${job.rounds} round${job.rounds === 1 ? "" : "s"}`);
  const suffix = reportSuffix();
  const fname = `panoptes-${job.id}-${suffix}.md`;
  exportLink.href = `/api/jobs/${encodeURIComponent(job.id)}/${suffix}.md`;
  exportLink.setAttribute("download", fname);
  exportName.textContent = `${fname} \u00b7 ${MODE_NOTE[exportMode]}`;
  const met = job.metrics || {};
  const jobList = $("ag-detail-job");
  const engList = $("ag-detail-engine");
  jobList.textContent = "";
  engList.textContent = "";
  let engShown = 0;
  for (const [name, label] of METRICS) {
    const m = met[name];
    if (!m) continue;
    if (m.scope === "engine-lifetime") { metricEntry(engList, name, label, m); engShown += 1; continue; }
    metricEntry(jobList, name, label, m);
  }
  for (const [name, m] of Object.entries(met)) {
    if (METRICS.some(([n]) => n === name) || !m || typeof m !== "object") continue;
    if (m.scope === "engine-lifetime") { metricEntry(engList, name, name, m); engShown += 1; continue; }
    metricEntry(jobList, name, name, m);
  }
  $("ag-detail-engine-h").hidden = engShown === 0;
  const rv = $("ag-detail-review");
  if (job.review_excerpt) { rv.hidden = false; rv.textContent = job.review_excerpt; }
  else { rv.hidden = true; rv.textContent = ""; }
}

function render() {
  if (!data) return;
  const jobs = data.jobs || [];
  const warnings = data.warnings || [];
  const warn = $("ag-warnings");
  warn.hidden = warnings.length === 0;
  warn.textContent = "";
  if (warnings.length) {
    const ul = el("ul", "warn-list", warn);
    for (const w of warnings) el("li", "", ul).textContent = w;
  }
  const sm = data.summary || {};
  $("ag-count").textContent =
    jobs.length ? `${sm.jobs ?? jobs.length} jobs \u00b7 ${sm.accepted ?? 0} accepted` : "no job records";
  renderHeadline();
  const keptMs = fillSelect(fMs, [...new Set(jobs.map((j) => j.milestone).filter((v) => v))].sort(
    (a, b) => String(a).localeCompare(String(b), "en", { numeric: true })), filters.milestone, "any milestone");
  const keptOc = fillSelect(fOc, [...new Set(jobs.map((j) => j.outcome || "unknown"))].sort(),
    filters.outcome, "any outcome");
  filters.milestone = keptMs;
  filters.outcome = keptOc;
  fFirst.checked = filters.first;
  const vis = visibleJobs(jobs);
  $("ag-filter-note").textContent =
    vis.length === jobs.length ? "" : `showing ${vis.length} of ${jobs.length} jobs`;
  const empty = $("ag-empty");
  empty.hidden = jobs.length > 0 && vis.length > 0;
  empty.textContent = jobs.length === 0
    ? "No job records found on this machine"
    : "No jobs match the filters";
  const rows = $("ag-rows");
  rows.textContent = "";
  for (const j of vis) buildRow(rows, j);
  renderExport(vis);
  renderDetail(new Map(jobs.map((j) => [j.id, j])));
}

for (const [ctrl, key] of [[fMs, "milestone"], [fOc, "outcome"], [fFirst, "first"]]) {
  ctrl.addEventListener("change", () => {
    filters[key] = key === "first" ? ctrl.checked : ctrl.value;
    writeHash();
    render();
  });
}

exportModeSel.addEventListener("change", () => {
  exportMode = exportModeSel.value;
  render();
});

/* ---------------------------------------------------------------- polling */

let inFlight = false;

async function tick() {
  if (inFlight) return;
  inFlight = true;
  try {
    data = await getJSON("/api/jobs");
    banner.hidden = true;
  } catch {
    banner.textContent = "Dashboard API unreachable at /api/jobs";
    banner.hidden = false;
    inFlight = false;
    return;
  }
  inFlight = false;
  render();
}

tick();
setInterval(tick, 30000);

/* --------------------------------------------------------- live workers */

/* T-031: the "Live workers" card, pinned to the top of the tab (above the
   API banner). One row per configured harness: lamp + label + model always.
   Working rows add the active task id — linked to #agents?task=<id> only
   when that job already exists in /api/jobs, otherwise plain text — plus
   elapsed time and "last activity Ns ago" ticking locally between the 5 s
   polls, turns, a context bar against the model window (max_model_len of
   the matching engine from /api/inference, else 65,536) with the peak in
   the text, compactions amber when > 0, output and reasoning tokens and a
   compact tools line. Idle rows stay muted: last session turns and file
   time. Capability flags hide whole sections a harness never reports; a
   missing value is an em dash, never a guess. Read-only, no console use. */

const DEFAULT_CTX = 65536;      /* context window when no engine reports one */
const STALL_S = 120;            /* working but silent this long -> amber */
const LIVE_POLL_MS = 5000;
const LAMP_BY_STATUS = { working: "busy", idle: "off", unknown: "warn" };
const STATUS_TEXT = { working: "Working", idle: "Idle", unknown: "Unknown" };

const liveCard = el("section", "card span12");
page.insertBefore(liveCard, banner);
const liveH2 = el("h2", "", liveCard);
liveH2.textContent = "Live workers";
const liveCount = el("span", "right", liveH2);
const liveNote = el("p", "note", liveCard);
liveNote.hidden = true;
const liveRowsHost = el("div", "ag-live", liveCard);
const liveWarn = el("div", "ag-live-warns", liveCard);
liveWarn.hidden = true;

let liveData = null;      /* last /api/harnesses payload or null */
let liveError = false;    /* last /api/harnesses poll failed */
let enginesData = [];     /* last /api/inference engines array */
let liveAt = 0;           /* seconds-epoch of the last successful poll */
let liveInFlight = false;
let liveTicks = [];       /* closures the 1 s ticker runs for live text */

/* "1h 02m 03s": hours unbounded, minutes and seconds zero padded. */
function fmtHMS(sec) {
  if (typeof sec !== "number" || !isFinite(sec) || sec < 0) return "\u2014";
  const t = Math.floor(sec);
  const p2 = (n) => String(n).padStart(2, "0");
  return `${Math.floor(t / 3600)}h ${p2(Math.floor(t / 60) % 60)}m ${p2(t % 60)}s`;
}

function fmtAgo(sec) {
  if (typeof sec !== "number" || !isFinite(sec)) return "\u2014";
  return `${Math.max(0, Math.floor(sec))}s`;
}

/* ISO timestamp -> "YYYY-MM-DD HH:MM:SS UTC"; the idle row's file time. */
function fmtStamp(iso) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "\u2014";
  return `${new Date(t).toISOString().replace("T", " ").slice(0, 19)} UTC`;
}

function isoEpoch(iso) {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t / 1000;
}

/* Context window for a harness session: the engine whose model id matches
   the session model, either exactly ("flashnext") or as the provider-suffix
   of it ("radiance/flashnext"), else the 65,536-token fallback. */
function contextWindow(sessionModel, engines) {
  if (typeof sessionModel !== "string" || !sessionModel || !Array.isArray(engines)) {
    return DEFAULT_CTX;
  }
  const short = sessionModel.split("/").pop();
  for (const eng of engines) {
    const model = eng && eng.model;
    const id = model && model.id;
    if (typeof id !== "string" || !id) continue;
    if (id === sessionModel || id === short || sessionModel.endsWith(`/${id}`)) {
      return typeof model.max_model_len === "number" && model.max_model_len > 0
        ? model.max_model_len : DEFAULT_CTX;
    }
  }
  return DEFAULT_CTX;
}

/* {"bash":25,"edit":26} -> "bash 25 \u00b7 edit 26", most-used first. */
function toolLine(toolCalls) {
  if (!toolCalls || typeof toolCalls !== "object") return "";
  return Object.entries(toolCalls)
    .filter(([, n]) => typeof n === "number")
    .sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0]), "en"))
    .map(([name, n]) => `${name} ${n}`)
    .join(" \u00b7 ");
}

function tickClock() {
  for (const fn of liveTicks) fn();
}

function buildLiveRow(h) {
  const row = el("div", "ag-live-row", liveRowsHost);
  const head = el("div", "ag-live-head", row);
  lamp(el("span", "", head))(
    LAMP_BY_STATUS[h.status] || "warn", STATUS_TEXT[h.status] || String(h.status));
  el("span", "ag-live-label", head).textContent = h.label || h.id || "harness";
  const sess = h.session;
  if (sess && sess.model) el("span", "ag-live-model", head).textContent = sess.model;
  const caps = h.capabilities || {};

  if (h.status !== "working") {
    const bits = [];
    if (sess && sess.turns != null) bits.push(`last session ${sess.turns} turns`);
    if (sess && sess.last_activity_at) bits.push(`file time ${fmtStamp(sess.last_activity_at)}`);
    el("span", "ag-live-idle", head).textContent = bits.length ? `\u00b7 ${bits.join(" \u00b7 ")}` : "";
    return;
  }

  const task = h.active_task;
  if (task && task.id) {
    if (((data && data.jobs) || []).some((j) => j.id === task.id)) {
      const a = el("a", "ag-live-task", head);
      a.href = `#agents?task=${encodeURIComponent(task.id)}`;
      a.textContent = task.id;
    } else {
      el("span", "ag-live-task", head).textContent = task.id;
    }
  }

  const mid = el("div", "ag-live-mid", row);
  if (task) {
    let anchor = null;
    if (typeof task.elapsed_s === "number" && isFinite(task.elapsed_s)) anchor = liveAt - task.elapsed_s;
    else if (task.started_at) anchor = isoEpoch(task.started_at);
    const span = el("span", "", mid);
    if (anchor === null) span.textContent = "elapsed \u2014";
    else liveTicks.push(() => {
      span.textContent = `elapsed ${fmtHMS(Date.now() / 1000 - anchor)}`;
    });
  }
  const turns = sess && sess.turns != null ? sess.turns : null;
  el("span", "", mid).textContent = `turns ${turns == null ? "\u2014" : turns}`;
  if (caps.tokens) {
    const tok = (v) => (typeof v === "number" ? fmtInt(v) : "\u2014");
    el("span", "", mid).textContent =
      `out ${tok(sess && sess.output_tokens)} \u00b7 reasoning ${tok(sess && sess.reasoning_tokens)} tok`;
  }
  if (caps.compactions) {
    const c = sess ? sess.compactions : null;
    const cs = el("span", "", mid);
    cs.textContent = `compactions ${c == null ? "\u2014" : c}`;
    if (typeof c === "number" && c > 0) cs.classList.add("ag-warn");
  }
  if (caps.errors) {
    /* One line, one span per count: an amber number is the only alarm. */
    const errs = el("span", "", mid);
    const count = (label, value) => {
      el("span", "", errs).textContent = `${label} `;
      const n = el("span", "", errs);
      n.textContent = typeof value === "number" ? String(value) : "\u2014";
      if (typeof value === "number" && value > 0) n.classList.add("ag-warn");
    };
    count("failed tool calls", sess ? sess.failed_tool_calls : null);
    el("span", "", errs).textContent = " \u00b7 ";
    count("aborted requests", sess ? sess.aborted_requests : null);
  }

  const win = contextWindow(sess && sess.model, enginesData);
  const cur = sess && typeof sess.current_context === "number" ? sess.current_context : null;
  const peak = sess && sess.peak_context != null ? fmtInt(sess.peak_context) : "\u2014";
  const setBar = segBar(el("div", "ag-live-bar", row), { label: "Context", total: win });
  setBar(cur != null && cur > 0 ? [{ value: cur, color: "--info", name: "current context" }] : [],
    `${cur == null ? "\u2014" : fmtInt(cur)} / ${fmtInt(win)} \u00b7 peak ${peak}`);

  const act = el("span", "ag-live-act", row);
  const actEpoch = sess && sess.last_activity_at ? isoEpoch(sess.last_activity_at) : null;
  if (actEpoch === null) act.textContent = "last activity \u2014";
  else liveTicks.push(() => {
    const age = Date.now() / 1000 - actEpoch;
    act.textContent = `last activity ${fmtAgo(age)} ago`;
    act.classList.toggle("ag-warn", age > STALL_S);
  });

  if (caps.tool_calls) {
    const line = toolLine(sess && sess.tool_calls);
    if (line) el("div", "ag-live-tools", row).textContent = `tools: ${line}`;
  }
}

function renderLive() {
  liveRowsHost.textContent = "";
  liveTicks = [];
  const hs = liveData && Array.isArray(liveData.harnesses) ? liveData.harnesses : [];
  if (liveError) {
    liveNote.hidden = false;
    liveNote.textContent = "Dashboard API unreachable at /api/harnesses";
  } else if (!hs.length) {
    liveNote.hidden = false;
    liveNote.textContent = "No agent harnesses configured ([[harnesses]] in config/panoptes.toml)";
  } else {
    liveNote.hidden = true;
    liveNote.textContent = "";
  }
  liveCount.textContent = hs.length ? `${hs.length} harness${hs.length === 1 ? "" : "es"}` : "";
  liveWarn.textContent = "";
  const warns = liveData && Array.isArray(liveData.warnings) ? [...liveData.warnings] : [];
  for (const h of hs) {
    buildLiveRow(h);
    for (const w of h.warnings || []) warns.push(`${h.label || h.id}: ${w}`);
  }
  liveWarn.hidden = warns.length === 0;
  if (warns.length) {
    const ul = el("ul", "warn-list", liveWarn);
    for (const w of warns) el("li", "", ul).textContent = w;
  }
  tickClock();
}

async function tickLive() {
  if (liveInFlight) return;
  liveInFlight = true;
  try {
    liveData = await getJSON("/api/harnesses");
    liveError = false;
    liveAt = Date.now() / 1000;
    /* Best effort: without the engines the bars just use the fallback
       window, so a dead inference endpoint must not blank the card. */
    try {
      enginesData = (await getJSON("/api/inference")).engines || [];
    } catch {
      enginesData = [];
    }
  } catch {
    liveError = true;
  }
  liveInFlight = false;
  renderLive();
}

tickLive();
setInterval(tickLive, LIVE_POLL_MS);
setInterval(tickClock, 1000);

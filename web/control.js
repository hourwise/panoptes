/* CONTROL tab (T-021): Radiance container lifecycle.
 * Reads hit /api/control/* with plain GETs. Nothing changes before the
 * in-page confirm step: the POST only fires after Confirm, always with
 * {"confirm": true} plus the page token. With the server in dry-run mode
 * every result carries dry_run and a badge appears next to the title.
 * The helper itself enforces the worker-task refusal; the UI locks
 * Stop/Restart up front as well: both stay disabled while a worker task is
 * active, and any click the page refuses says why in the result line. */
import { lamp, progressRing } from "./ui/kit.js";

const $ = (id) => document.getElementById(id);
const opBtn = (op) => document.querySelector(`button#ctl-${op}`);
const token = document.querySelector('meta[name="wsd-token"]')?.content || "";

const WORKER_NOTE = "A supervised worker task is running: Stop and Restart are locked";
const CONFIRM_TEXT = {
  start: "Start Radiance? The model loads on start (about 30 s).",
  stop: "Stop Radiance? The helper refuses while requests are in flight.",
  restart: "Restart Radiance? The model will reload (about 30 s); requests in progress are refused by the helper.",
};
const LAMP_BY_STATE = { running: "ok", exited: "off", restarting: "busy", created: "off", paused: "off", dead: "off" };
/* Docker reports "0001-01-01T00:00:00Z" when the container has never
 * started; that is "no start time", never a 2000-year uptime. */
const NEVER = (iso) => !iso || /^0001-01-01T00:00:00/.test(iso);

// Docker inspect ports {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}]}
// -> "127.0.0.1:8000 -> 8000/tcp"; a plain string passes through untouched.
export function formatPorts(ports) {
  if (typeof ports === "string") return ports;
  if (!ports || typeof ports !== "object") return "";
  const parts = [];
  for (const [port, bindings] of Object.entries(ports)) {
    for (const b of bindings || []) {
      parts.push(`${b.HostIp || "0.0.0.0"}:${b.HostPort} -> ${port}`);
    }
  }
  return parts.join(", ");
}

// restart_policy is either the helper's plain name or Docker's
// {"Name": "unless-stopped", "MaximumRetryCount": 0}.
export function formatRestart(policy) {
  if (typeof policy === "string") return policy;
  return (policy && typeof policy === "object" && policy.Name) || "";
}

// Docker's nanosecond StartedAt -> "2026-11-18 08:41:07 UTC"; never started -> "".
function formatStarted(iso) {
  if (NEVER(iso)) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return String(iso);
  return `${new Date(t).toISOString().replace(/\.\d+Z$/, "").replace("T", " ")} UTC`;
}

/**
 * One view over both /api/control/status shapes: the dry-run canned payload
 * ({state: "running", started_at, ports: "", restart_policy: "..."}) and the
 * live helper's Docker inspect data ({state: {Status, StartedAt, ...},
 * ports: {...}, restart_policy: {Name, ...}}).
 */
export function statusView(st) {
  const live = st.state && typeof st.state === "object" ? st.state : null;
  const raw = live ? live.StartedAt : st.started_at;
  const startedIso = NEVER(raw) ? null : raw;
  return {
    state: live
      ? live.Status || (live.Running ? "running" : "")
      : typeof st.state === "string" ? st.state : "",
    started: startedIso ? formatStarted(startedIso) : "",
    ports: formatPorts(st.ports),
    restart: formatRestart(st.restart_policy),
    startedIso,
  };
}
const ENGINE_ID = "radiance-r9700";
const INFER_POLL_MS = 2000;
const INFER_TIMEOUT_MS = 180000;

const setLamp = lamp($("ctl-lamp"));
const setRing = progressRing($("ctl-ring"));

let workerActive = false; // payload from the last status poll
let pendingOp = null; // op awaiting confirmation, null when no confirm is open
let opInFlight = false; // a POST is running
let inferTimer = null; // follow-up poll after start/restart

const fmt = (value, fallback = "\u2014") => {
  if (value === undefined || value === null || value === "") return fallback;
  return typeof value === "object" ? JSON.stringify(value) : String(value);
};

export function uptime(startedAt) {
  const t = Date.parse(startedAt);
  if (Number.isNaN(t)) return null;
  let secs = Math.max(0, Math.floor((Date.now() - t) / 1000));
  const d = Math.floor(secs / 86400); secs -= d * 86400;
  const h = Math.floor(secs / 3600); secs -= h * 3600;
  const m = Math.floor(secs / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m ${secs - m * 60}s`;
}

/* ---------------------------------------------------------------- status */

function renderStatus(payload) {
  const st = (payload && payload.status) || {};
  $("ctl-dryrun-badge").hidden = !st.dry_run;
  $("ctl-name").textContent = fmt(st.name, "");
  const dd = (id, value) => { $(id).textContent = fmt(value); };
  const view = statusView(st);
  dd("ctl-image", st.image);
  dd("ctl-started", view.started);
  dd("ctl-ports", view.ports);
  dd("ctl-network", st.network_mode);
  dd("ctl-restart-policy", view.restart);
  if (st.ok === false) {
    setLamp("error", fmt(st.error, "helper error"));
    dd("ctl-uptime", null);
    return;
  }
  setLamp(LAMP_BY_STATE[view.state] || "error", view.state || "unknown");
  dd("ctl-uptime", view.state === "running" ? uptime(view.startedIso) : null);
}

function renderWorker(active) {
  workerActive = !!active;
  $("ctl-worker-note").hidden = !workerActive;
  updateButtons();
}

async function pollStatus() {
  if (opInFlight) return; // don't fight a running op for the cached helper
  try {
    const resp = await fetch("/api/control/status");
    const payload = await resp.json();
    renderStatus(payload);
    renderWorker(payload.worker_active);
  } catch {
    setLamp("error", "status unavailable");
  }
}

/* ------------------------------------------------------------------- ops */

function updateButtons() {
  for (const op of ["start", "stop", "restart"]) {
    const btn = opBtn(op);
    const lockedByWorker = op !== "start" && workerActive;
    btn.disabled = lockedByWorker || opInFlight || pendingOp !== null;
    btn.title = lockedByWorker ? WORKER_NOTE : "";
  }
}

function showWarn(op, reason) {
  const el = $("ctl-result");
  el.hidden = false;
  el.className = "op-result warn";
  el.textContent = `refused: ${op} \u2014 ${reason}`;
}

function askOp(op) {
  if (opInFlight) { showWarn(op, "another operation is already running"); return; }
  if (workerActive && op !== "start") { showWarn(op, WORKER_NOTE); return; }
  pendingOp = op;
  $("ctl-result").hidden = true;
  $("ctl-confirm-text").textContent = CONFIRM_TEXT[op];
  $("ctl-confirm").hidden = false;
  updateButtons();
}

function cancelOp() {
  pendingOp = null;
  $("ctl-confirm").hidden = true;
  updateButtons();
}

function showResult(op, result) {
  const el = $("ctl-result");
  el.hidden = false;
  const dry = result && result.dry_run ? " (dry run)" : "";
  if (result && result.ok) {
    el.textContent = `ok: ${op} succeeded${dry}`;
    el.className = "op-result ok";
    if (op !== "stop") followModel();
  } else {
    const reason = (result && result.error) || "helper returned no reason";
    el.textContent = `refused: ${op} \u2014 ${reason}`;
    el.className = "op-result err";
  }
}

async function confirmOp() {
  const op = pendingOp;
  if (!op) { showWarn("confirm", "no operation is waiting for confirmation"); return; }
  if (opInFlight) { showWarn(op, "another operation is already running"); return; }
  if (workerActive && op !== "start") { cancelOp(); showWarn(op, WORKER_NOTE); return; }
  pendingOp = null;
  $("ctl-confirm").hidden = true;
  opInFlight = true;
  updateButtons();
  const el = $("ctl-result");
  el.hidden = false;
  el.className = "op-result";
  el.textContent = `${op} running ...`;
  try {
    const resp = await fetch(`/api/control/${op}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-WSD-Token": token },
      body: JSON.stringify({ confirm: true }),
    });
    showResult(op, await resp.json());
  } catch {
    el.textContent = `refused: ${op} \u2014 network error`;
    el.className = "op-result err";
  } finally {
    opInFlight = false;
    updateButtons();
    pollStatus();
  }
}

/* ------------------------------------------------- model-load follow-up */

function followModel() {
  clearTimeout(inferTimer);
  const row = $("ctl-progress");
  const text = $("ctl-progress-text");
  row.hidden = false;
  text.className = "";
  text.textContent = "model loading ...";
  setRing(null);
  const deadline = Date.now() + INFER_TIMEOUT_MS;
  const tick = async () => {
    if (Date.now() > deadline) {
      text.className = "stale";
      text.textContent = "still loading after 3 min \u2014 check the logs";
      return;
    }
    try {
      const resp = await fetch("/api/inference");
      const engines = (await resp.json()).engines || [];
      const engine = engines.find((e) => e && e.id === ENGINE_ID);
      if (engine && engine.status === "ready") {
        setRing(1);
        text.textContent = "model ready";
        return;
      }
      text.textContent = engine && engine.status === "down"
        ? "container is down \u2014 model is not loading"
        : "model loading ...";
    } catch {
      text.textContent = "model loading ...";
    }
    inferTimer = setTimeout(tick, INFER_POLL_MS);
  };
  inferTimer = setTimeout(tick, INFER_POLL_MS);
}

/* ------------------------------------------------------------------ logs */

async function refreshLogs() {
  const box = $("ctl-logs");
  const stuckToBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 4;
  try {
    const resp = await fetch("/api/control/logs");
    const payload = await resp.json();
    box.textContent = Array.isArray(payload.lines)
      ? payload.lines.slice(-200).join("\n")
      : fmt(payload.error, "logs unavailable");
  } catch {
    /* keep the previous lines on a failed refresh */
  }
  if (stuckToBottom) box.scrollTop = box.scrollHeight;
}

/* ------------------------------------------------------------------ wire */

for (const op of ["start", "stop", "restart"]) {
  opBtn(op).addEventListener("click", () => askOp(op));
}
$("ctl-confirm-yes").addEventListener("click", confirmOp);
$("ctl-confirm-no").addEventListener("click", cancelOp);
$("ctl-logs-refresh").addEventListener("click", refreshLogs);

setInterval(pollStatus, 5000); // the server caches the helper for the same 5 s
pollStatus();
refreshLogs();

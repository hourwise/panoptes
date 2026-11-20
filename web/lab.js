/* Panoptes Lab tab: one benchmark variant against another (T-036), read-only.
   GET /api/lab/variants every 30 s (non-overlapping) to fill the two pickers;
   a change of pair GETs /api/lab/compare/<baseline>/<modified> and renders the
   API's measurements in the API's order — rows are never re-sorted. Colour
   comes only from the API's `better` field, a within-noise difference stays
   neutral and says so, and a side that never measured reads "no data", never
   a 0. The picked pair lives in the URL (#lab?baseline=..&modified=..) so a
   reload or a pasted link shows the same comparison. No writes, no console. */

import {
  NOISE_NOTE, fmtSide, fmtDiff, diffClass, parseLabQuery, buildLabHash,
} from "./lab-format.js";

const page = document.getElementById("page-lab");
if (!page) throw new Error("missing #page-lab");

const $ = (id) => document.getElementById(id);

function el(tag, cls, parent) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (parent) parent.appendChild(n);
  return n;
}

/* GET a lab endpoint; a RouteError body becomes the note text, so a bad
   variant picked from a stale URL explains itself instead of hitting the log. */
async function getJSON(url) {
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      if (body && typeof body.error === "string") detail = `: ${body.error}`;
    } catch { /* a non-JSON error page still explains itself by status */ }
    throw new Error(`${url} -> HTTP ${res.status}${detail}`);
  }
  return res.json();
}

const selBase = $("lab-baseline");
const selMod = $("lab-modified");
const countEl = $("lab-count");
const emptyEl = $("lab-empty");
const sameEl = $("lab-same");
const warnsEl = $("lab-warnings");
const compareEl = $("lab-compare");
const metaEl = $("lab-meta");
const rowsEl = $("lab-rows");
const exportEl = $("lab-export");
const exportModeSel = $("lab-export-mode");

let variantsData = null;   /* last /api/lab/variants payload or null */
let variantsError = "";    /* last /api/lab/variants failure text */
let compareData = null;    /* last /api/lab/compare payload or null */
let compareError = "";     /* last /api/lab/compare failure text */
let want = { baseline: null, modified: null };   /* names asked for by the URL */
let picked = { baseline: null, modified: null }; /* names the pickers actually show */
let exportMode = exportModeSel.value;            /* shareable or private */

/* ----------------------------------------------------------- URL selection */

/* app.js owns the hash and publishes every applied route as a
   `panoptes:route` event; the load-time selection comes from
   window.panoptesRoute(), and a picker change rewrites only the query with
   history.replaceState, so a reload keeps the comparison without pushing
   history entries. Nothing here touches the tab itself. */
function readPicked(detail) {
  return parseLabQuery(detail && detail.tab === "lab" ? detail.query : "");
}

function writeHash() {
  history.replaceState(null, "", buildLabHash(picked.baseline, picked.modified));
}

/* ------------------------------------------------------------ variants list */

function variantNames() {
  return (variantsData && variantsData.variants || []).map((v) => v.variant);
}

/* A name from the URL counts only while the records still have it; anything
   else (or nothing) falls back to the first two variants. */
function resolvePicked(want) {
  const names = variantNames();
  if (names.length >= 2 && names.includes(want.baseline) && names.includes(want.modified)) {
    return { baseline: want.baseline, modified: want.modified };
  }
  return { baseline: names[0] || null, modified: names[1] || null };
}

function fillPickers() {
  const variants = (variantsData && variantsData.variants) || [];
  for (const [sel, key] of [[selBase, "baseline"], [selMod, "modified"]]) {
    const keyText = variants.map((v) => v.variant).join(",");
    if (sel.dataset.key !== keyText) {
      sel.dataset.key = keyText;
      sel.textContent = "";
      for (const v of variants) {
        const o = el("option", "", sel);
        o.value = v.variant;
        o.textContent = `${v.variant} (${v.runs} run${v.runs === 1 ? "" : "s"})`;
      }
    }
    sel.value = picked[key] || "";
  }
}

/* ---------------------------------------------------------------- rendering */

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function renderWarnings() {
  warnsEl.textContent = "";
  const warnings = (variantsData && variantsData.warnings) || [];
  for (const text of warnings) el("div", "warn-note", warnsEl).textContent = text;
  warnsEl.hidden = warnings.length === 0;
}

function renderEmptyNote() {
  /* One honest reason, in this order: no API, no runs, one variant only. */
  if (variantsError) {
    emptyEl.textContent = `Dashboard API unreachable at /api/lab/variants: ${variantsError}`;
  } else if (!variantsData || variantsData.variants.length === 0) {
    emptyEl.textContent = "No benchmark runs recorded yet. They appear here once "
      + "the benchmark suite has scored at least two variants.";
  } else if (variantsData.variants.length < 2) {
    emptyEl.textContent = `Only one variant recorded (${variantsData.variants[0].variant}): `
      + "a second variant is needed to compare.";
  } else {
    emptyEl.textContent = "";
  }
  emptyEl.hidden = emptyEl.textContent === "";
}

function taskListEl(parent, prefix, tasks) {
  el("div", "", parent).textContent = `${prefix}: ${tasks.join(", ")}`;
}

function renderCompare() {
  sameEl.hidden = picked.baseline === null || picked.baseline !== picked.modified;
  if (compareError) {
    rowsEl.textContent = "";
    metaEl.textContent = `Comparison unavailable (${compareError}).`;
    compareEl.hidden = false;
    return;
  }
  if (!compareData) {
    compareEl.hidden = true;
    return;
  }
  $("lab-th-base").textContent = `Baseline · ${compareData.baseline}`;
  $("lab-th-mod").textContent = `Modified · ${compareData.modified}`;
  metaEl.textContent = "";
  el("div", "", metaEl).textContent =
    `${plural(compareData.runs.baseline, "run")} baseline against `
    + `${plural(compareData.runs.modified, "run")} modified, over `
    + `${plural(compareData.tasks_compared.length, "task")}`
    + (compareData.tasks_compared.length ? `: ${compareData.tasks_compared.join(", ")}` : "");
  if (compareData.tasks_only_in_baseline.length) {
    taskListEl(metaEl, "Not compared, ran only in baseline", compareData.tasks_only_in_baseline);
  }
  if (compareData.tasks_only_in_modified.length) {
    taskListEl(metaEl, "Not compared, ran only in modified", compareData.tasks_only_in_modified);
  }
  rowsEl.textContent = "";
  for (const m of compareData.measurements) {
    const tr = el("tr", "", rowsEl);
    el("td", "ag-title", tr).textContent =
      m.label + (m.unit && m.unit !== "count" && !m.label.includes(m.unit) ? ` (${m.unit})` : "");
    el("td", "ag-num", tr).textContent = fmtSide(m.baseline, m.unit);
    el("td", "ag-num", tr).textContent = fmtSide(m.modified, m.unit);
    const td = el("td", `ag-num ${diffClass(m.better, m.within_noise)}`.trim(), tr);
    td.textContent = fmtDiff(m.difference, m.unit);
    if (m.within_noise && m.difference && m.difference.absolute !== null
      && m.difference.absolute !== undefined) {
      el("div", "lab-noise", td).textContent = NOISE_NOTE;
    }
  }
  compareEl.hidden = false;
}

/* The comparison download: a plain link at the report endpoint (no fetch, no
   Blob), built from the picked pair and the mode chosen here. It stays
   disabled until two different variants are picked, exactly like the table: a
   variant compared with itself says nothing, and a report of it would too. */
function renderExport() {
  const ready = picked.baseline !== null && picked.modified !== null
    && picked.baseline !== picked.modified;
  exportEl.classList.toggle("is-disabled", !ready);
  exportEl.setAttribute("aria-disabled", ready ? "false" : "true");
  if (!ready) {
    exportEl.removeAttribute("href");
    exportEl.removeAttribute("download");
    return;
  }
  const pair = `${encodeURIComponent(picked.baseline)}/${encodeURIComponent(picked.modified)}`;
  exportEl.href = `/api/reports/lab/${exportMode}/${pair}.md`;
  exportEl.setAttribute("download", `panoptes-lab-${exportMode}.md`);
  exportEl.title = exportMode === "private"
    ? "Private: paths and machine names kept, secrets still redacted"
    : "Shareable: paths, machine names and secrets redacted";
}

exportModeSel.addEventListener("change", () => {
  exportMode = exportModeSel.value;
  render();
});

function render() {
  if (!variantsData && !variantsError) return;
  countEl.textContent = variantsData
    ? `${plural(variantsData.variants.length, "variant")}, ${plural(variantsData.runs, "run")}`
    : "";
  renderWarnings();
  fillPickers();
  renderEmptyNote();
  const comparable = variantsData && variantsData.variants.length >= 2
    && picked.baseline !== picked.modified;
  if (!comparable) {
    compareData = null;
    compareError = "";
  }
  renderCompare();
  renderExport();
}

/* ---------------------------------------------------------------- fetching */

let inFlight = false;

async function fetchCompare() {
  compareError = "";
  if (!picked.baseline || picked.baseline === picked.modified) {
    compareData = null;
    return;
  }
  const url = `/api/lab/compare/${encodeURIComponent(picked.baseline)}`
    + `/${encodeURIComponent(picked.modified)}`;
  try {
    compareData = await getJSON(url);
  } catch (err) {
    compareData = null;
    compareError = String(err && err.message || err);
  }
}

async function tick() {
  if (inFlight) return;
  inFlight = true;
  try {
    try {
      variantsData = await getJSON("/api/lab/variants");
      variantsError = "";
    } catch (err) {
      variantsError = String(err && err.message || err);
    }
    if (variantsData && variantsData.variants.length) {
      const next = resolvePicked(want);
      if (next.baseline !== picked.baseline || next.modified !== picked.modified) {
        picked = next;
        writeHash();
      }
      if (variantsData.variants.length >= 2 && picked.baseline !== picked.modified) {
        await fetchCompare();
      } else {
        compareData = null;
        compareError = "";
      }
    } else {
      compareData = null;
      compareError = "";
    }
  } finally {
    inFlight = false;
  }
  render();
}

/* A picker change rewrites the URL and refetches; picking the same variant on
   both sides fetches nothing and explains itself. */
async function onPick() {
  want = picked = { baseline: selBase.value || null, modified: selMod.value || null };
  writeHash();
  if (picked.baseline && picked.baseline !== picked.modified) await fetchCompare();
  else { compareData = null; compareError = ""; }
  render();
}

selBase.addEventListener("change", onPick);
selMod.addEventListener("change", onPick);

/* A route change is the only way this tab's URL can move underneath it. */
window.addEventListener("panoptes:route", async (ev) => {
  if (!ev.detail || ev.detail.tab !== "lab") return;
  want = readPicked(ev.detail);
  const next = resolvePicked(want);
  if (next.baseline === picked.baseline && next.modified === picked.modified) return;
  picked = next;
  if (picked.baseline && picked.baseline !== picked.modified) await fetchCompare();
  else { compareData = null; compareError = ""; }
  render();
});

want = readPicked(window.panoptesRoute && window.panoptesRoute());
tick();
setInterval(tick, 30000);

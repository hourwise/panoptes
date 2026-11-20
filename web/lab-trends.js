/* Panoptes Lab trends: how the accepted tasks went, with the config changes
   marked on them (T-045), read-only.
   GET /api/lab/trends once when the Lab tab opens (again if that attempt
   failed), then render one small multiple per measure as inline SVG — no
   canvas, no chart library. One slot per task in acceptance order, so a gap is
   a gap and a value that was never measured is simply not drawn: nothing here
   invents a number or drops a point to zero. Hollow = partial, calculated or
   unavailable provenance; amber = not accepted first time; dashed violet = an
   applied profile switch or restore, drawn at the first task accepted after
   it. The grey line is the rolling median over the last five task slots.
   value. Only the worker-minutes chart converts anything (the API's own
   seconds divided by 60, still marked partial when it is). No writes, no
   polling, no logging in the page. */

import { fmtNum, fraction } from "./ui/geom.js";
import {
  markerLabels, rollingMedian, markerSlot, scaleY, summary, pointValue, pointComplete,
  layoutMarkerLabels, groupWarnings,
} from "./lab-trends-format.js";

const page = document.getElementById("page-lab");
if (!page) throw new Error("missing #page-lab");

const $ = (id) => document.getElementById(id);
const SVGNS = "http://www.w3.org/2000/svg";

function el(tag, cls, parent) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (parent) parent.appendChild(node);
  return node;
}

function svgEl(tag, attrs, parent) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, String(value));
  if (parent) parent.appendChild(node);
  return node;
}

/* GET a trends endpoint; a RouteError body becomes the note text. */
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

/* The six small multiples, in reading order. `plain` measures are plain job
   figures with no metric cell; only worker minutes changes the API's unit. */
const MEASURES = [
  { key: "worker_seconds", label: "Worker minutes per task", unit: "min", factor: 1 / 60, dp: 1 },
  { key: "total_energy_wh", label: "Wh per task", unit: "Wh", dp: 1 },
  { key: "effective_worker_tps", label: "Effective tok/s", unit: "tok/s", dp: 1, zeroFloor: false },
  { key: "rounds", label: "Rounds per task", unit: "rounds", dp: 0, plain: true },
  { key: "peak_context", label: "Peak context", unit: "tokens", dp: 0 },
  { key: "compactions", label: "Compactions", unit: "count", dp: 0 },
];

const MEDIAN_WINDOW = 5;
const RECENT = 10;
const BOX = { w: 320, h: 138, l: 34, r: 10, b: 22 };
const MARK_ROWS = 3;      /* label rows above the plot before they merge */
const MARK_TOP = 4;       /* where the first label row sits */
const MARK_ROW_H = 12;    /* one label row: the 8.5px glyph box is 11 tall */
const PLOT_TOP = 12;      /* the gap between the lowest label and the points */
const MARK_CHAR_W = 4.2;  /* one glyph of the 8.5px label font, measured wide */

/* The x of one task slot: one slot per accepted task, spread across the plot. */
const slotX = (slot, count) => {
  const span = BOX.w - BOX.l - BOX.r;
  return count === 1 ? BOX.l + span / 2 : BOX.l + (slot / (count - 1)) * span;
};

const chartsEl = $("lab-trends-charts");
const summaryEl = $("lab-trends-summary");
const emptyEl = $("lab-trends-empty");
const warnsEl = $("lab-trends-warnings");
const titleEl = $("lab-trends-count");

let data = null;        /* last /api/lab/trends payload, or null */
let failure = "";       /* last failed attempt's text */
let inFlight = false;

/* --------------------------------------------------------------- rendering */

/** Tooltip text for one point: what it is, what it measured, how well. */
function pointTitle(point, measure) {
  const cell = measure.plain
    ? { value: point[measure.key], unit: measure.unit, provenance: "job figure" }
    : point[measure.key] || {};
  const shown = pointValue(point, measure);
  const value = shown === null
    ? `no ${measure.unit} recorded`
    : `${fmtNum(shown, measure.dp)} ${measure.unit}`;
  const answer = point.first_time_accept === null || point.first_time_accept === undefined
    ? "answer unknown" : point.first_time_accept ? "accepted first time" : "accepted after a round back";
  return `${point.id} ${point.title || ""} — ${value}`
    + ` (${cell.provenance || "unavailable"}${cell.scope ? `, ${cell.scope}` : ""})`
    + ` — ${answer}, accepted ${point.accepted_at}`;
}

function chart(host, measure, points, slots, labels, rows) {
  const cell = el("div", "lt-cell", host);
  el("h3", "lt-name", cell).textContent = measure.label;
  /* the switch labels get their own band above the plot, so the points can
     never sit behind them however many rows the crowded ones needed */
  const top = MARK_TOP + rows * MARK_ROW_H + PLOT_TOP;
  const h = BOX.h + rows * MARK_ROW_H;
  const svg = svgEl("svg", {
    viewBox: `0 0 ${BOX.w} ${h}`, class: "lt-chart", role: "group",
    "aria-label": `${measure.label}, one point per accepted task in acceptance order`,
  }, cell);
  const values = points.map((point) => pointValue(point, measure));
  const scale = scaleY(values, { zeroFloor: measure.zeroFloor !== false });
  const spanY = h - top - BOX.b;
  const xs = (slot) => slotX(slot, points.length);
  const ys = (value) => h - BOX.b - fraction(value, scale.min, scale.max) * spanY;

  svgEl("line", { class: "lt-axis", x1: BOX.l, y1: h - BOX.b, x2: BOX.w - BOX.r, y2: h - BOX.b }, svg);
  for (const value of [scale.min, scale.max]) {
    svgEl("text", { class: "lt-tick", x: BOX.l - 5, y: ys(value) + 3, "text-anchor": "end" }, svg)
      .textContent = fmtNum(value, measure.dp);
  }

  /* Switch markers first so the data sits on top of them. `slots` maps a task
     slot to every applied switch that lands on it; `labels` is the result of
     laying their text out over `rows` rows, clear of each other and of the
     right edge, and never over the points or the task ids below. */
  for (const [slot, group] of slots) {
    const x = xs(slot);
    const line = svgEl("line", { class: "lt-marker", x1: x, y1: MARK_TOP, x2: x, y2: h - BOX.b }, svg);
    svgEl("title", {}, line).textContent = group
      .map((marker) => `${marker.action || "switch"} ${marker.to} on ${marker.engine}`
        + ` at ${marker.at}, before ${points[slot].id}`)
      .join("\n");
  }
  for (const label of labels) {
    const text = svgEl("text", {
      class: "lt-marker-label", y: MARK_TOP + label.row * MARK_ROW_H + 7,
      x: label.anchor === "end" ? label.right : label.left, "text-anchor": label.anchor,
    }, svg);
    text.textContent = label.text;
    /* the whole list stays readable even once a crowded stretch merged */
    svgEl("title", {}, text).textContent = label.markers
      .map((marker) => `${marker.action || "switch"} ${marker.to} on ${marker.engine} at ${marker.at}`)
      .join("\n");
  }

  const medians = rollingMedian(values, MEDIAN_WINDOW);
  let path = "";
  let open = false;
  medians.forEach((value, slot) => {
    if (value === null) { open = false; return; }
    path += `${open ? "L" : "M"}${xs(slot)} ${ys(value)} `;
    open = true;
  });
  if (path.trim()) svgEl("path", { class: "lt-median", d: path.trim(), fill: "none" }, svg);

  points.forEach((point, slot) => {
    const value = pointValue(point, measure);
    if (value === null) return;               /* never plotted at zero */
    const cls = ["lt-pt"];
    if (!pointComplete(point, measure)) cls.push("lt-hollow");
    if (point.first_time_accept === false) cls.push("lt-back");
    const dot = svgEl("circle", {
      class: cls.join(" "), cx: xs(slot), cy: ys(value), r: 3.4, tabindex: 0,
      "aria-label": pointTitle(point, measure),
    }, svg);
    svgEl("title", {}, dot).textContent = pointTitle(point, measure);
  });

  const every = Math.max(1, Math.ceil(points.length / 8));
  points.forEach((point, slot) => {
    if (slot % every) return;
    svgEl("text", { class: "lt-id", x: xs(slot), y: h - 6, "text-anchor": "middle" }, svg)
      .textContent = point.id.replace(/^T-0*/, "T-");
  });

  const gaps = points.length - values.filter((value) => value !== null).length;
  if (gaps) {
    el("p", "lt-skip", cell).textContent
      = `${gaps} of ${points.length} tasks have no ${measure.unit} recorded; they are not on this chart.`;
  }
}

/** The line above the charts, and the markers that fell past the last task. */
function summarise(host, points, undrawn) {
  const s = summary(points, RECENT);
  const bits = [`${s.tasks} accepted tasks`];
  bits.push(s.first_time.of
    ? `${s.first_time.count} of ${s.first_time.of} accepted first time`
    + ` (${Math.round(s.first_time.rate * 100)}%)`
    : "first-time acceptance unknown");
  for (const [label, recent, all] of [
    ["min", s.worker_minutes_recent, s.worker_minutes],
    ["Wh", s.energy_wh_recent, s.energy_wh],
  ]) {
    /* a median that does not exist is said so, never printed as a blank or a 0 */
    const show = (value) => (value === null ? "nothing measured" : fmtNum(value, 1));
    if (recent.median === null && all.median === null) {
      bits.push(`no measured ${label} to take a median of`);
      continue;
    }
    bits.push(`median ${label} ${show(recent.median)} over the last ${s.recent}`
      + ` vs ${show(all.median)} over all`
      + (recent.skipped ? ` (${recent.skipped} of the last ${s.recent} partial or missing)` : ""));
  }
  el("span", "", host).textContent = bits.join(" · ");
  if (undrawn.length) {
    el("span", "lt-after", host).textContent = ` · ${undrawn.length} applied switch`
      + `${undrawn.length > 1 ? "es" : ""} after the last accepted task, not drawn: `
      + undrawn.map((marker) => `${marker.to} at ${marker.at}`).join(", ");
  }
}

function render() {
  chartsEl.textContent = "";
  warnsEl.textContent = "";
  summaryEl.textContent = "";
  if (failure) {
    emptyEl.textContent = failure;
    emptyEl.hidden = false;
    chartsEl.hidden = true;
    return;
  }
  if (!data) {
    emptyEl.textContent = "loading the task trends…";
    emptyEl.hidden = false;
    chartsEl.hidden = true;
    return;
  }
  const { points = [], markers = [], warnings = [] } = data;
  /* one line per kind of warning: ten tasks with the same hole are a sentence
     with the full list behind a <details>, not ten boxes pushing the charts
     a screen and a half down */
  for (const group of groupWarnings(warnings)) {
    if (!group.ids.length) {
      el("div", "warn-note", warnsEl).textContent = group.text;
      continue;
    }
    const box = el("details", "warn-note", warnsEl);
    el("summary", "", box).textContent = group.text;
    const list = el("ul", "lt-warn-list", box);
    for (const detail of group.detail) el("li", "", list).textContent = detail;
  }
  warnsEl.hidden = !warnings || !warnings.length;
  if (!points.length) {
    emptyEl.textContent = "Nothing to plot yet: no accepted task with an accepted time is on record.";
    emptyEl.hidden = false;
    chartsEl.hidden = true;
    titleEl.textContent = "";
    return;
  }
  emptyEl.hidden = true;
  chartsEl.hidden = false;
  titleEl.textContent = `${points.length} accepted tasks, ${markers.length} applied switches marked`;
  /* one slot per applied switch: the first task accepted at or after it */
  const slots = new Map();
  const undrawn = [];
  for (const marker of markers || []) {
    const slot = markerSlot(points, marker.at);
    if (slot === null) { undrawn.push(marker); continue; }
    if (!slots.has(slot)) slots.set(slot, []);
    slots.get(slot).push(marker);
  }
  /* the label geometry is the same in every chart: it depends only on the
     slot each switch lands on, so it is worked out once for all six */
  const raw = [];
  for (const [slot, group] of slots) {
    for (const name of markerLabels(group, MARK_ROWS)) {
      raw.push({ x: slotX(slot, points.length), text: name, markers: group });
    }
  }
  const labels = layoutMarkerLabels(raw, BOX.w - BOX.r, MARK_CHAR_W, MARK_ROWS);
  const rows = labels.reduce((high, label) => Math.max(high, label.row + 1), 0);
  for (const measure of MEASURES) chart(chartsEl, measure, points, slots, labels, rows);
  summarise(summaryEl, points, undrawn);
}

/* ----------------------------------------------------------------- fetching */

async function load() {
  if (inFlight || (data && !failure)) return;
  inFlight = true;
  try {
    data = await getJSON("/api/lab/trends");
    failure = "";
  } catch (err) {
    failure = `The task trends could not be read (${err.message}).`;
  } finally {
    inFlight = false;
  }
  render();
}

/* The Lab tab is the only reader of this card: fetch when it is the route on
   load, and again when it becomes the route after a failed attempt. Trends
   change when a task is accepted or a profile switches, so no polling. */
function onRoute(tab) {
  if (tab === "lab") load();
}

window.addEventListener("panoptes:route", (ev) => {
  onRoute(ev.detail && ev.detail.tab);
});

onRoute(window.panoptesRoute && window.panoptesRoute().tab);

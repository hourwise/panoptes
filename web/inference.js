// Panoptes Inference tab. Polls /api/inference every 2 s (never overlapping)
// and always — so sparkline history keeps accumulating. The API lists every
// configured engine (T-012); one is selected at a time (#inference/<id>), and
// every card not backed by a capability that engine has says so instead of
// showing empty dials. Widgets come from ui/kit.js; nulls render as "—" /
// "not exposed", never as invented numbers.

import { dial, sparkline, segBar, lamp, progressRing } from "./ui/kit.js";
import { zone, fmtNum } from "./ui/geom.js";

const $ = (id) => document.getElementById(id);
const num = (v) => typeof v === "number" && Number.isFinite(v);
const put = (host, s) => {
  host.textContent = s == null ? "—" : s;
};

/* Duration in seconds → "4.7 s" / "12.4 min" / "2 h 43 m". */
function fmtDur(s) {
  if (!num(s)) return null;
  if (s < 90) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
  if (s < 5400) return `${(s / 60).toFixed(1)} min`;
  return `${Math.floor(s / 3600)} h ${String(Math.round((s % 3600) / 60)).padStart(2, "0")} m`;
}

const pctOf = (v) => (num(v) ? v * 100 : null);
const orDash = (v) => (v == null ? "—" : v);

/* ---------------------------------------------------------------- engine */

const LAMP_FOR = { ready: "ok", loading: "busy", down: "off", error: "error" };
const engineLamp = lamp($("inf-lamp"));
const ring = progressRing($("inf-ring"));

function renderEngine(snap) {
  const model = (snap && snap.model) || {};
  const name = snap && snap.type ? snap.type[0].toUpperCase() + snap.type.slice(1) : "—";
  if (!snap) {
    engineLamp("off", "engine data unavailable");
    $("inf-model-state").textContent = "";
  } else {
    engineLamp(LAMP_FOR[snap.status] || "off", [name, orDash(snap.version), orDash(model.id)].join(" "));
    $("inf-model-state").textContent = orDash(model.state);
  }
  const loading = !!snap && snap.status === "loading";
  $("inf-loading").hidden = !loading;
  if (loading) ring(null); // no fraction exposed by the engine

  put($("inf-uptime"), fmtDur(snap && snap.uptime_s));
  const vram = snap && model.vram_bytes;
  put($("inf-vram"), num(vram) ? `${fmtNum(vram / (1024 ** 3), 1)} GB` : null);
  put($("inf-load"), num(model.load_seconds) ? `${fmtNum(model.load_seconds, 1)} s` : null);
  const q = (snap && snap.queue) || {};
  put(
    $("inf-queue"),
    snap ? `${orDash(q.running)} running · ${orDash(q.waiting)} waiting · cap ${orDash(q.queue_cap)}` : null,
  );
  put($("inf-maxseqs"), num(q.max_num_seqs) ? fmtNum(q.max_num_seqs, 0) : null);
}

/* ------------------------------------------------------------ dials, etc */

const dialBox = () => {
  const d = document.createElement("div");
  $("inf-dials").appendChild(d);
  return d;
};

/* Generation and prefill are measured from Radiance's token counters between
   polls, so the number is what the engine really committed. A null means that
   phase produced nothing in the last window: keep the last measured sample on
   the dial, greyed out and labelled "· last", so an idle engine reads as "the
   last run did 69 tok/s" instead of a cliff to zero. Before the first measured
   sample the dial shows the kit's not-exposed state. The engine's own decaying
   gauges (throughput.*_gauge) are not rendered: they drift through 0.4, 0.1 …
   after a request finishes and would move the needle on an idle engine. */
function rateDial(label, o) {
  const host = dialBox();
  const show = dial(host, { label, ...o });
  const lbl = host.querySelector(".lbl");
  const arc = host.querySelector("svg path:last-of-type");
  const text = host.querySelector("svg text.v");
  const unit = host.querySelector("svg text:last-of-type");
  return (v, last) => {
    lbl.textContent = label;
    if (num(v)) {
      text.style.fill = "";
      show(v);
      return v;
    }
    if (num(last)) {
      show(last);
      lbl.textContent = `${label} · last`;
      arc.setAttribute("stroke", "var(--muted)");
      text.style.fill = "var(--muted)";
      text.textContent = fmtNum(last, o.dp || 0); // no zone colour, no hot ▲
      unit.textContent = o.unit;
      unit.setAttribute("class", "u");
      return null;
    }
    show(null);
    unit.textContent = "idle"; // nothing is hidden here, there is just no sample yet
    return null;
  };
}

const dials = {
  gen: rateDial("Generation tok/s", { unit: "tok/s", min: 0, max: 120, warn: 40, hot: 20, invert: true }),
  pre: rateDial("Prefill tok/s", { unit: "tok/s", min: 0, max: 2000, warn: 600, hot: 300, invert: true }),
  ttft: dial(dialBox(), { label: "TTFT p50", unit: "s", min: 0, max: 15, warn: 5, hot: 10, dp: 2 }),
  cache: dial(dialBox(), { label: "Prefix cache hit", unit: "%", min: 0, max: 100, warn: 60, hot: 30, invert: true }),
  draft: dial(dialBox(), { label: "Draft acceptance", unit: "%", min: 0, max: 100, warn: 50, hot: 30, invert: true }),
};

/* Chart history belongs to the engine, not the page: each engine keeps its
   own samples, and switching engines rebuilds the three sparklines from the
   selected engine's history so one engine's traffic never draws under
   another engine's card. Only measured (numeric) samples are stored. */
const SPARK_POINTS = 150;
const SPARK_DEFS = {
  gen: { host: () => $("inf-gen-spark"), o: { max: 120, color: "--info", points: SPARK_POINTS } },
  pre: { host: () => $("inf-pre-spark"), o: { max: 2000, color: "--violet", points: SPARK_POINTS } },
  h2d: { host: () => $("inf-h2d-spark"), o: { max: 4000, color: "--info", points: SPARK_POINTS } },
};
const sparkHistory = new Map(); // engine id -> { gen: [], pre: [], h2d: [] }
let sparkId = null;
let sparkWidgets = {};

function historyFor(id) {
  let h = sparkHistory.get(id);
  if (!h) { h = { gen: [], pre: [], h2d: [] }; sparkHistory.set(id, h); }
  return h;
}

function switchSparks(id) {
  sparkId = id;
  const h = historyFor(id);
  sparkWidgets = {};
  for (const [key, def] of Object.entries(SPARK_DEFS)) {
    const host = def.host();
    host.textContent = ""; // kit sparkline appends: drop the previous engine's paths
    const show = sparkline(host, def.o);
    for (const v of h[key]) show(v);
    sparkWidgets[key] = show;
  }
}

function pushSpark(key, v) {
  if (sparkId == null || !num(v)) return;
  const h = historyFor(sparkId);
  h[key].push(v);
  if (h[key].length > SPARK_POINTS) h[key].shift();
  sparkWidgets[key](v);
}

/* "69.6 tok/s" while measured, "69.6 tok/s · last" when kept from an earlier
   window, "idle" once nothing has been measured at all. */
const rateText = (snap, v, last) =>
  !snap ? null : num(v) ? `${fmtNum(v, 1)} tok/s` : num(last) ? `${fmtNum(last, 1)} tok/s · last` : "idle";

function renderPerformance(snap) {
  const th = (snap && snap.throughput) || {};
  const lat = (snap && snap.latency) || {};
  const cache = (snap && snap.cache) || {};
  const spec = (snap && snap.speculative) || {};
  // The dials return the measured sample, or null when there is none: only real
  // measurements reach the sparklines, so idle windows leave a gap instead of a
  // false zero or a repeated stale value.
  const gen = dials.gen(th.decode_tps, th.last_decode_tps);
  const pre = dials.pre(th.prefill_tps, th.last_prefill_tps);
  dials.ttft(lat.ttft ? lat.ttft.p50 : null);
  dials.cache(pctOf(cache.prefix_hit_rate));
  dials.draft(pctOf(spec.draft_accept));

  pushSpark("gen", gen);
  pushSpark("pre", pre);
  put($("inf-gen-v"), rateText(snap, th.decode_tps, th.last_decode_tps));
  put($("inf-pre-v"), rateText(snap, th.prefill_tps, th.last_prefill_tps));
}

/* ------------------------------------------------ throughput levels card */

/* "How fast" means three different things. Active generation is the speed
   while the engine is actually decoding; effective request spreads the same
   tokens over each request's whole life, queue included; queue share is how
   much of that wall-clock time was spent waiting. Both the since-start
   totals and the ~15 min window come from the backend (throughput_levels);
   the needle follows the window and falls back to the lifetime value while
   the window holds no completed requests yet. The two texts below the dial
   always show both levels explicitly. */
const LEVELS = [
  {
    key: "active_decode_tps", label: "Active generation", unit: "tok/s",
    max: 150, warn: 40, hot: 20, invert: true,
    expl: "Speed while the engine is actually decoding.",
  },
  {
    key: "effective_request_tps", label: "Effective request", unit: "tok/s",
    max: 120, warn: 30, hot: 15, invert: true,
    expl: "Speed over each request's whole life, queue included.",
  },
  {
    key: "queue_share", label: "Queue share", unit: "%", pct: true,
    max: 100, warn: 10, hot: 25,
    expl: "Share of request time spent waiting in the queue.",
  },
];

/* Fallbacks only used when the snapshot omits the backend's own notes. */
const WORKER_NOTE =
  "Effective worker speed (whole task incl. tools and compaction): see Agents (milestone 4).";
const PREFILL_NOTE = "approximate: new-token prefill can include mixed prefill+decode steps";

/* 12.35 M / 1795.5 k / 812 — token counters run into millions, plain fixed
   notation is unreadable there. */
const fmtTok = (v) =>
  !num(v) ? "—" : v >= 1e6 ? `${fmtNum(v / 1e6, 2)} M` : v >= 1e3 ? `${fmtNum(v / 1e3, 1)} k` : fmtNum(v, 0);

const lvCard = document.createElement("section");
lvCard.className = "card span4";
const lvHead = document.createElement("h2");
lvHead.textContent = "Throughput, three ways ";
const lvWin = document.createElement("span");
lvWin.className = "right";
lvHead.appendChild(lvWin);
const lvDials = document.createElement("div");
lvDials.className = "dials";
const lvBarHost = document.createElement("div");
const lvBar = segBar(lvBarHost, { label: "Prompt tokens · cached vs new (lifetime)", total: 1 });
const lvLegend = document.createElement("div");
lvLegend.className = "legend";
const lvLegendItems = ["cached", "new"].map((name, i) => {
  const s = document.createElement("span");
  const box = document.createElement("i");
  box.style.background = `var(--${i ? "violet" : "info"})`;
  const txt = document.createElement("span");
  s.append(box, txt);
  lvLegend.appendChild(s);
  return txt;
});
const lvPreNote = document.createElement("p");
lvPreNote.className = "note";
const lvWorker = document.createElement("p");
lvWorker.className = "note";
lvCard.append(lvHead, lvDials, lvBarHost, lvLegend, lvPreNote, lvWorker);
$("inf-dials").closest(".card").after(lvCard);

const lvWidgets = LEVELS.map((l) => {
  const box = document.createElement("div");
  box.className = "lvl";
  const dialHost = document.createElement("div");
  const show = dial(dialHost, {
    label: l.label, unit: l.unit, min: 0, max: l.max, warn: l.warn, hot: l.hot, invert: !!l.invert,
  });
  const rows = ["lifetime", "last 15 min"].map((cap) => {
    const row = document.createElement("div");
    row.className = "lvl-v";
    const label = document.createElement("span");
    label.textContent = cap;
    const val = document.createElement("b");
    row.append(label, val);
    box.appendChild(row);
    return val;
  });
  const expl = document.createElement("div");
  expl.className = "lvl-expl";
  expl.textContent = l.expl;
  box.prepend(dialHost);
  box.appendChild(expl);
  lvDials.appendChild(box);
  return { show, life: rows[0], win: rows[1] };
});

function renderLevels(snap) {
  const lv = (snap && snap.throughput_levels) || {};
  const life = lv.lifetime || {};
  const win = lv.window || {};
  LEVELS.forEach((l, i) => {
    const conv = (o) => (num(o[l.key]) ? (l.pct ? o[l.key] * 100 : o[l.key]) : null);
    const vLife = conv(life);
    const vWin = conv(win);
    lvWidgets[i].show(vWin != null ? vWin : vLife);
    lvWidgets[i].life.textContent = vLife != null ? `${fmtNum(vLife, 1)} ${l.unit}` : "—";
    lvWidgets[i].win.textContent = vWin != null ? `${fmtNum(vWin, 1)} ${l.unit}` : "—";
  });
  const ws = win.window_seconds;
  lvWin.textContent = !snap
    ? ""
    : (num(ws) ? `${fmtDur(ws)} window` : "window not exposed") +
      (num(win.requests) ? ` · ${fmtNum(win.requests, 0)} requests` : "");

  const prompt = life.prompt_tokens;
  const cached = life.cached_prompt_tokens;
  const fresh = life.new_prompt_tokens;
  const has = num(prompt) && prompt > 0;
  lvBar(
    has
      ? [
          { value: num(cached) ? cached / prompt : null, color: "--info", name: "cached prompt tokens" },
          { value: num(fresh) ? fresh / prompt : null, color: "--violet", name: "new prompt tokens" },
        ]
      : [],
    has && num(life.cache_share) ? `${fmtNum(life.cache_share * 100, 1)} % cached` : "not exposed",
  );
  lvLegendItems[0].textContent = `cached ${fmtTok(cached)}`;
  lvLegendItems[1].textContent = `new ${fmtTok(fresh)}`;
  const ptps = life.prefill_new_tps;
  const pnote = typeof lv.prefill_note === "string" ? lv.prefill_note : PREFILL_NOTE;
  lvPreNote.textContent = num(ptps)
    ? `new-token prefill ≈ ${fmtNum(ptps, 0)} tok/s (${pnote})`
    : "new-token prefill ≈ — (not exposed)";
  lvWorker.textContent = !snap
    ? ""
    : typeof lv.worker_note === "string"
      ? lv.worker_note
      : WORKER_NOTE;
}

/* --------------------------------------------------------------- latency */

const LATENCY = [
  ["ttft", "TTFT"],
  ["e2e", "end-to-end"],
  ["queue", "queue"],
  ["itl", "inter-token"],
];

function renderLatency(snap) {
  const host = $("inf-latency");
  host.textContent = "";
  const lat = (snap && snap.latency) || {};
  const p95s = LATENCY.map(([k]) => (lat[k] ? lat[k].p95 : null)).filter(num);
  const total = p95s.length ? Math.max(...p95s) : 1;
  for (const [key, label] of LATENCY) {
    const h = lat[key];
    const line = document.createElement("div");
    line.className = "lat-line";
    const barHost = document.createElement("div");
    if (h && num(h.p50)) {
      line.textContent = `p50 ${fmtNum(h.p50, 2)} s · ${fmtNum(h.count, 0)} samples`;
      const tail = num(h.p95) ? h.p95 - h.p50 : null;
      segBar(barHost, { label, total })(
        [
          { value: h.p50, color: "--info" },
          { value: tail, color: "--violet" },
        ],
        num(h.p95) ? `p95 ${fmtNum(h.p95, 2)} s` : null,
      );
    } else {
      line.textContent = "no samples yet";
      segBar(barHost, { label, total })([], "not exposed");
    }
    host.append(line, barHost);
  }
}

/* -------------------------------------------------------------- requests */

function td(row, value) {
  const c = document.createElement("td");
  c.textContent = value == null ? "—" : value;
  row.appendChild(c);
  return c;
}

function renderRequests(snap) {
  const host = $("inf-reqs");
  host.textContent = "";
  const reqs = (snap && snap.requests) || [];
  put($("inf-req-count"), snap ? `${reqs.length}` : null);
  if (!reqs.length) {
    const p = document.createElement("p");
    p.className = "note";
    p.textContent = snap ? "No active requests" : "—";
    host.appendChild(p);
    return;
  }
  const maxLen = snap.model && snap.model.max_model_len;
  const table = document.createElement("table");
  table.className = "inf-table";
  const head = document.createElement("tr");
  for (const h of ["id", "state", "prompt", "cached", "output", "tok/s", "age", "ctx / max"]) {
    const th = document.createElement("th");
    th.textContent = h;
    head.appendChild(th);
  }
  const thead = document.createElement("thead");
  thead.appendChild(head);
  table.appendChild(thead);
  const body = document.createElement("tbody");
  for (const r of reqs) {
    const row = document.createElement("tr");
    td(row, r.id);
    td(row, r.state);
    td(row, fmtNum(r.prompt, 0));
    td(row, fmtNum(r.cached, 0));
    td(row, fmtNum(r.output, 0));
    td(row, fmtNum(r.tps, 1));
    td(row, fmtDur(r.age_s));
    const cell = td(row, null);
    const z = zone(r.ctx_pct, { warn: 70, hot: 90 });
    const bar = document.createElement("div");
    cell.appendChild(bar);
    segBar(bar, { label: "", total: num(maxLen) ? maxLen : 1 })(
      [{ value: r.ctx, color: `--${z === "na" ? "muted" : z}` }],
      num(maxLen) ? `${fmtNum(r.ctx, 0)} / ${fmtNum(maxLen, 0)}` : "max length not exposed",
    );
    body.appendChild(row);
  }
  table.appendChild(body);
  host.appendChild(table);
}

/* ----------------------------------------------------- speculative decode */

function renderSpec(snap) {
  const host = $("inf-spec");
  host.textContent = "";
  const spec = (snap && snap.speculative) || {};
  const depth = spec.adaptive_depth;
  $("inf-spec-depth").textContent = snap
    ? `adaptive depth ${num(depth) ? fmtNum(depth, 1) : "not exposed"}`
    : "";
  const perPos = spec.per_position;
  if (!snap) {
    const p = document.createElement("p");
    p.className = "note";
    p.textContent = "—";
    host.appendChild(p);
    return;
  }
  if (!Array.isArray(perPos) || !perPos.length) {
    const p = document.createElement("p");
    p.className = "note";
    p.textContent = "not exposed";
    host.appendChild(p);
    return;
  }
  for (const p of perPos) {
    const rate = pctOf(p.rate);
    const box = document.createElement("div");
    host.appendChild(box);
    segBar(box, { label: `position ${num(p.position) ? p.position + 1 : "?"}`, total: 100 })(
      [{ value: rate, color: "--violet" }],
      num(rate) ? `${fmtNum(rate, 1)} %` : null,
    );
  }
}

/* ---------------------------------------------------------------- traffic */

function renderTraffic(snap) {
  const tr = (snap && snap.transfers) || {};
  const mbs = (bps) => (num(bps) ? `${fmtNum(bps / 1e6, 1)} MB/s` : null);
  put($("inf-h2d"), mbs(tr.h2d_bps));
  put($("inf-d2h"), mbs(tr.d2h_bps));
  pushSpark("h2d", num(tr.h2d_bps) ? tr.h2d_bps / 1e6 : null);
  put($("inf-h2d-v"), !snap ? null : num(tr.h2d_bps) ? `${fmtNum(tr.h2d_bps / 1e6, 1)} MB/s` : null);

  const host = $("inf-tiers");
  host.textContent = "";
  const counts = snap && snap.experts ? snap.experts.tier_counts : null;
  const colors = ["--info", "--violet", "--warn", "--ok", "--hot"];
  if (snap && snap.capabilities && !snap.capabilities.experts) {
    const p = document.createElement("p");
    p.className = "note";
    p.textContent = `Not exposed by ${snap.label || snap.id}`;
    host.appendChild(p);
    return;
  }
  if (!counts || !Object.keys(counts).length) {
    segBar(host, { label: "Expert tiers", total: 1 })([], "not exposed");
    return;
  }
  const entries = Object.entries(counts);
  const total = entries.reduce((acc, [, n]) => acc + (num(n) ? n : 0), 0);
  segBar(host, { label: "Expert tiers", total: total || 1 })(
    entries.map(([tier, n], i) => ({ value: n, name: `tier ${tier}`, color: colors[i % colors.length] })),
    `${fmtNum(total, 0)} experts`,
  );
}

/* --------------------------------------------------------------- warnings */

function renderWarnings(snap) {
  const host = $("inf-warnings");
  host.textContent = "";
  const warns = snap && Array.isArray(snap.warnings) ? snap.warnings : null;
  put($("inf-warn-count"), warns ? `${warns.length}` : null);
  if (!warns || !warns.length) {
    const p = document.createElement("p");
    p.className = "note";
    p.textContent = snap ? "No warnings" : "—";
    host.appendChild(p);
    return;
  }
  const ul = document.createElement("ul");
  ul.className = "warn-list";
  for (const w of warns.slice().reverse()) {
    // newest first
    const li = document.createElement("li");
    li.textContent = w;
    ul.appendChild(li);
  }
  host.appendChild(ul);
}

/* ------------------------------------------------- engine gating, select */

/* One engine is shown at a time; the selected id lives in the URL hash as
   #inference/<id>. app.js owns the hash: it publishes every applied route as a
   `panoptes:route` event and, because this module runs after its first route,
   the load-time route is read from window.panoptesRoute(). Neither the tab nor
   the hash is rewritten here. Cards whose capability the engine lacks stay
   visible but show a single muted "Not exposed by <engine>" line instead of
   empty gauges. */
function routeEngineId(detail) {
  if (!detail || detail.tab !== "inference" || !detail.rest) return null;
  try { return decodeURIComponent(detail.rest); } catch { return null; }
}

let selectedId = routeEngineId(window.panoptesRoute && window.panoptesRoute());

// null means "no engine picked yet": renderAll falls back to the first one.
function selectEngine(id) {
  selectedId = id;
  for (const b of pillRow.querySelectorAll(".eng-pill"))
    b.classList.toggle("active", b.dataset.engine === id);
}

window.addEventListener("panoptes:route", (ev) => {
  if (!ev.detail || ev.detail.tab !== "inference") return;
  selectEngine(routeEngineId(ev.detail));
  tick();
});

function gateCard(card) {
  const content = Array.from(card.children).filter((el) => el.tagName !== "H2");
  const note = document.createElement("p");
  note.className = "note";
  note.hidden = true;
  card.insertBefore(note, content[0] || null);
  return (show, label) => {
    note.textContent = `Not exposed by ${label}`;
    note.hidden = show;
    for (const el of content) el.hidden = !show;
  };
}

const infPage = $("page-inference");
const infCards = Array.from(infPage.querySelectorAll(":scope > .card"));
// The Performance card's histogram-derived dial is TTFT p50, hence
// latency_histograms there; every other card maps to its own capability key.
const GATES = [
  { el: $("inf-dials").closest(".card"), cap: "latency_histograms", draw: renderPerformance },
  { el: lvCard, cap: "throughput_levels", draw: renderLevels },
  { el: $("inf-latency").closest(".card"), cap: "latency_histograms", draw: renderLatency },
  { el: $("inf-reqs").closest(".card"), cap: "per_request", draw: renderRequests },
  { el: $("inf-spec").closest(".card"), cap: "speculative", draw: renderSpec },
  { el: $("inf-h2d").closest(".card"), cap: "transfers", draw: renderTraffic },
];
for (const g of GATES) g.set = gateCard(g.el);

const zeroCard = document.createElement("section");
zeroCard.className = "card span12";
zeroCard.hidden = true;
{
  const h = document.createElement("h2");
  h.textContent = "Inference";
  const p = document.createElement("p");
  p.className = "note";
  p.textContent = "No inference engines configured — see config/panoptes.toml";
  zeroCard.append(h, p);
}

const pillRow = document.createElement("div");
pillRow.className = "eng-pills";
pillRow.hidden = true;
infPage.insertBefore(zeroCard, infCards[0]);
infPage.insertBefore(pillRow, zeroCard);

const pillLamps = new Map();
let pillKey = null;

/* Selector pills only exist when more than one engine is configured; with a
   single engine the tab looks exactly like it did before multi-engine. */
function syncPills(engines) {
  const key = engines.map((e) => e.id).join("\u0000");
  if (key !== pillKey) {
    pillKey = key;
    pillRow.textContent = "";
    pillLamps.clear();
    for (const e of engines) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "eng-pill";
      btn.dataset.engine = e.id;
      const dot = document.createElement("span");
      pillLamps.set(e.id, lamp(dot));
      btn.append(dot, document.createTextNode(e.label || e.id));
      btn.addEventListener("click", () => {
        selectEngine(e.id);
        location.hash = `inference/${encodeURIComponent(e.id)}`;
        tick();
      });
      pillRow.appendChild(btn);
    }
  }
  for (const e of engines) {
    const show = pillLamps.get(e.id);
    if (show) show(LAMP_FOR[e.status] || "off", "");
    const btn = pillRow.querySelector(`[data-engine="${CSS.escape(e.id)}"]`);
    if (btn) btn.classList.toggle("active", e.id === selectedId);
  }
  pillRow.hidden = engines.length <= 1;
}

function renderAll(data) {
  const engines = data && Array.isArray(data.engines) ? data.engines : null;
  const none = !!engines && engines.length === 0;
  zeroCard.hidden = !none;
  for (const card of infCards) card.hidden = none;
  if (none) {
    pillRow.hidden = true;
    return;
  }
  let snap = null;
  if (engines) {
    if (selectedId && !engines.some((e) => e.id === selectedId)) selectedId = null;
    snap = engines.find((e) => e.id === selectedId) || engines[0];
    selectedId = snap.id;
    syncPills(engines);
  } else {
    pillRow.hidden = true; // fetch failure: keep the cards, render dashes
  }
  if (snap && snap.id !== sparkId) switchSparks(snap.id);
  const caps = snap && snap.capabilities;
  const label = snap ? snap.label || snap.id : "the engine";
  renderEngine(snap);
  for (const g of GATES) {
    const ok = !caps || !!caps[g.cap];
    g.set(ok, label);
    if (ok) g.draw(snap);
  }
  renderWarnings(snap);
}

/* ------------------------------------------------------------------ poll */

let inFlight = false;
function tick() {
  if (inFlight) return;
  inFlight = true;
  fetch("/api/inference")
    .then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    })
    .then(
      (data) => {
        try {
          renderAll(data);
        } catch (err) {
          console.error("/api/inference: render failed", err);
        }
      },
      () => renderAll(null),
    )
    .finally(() => {
      inFlight = false;
    });
}

tick();
setInterval(tick, 2000);

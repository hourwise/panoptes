// Live system overview: /api/system and /api/gpus every 2 s, /api/storage every 5 s.
// Every widget comes from web/ui/kit.js; a null from the API is passed straight through
// so the widget shows its own "not exposed" state. Cards for GPUs and their PCIe paths
// are created once, on the first poll, and reused afterwards.

import { dial, sparkline, segBar, heatStrip, lamp, pcieChain, themeToggle } from "./ui/kit.js";
import { zone, fmtBytes } from "./ui/geom.js";

const $ = (id) => document.getElementById(id);
const GB = 1024 ** 3;
const MB = 1024 ** 2;
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
const pct = (part, whole) => (num(part) !== null && num(whole) > 0 ? (part / whole) * 100 : null);
const to = (bytes, unit) => (num(bytes) === null ? null : bytes / unit);
// kit turns its host into the widget itself (a dial, a bar), so every widget gets a child
// element of its own; passing a shared container would collapse it.
const slot = (box) => box.appendChild(document.createElement("div"));
const fmtUptime = (s) => {
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return (d ? `${d}d ` : "") + `${h}h ${m}m`;
};

// CPU card subtitle: "AMD Ryzen Threadripper PRO 5955WX · 16 cores / 32 threads".
// The kernel appends a redundant "16-Cores" suffix to the model name; strip it.
// Whatever the topology does not report is left out; with nothing known at all
// the logical-CPU count is shown, which is threads, not cores.
const plural = (v, word) => `${v} ${word}${v === 1 ? "" : "s"}`;
const cleanModel = (m) => m ? m.replace(/\s+\d+-cores?\s*$/i, "").trim() : m;
const topologyLabel = (t, logicalCpus) => {
  const counts = [
    num(t.cores) !== null && plural(t.cores, "core"),
    num(t.threads) !== null && plural(t.threads, "thread"),
  ].filter(Boolean);
  const parts = [cleanModel(t.model), counts.length ? counts.join(" / ") : null].filter(Boolean);
  return parts.join(" · ") || `${logicalCpus} threads`;
};

themeToggle($("theme"));
fetch("/api/health")
  .then((r) => (r.ok ? r.json() : null))
  .then((h) => {
    if (h) $("verline").textContent = `v${h.version} · Milestone ${h.milestone}`;
  })
  .catch(() => {});

/* ------------------------------------------------------------------- tabs */
// Polling below runs on wall-clock intervals no matter which tab is visible,
// so the sparklines keep filling while another section is shown.
const TAB_LABELS = [
  ["hardware", "Hardware"], ["inference", "Inference"], ["engines", "Engines"],
  ["agents", "Agents"], ["lab", "Lab"], ["control", "Control"],
];
const tabButtons = [...document.querySelectorAll(".tabs [role=tab]")];
const tabPages = new Map([...document.querySelectorAll(".tab-page")].map((p) => [p.dataset.tab, p]));

// The hash is `#<tab>`, `#<tab>/<rest>` or `#<tab>?<query>`. The tab name is
// all this router owns: `rest` and `query` are the tab module's own state (an
// engine id, a set of filters) and are left exactly as the URL has them.
//   "#agents?m=M3&f=1"           -> {tab:"agents", rest:"", query:"m=M3&f=1"}
//   "#inference/llamacpp-4060ti" -> {tab:"inference", rest:"llamacpp-4060ti", query:""}
function parseHash(hash) {
  const raw = String(hash || "").replace(/^#/, "");
  const sep = raw.search(/[/?]/);
  if (sep < 0) return { tab: raw, rest: "", query: "" };
  const tab = raw.slice(0, sep);
  const tail = raw.slice(sep + 1);
  if (raw[sep] === "?") return { tab, rest: "", query: tail };
  const q = tail.indexOf("?");
  return q < 0 ? { tab, rest: tail, query: "" } : { tab, rest: tail.slice(0, q), query: tail.slice(q + 1) };
}

// The route the URL asks for, with one exception: a tab name this page does
// not have falls back to Hardware and its sub-path is dropped. A module that
// loaded after the first route reads it here; `panoptes:route` carries detail
// in exactly this shape for every later change.
function currentRoute() {
  const route = parseHash(location.hash);
  if (TAB_LABELS.some(([key]) => key === route.tab)) return route;
  return { tab: "hardware", rest: "", query: "" };
}

function showTab(name) {
  for (const b of tabButtons) b.setAttribute("aria-selected", String(b.dataset.tab === name));
  for (const [key, page] of tabPages) page.hidden = key !== name;
  document.title = `Panoptes · ${TAB_LABELS.find(([key]) => key === name)[1]}`;
}

function applyRoute() {
  const route = currentRoute();
  const wanted = `#${route.tab}${route.rest ? `/${route.rest}` : ""}${route.query ? `?${route.query}` : ""}`;
  // The only hash this router ever rewrites: an unknown tab becoming #hardware.
  if (location.hash !== wanted) history.replaceState(null, "", wanted);
  showTab(route.tab);
  window.dispatchEvent(new CustomEvent("panoptes:route", { detail: route }));
}

for (const b of tabButtons) {
  // Switching tabs is a real navigation: the old tab's sub-path goes with it.
  b.addEventListener("click", () => { location.hash = b.dataset.tab; });
  b.addEventListener("keydown", (e) => {
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    e.preventDefault();
    const next = tabButtons[(tabButtons.indexOf(b) + step + tabButtons.length) % tabButtons.length];
    next.focus();
    location.hash = next.dataset.tab;
  });
}
window.addEventListener("hashchange", applyRoute);
window.panoptesRoute = currentRoute;
applyRoute();

/* ---------------------------------------------------------------- polling */
// One request per endpoint per interval; a tick landing while a request is in
// flight is skipped. On failure the last rendered values stay on screen and
// the header status pill goes amber or red until that endpoint answers again.
// A throw inside the render callback is our own bug: it is logged, never blamed on the endpoint.
const endpointOk = new Map();
const statusHost = $("api-lamp");
const paintLamp = () => {
  const bad = [...endpointOk.entries()].filter(([, ok]) => !ok).map(([path]) => path.slice(5));
  if (!endpointOk.size) {
    statusPill("off", "OFFLINE");
    statusHost.title = "awaiting first poll";
  } else if (bad.length === endpointOk.size) {
    statusPill("error", "OFFLINE");
    statusHost.title = `api down: ${bad.join(", ")}`;
  } else if (bad.length) {
    statusPill("warn", "DEGRADED");
    statusHost.title = `api down: ${bad.join(", ")}`;
  } else {
    statusPill("ok", "SYSTEM ONLINE");
    statusHost.title = "api ok";
  }
};
const statusPill = lamp(statusHost);
statusPill("off", "OFFLINE");

function poll(path, intervalMs, onData) {
  let inFlight = false;
  const tick = () => {
    if (inFlight) return;
    inFlight = true;
    fetch(path)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(
        (data) => {
          endpointOk.set(path, true);
          try {
            onData(data);
          } catch (err) {
            console.error(`${path}: render failed`, err);
          }
        },
        () => endpointOk.set(path, false),
      )
      .finally(() => {
        inFlight = false;
        paintLamp();
      });
  };
  tick();
  setInterval(tick, intervalMs);
}

/* -------------------------------------------------------------------- CPU */
let cpu = null;
function renderCpu(sys) {
  const c = sys.cpu || {};
  if (!cpu) {
    const n = (c.per_cpu || []).length || 1;
    const topo = sys.topology || {};
    $("cpu-cores").textContent = topologyLabel(topo, n);
    const dials = $("cpu-dials");
    cpu = {
      n,
      overall: dial(slot(dials), { label: "CPU load %", unit: "%", min: 0, max: 100, warn: 70, hot: 90 }),
      clock: dial(slot(dials), { label: "Avg clock", unit: "GHz", min: 0, max: 4.5, dp: 1 }),
      load1: dial(slot(dials), {
        label: "Load 1 min", unit: "tasks", min: 0, max: num(topo.threads) || n, dp: 2,
      }),
      heat: heatStrip($("cpu-heat"), n, { name: "cpu" }),
      spark: sparkline($("cpu-spark"), { max: 100, color: "--info", points: 90 }),
    };
  }
  const overall = num(c.overall);
  const freq = c.freq_mhz || {};
  cpu.overall(overall);
  cpu.clock(to(freq.avg, 1000));
  cpu.load1(Array.isArray(sys.load_avg) ? num(sys.load_avg[0]) : null);
  cpu.heat((c.per_cpu || []).slice(0, cpu.n));
  cpu.spark(overall);
  $("cpu-spark-v").textContent = overall === null ? "—" : `${overall.toFixed(0)} %`;
}

/* ----------------------------------------------------------------- Memory */
let mem = null;
function renderMemory(sys) {
  const m = sys.memory || {};
  if (!mem) {
    const totalGB = to(m.total, GB);
    if (totalGB === null) return; // the first poll that knows the size builds the widgets
    $("mem-total").textContent = `${totalGB.toFixed(0)} GB`;
    const dials = $("mem-dials");
    const bars = $("mem-bars");
    mem = {
      totalGB,
      ram: dial(slot(dials), { label: "RAM used %", unit: "%", min: 0, max: 100, warn: 70, hot: 90 }),
      swap: dial(slot(dials), { label: "Swap used %", unit: "%", min: 0, max: 100, warn: 70, hot: 90 }),
      ramBar: segBar(slot(bars), { label: "RAM", total: totalGB }),
      swapBar: segBar(slot(bars), { label: "Swap", total: to(m.swap_total, GB) || 0 }),
      spark: sparkline($("mem-spark"), { max: Math.ceil(totalGB), color: "--violet", points: 90 }),
    };
  }
  const usedGB = to(m.used, GB);
  const availGB = to(m.available, GB);
  const swapUsedGB = to(m.swap_used, GB);
  const swapTotalGB = to(m.swap_total, GB);
  mem.ram(pct(m.used, m.total));
  mem.swap(pct(m.swap_used, m.swap_total));
  mem.ramBar(
    usedGB === null ? null : [{ value: usedGB, color: "var(--info)", name: "used" }],
    usedGB === null ? null : `${fmtBytes(m.used)} / ${fmtBytes(m.total)}`);
  mem.swapBar(
    swapUsedGB === null || swapUsedGB <= 0 ? null : [{ value: swapUsedGB, color: "var(--warn)", name: "used" }],
    swapTotalGB === null ? "no swap"
      : swapUsedGB === null ? null : `${fmtBytes(m.swap_used)} / ${fmtBytes(m.swap_total)}`);
  mem.spark(availGB);
  $("mem-spark-v").textContent = availGB === null ? "—" : `${availGB.toFixed(1)} GB`;
}

/* -------------------------------------------------------------------- GPU */
const gpuCards = new Map();
const pcieCards = new Map();
const svgNode = () => document.createElementNS("http://www.w3.org/2000/svg", "svg");
// The chain's endpoint box is 150 px wide, so it gets a compact label: the last bracketed
// model name, else the name without its vendor prefix. Card titles always use the full name.
const chainLabel = (name) => {
  const full = name || "GPU";
  const brackets = full.match(/\[([^\]]+)\]/g);
  const label = brackets
    ? brackets[brackets.length - 1].slice(1, -1)
    : full.replace(/^(AMD|NVIDIA|Advanced Micro Devices)[^a-zA-Z]*|\s*\[[^\]]*\]/g, "").trim();
  return label.length > 24 ? `${label.slice(0, 23)}…` : label || full;
};
const hottest = (temps) => {
  if (!temps) return null;
  if (num(temps.junction) !== null) return temps.junction;
  const vals = Object.values(temps).map(num).filter((v) => v !== null);
  return vals.length ? Math.max(...vals) : null;
};
const linkTxt = (link) => {
  const parts = [];
  if (num(link.gen)) parts.push(`Gen${link.gen}`);
  if (num(link.width)) parts.push(`x${link.width}`);
  return parts.join(" ") || "not reported";
};
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};
const insertBeforeStorage = (section) => $("grid").insertBefore(section, $("storage-card"));

function renderGpu(gpu) {
  let card = gpuCards.get(gpu.id);
  if (!card) {
    const h2 = el("h2");
    const sub = el("span", "sub");
    const badge = el("span", "badge info");
    h2.append("GPU ", sub, badge);
    const dials = el("div", "dials");
    const bars = el("div");
    const kv = el("dl", "kv");
    const section = el("section", "card span4");
    section.append(h2, dials, bars, kv);
    insertBeforeStorage(section);
    card = {
      section, sub, badge, kv,
      util: dial(slot(dials), { label: "Busy %", unit: "%", min: 0, max: 100, warn: 70, hot: 90 }),
      temp: dial(slot(dials), { label: "Hottest °C", unit: "°C", min: 20, max: 110, warn: 80, hot: 95 }),
      power: dial(slot(dials), { label: "Board power", unit: "W", min: 0, max: 300, warn: 220, hot: 270 }),
      vram: segBar(bars, { label: "VRAM", total: to(gpu.vram_total, GB) || 0 }),
    };
    gpuCards.set(gpu.id, card);
  }
  card.sub.textContent = gpu.name || "GPU";
  card.badge.textContent = `${gpu.vendor || "unknown"} · ${gpu.source || "no source"}`;
  card.util(num(gpu.util_pct));
  card.temp(hottest(gpu.temps_c));
  card.power(num(gpu.power_w));
  const usedGB = to(gpu.vram_used, GB);
  card.vram(
    usedGB === null ? null : [{ value: usedGB, color: "var(--violet)", name: "used" }],
    usedGB === null ? null : `${fmtBytes(gpu.vram_used)} / ${fmtBytes(gpu.vram_total)}`);
  card.kv.textContent = "";
  const row = (k, v) => card.kv.append(el("dt", undefined, k), el("dd", undefined, v));
  const mhz = (v) => (num(v) === null ? "—" : `${v} MHz`);
  row("core clock", mhz(gpu.sclk_mhz));
  row("mem clock", mhz(gpu.mclk_mhz));
  for (const [label, t] of Object.entries(gpu.temps_c || {})) {
    row(`temp ${label}`, num(t) === null ? "—" : `${t.toFixed(0)} °C`);
  }
}

/* ------------------------------------------------------------------- PCIe */
function renderPcie(gpu) {
  const pcie = gpu.pcie;
  if (!pcie) return;
  let card = pcieCards.get(gpu.id);
  if (!card) {
    const h2 = el("h2");
    const badge = el("span", "badge ok");
    h2.append(`PCIe ${gpu.name || "GPU"} `, badge);
    const svg = svgNode();
    const caveat = el("p", "note");
    const endpointNote = el("p", "note");
    const section = el("section", "card span6");
    section.append(h2, svg, caveat, endpointNote);
    insertBeforeStorage(section);
    card = {
      section, badge,
      caveat,
      endpointNote,
      chain: pcieChain(svg, pcie, { name: chainLabel(gpu.name) }),
    };
    pcieCards.set(gpu.id, card);
  }
  card.badge.className = `badge ${pcie.below_max ? "warn" : "ok"}`;
  card.badge.textContent = `Host link ${linkTxt(pcie.host_link || pcie)}`;
  card.chain(pcie);
  card.caveat.textContent = pcie.below_max && pcie.note ? pcie.note : "";
  card.caveat.style.display = card.caveat.textContent ? "" : "none";
  const ep = pcie.endpoint;
  const host = pcie.host_link;
  const differs = ep && host && (ep.gen !== host.gen || ep.width !== host.width);
  card.endpointNote.textContent = differs ? `endpoint reports ${linkTxt(ep)}` : "";
  card.endpointNote.style.display = differs ? "" : "none";
}

/* ---------------------------------------------------------------- Storage */
const fsBars = new Map();
let ioSpark = null;
function renderStorage(st) {
  if (!ioSpark) ioSpark = sparkline($("io-spark"), { max: 1024, color: "--info", points: 120 });
  for (const fs of st.filesystems || []) {
    let bar = fsBars.get(fs.mountpoint);
    if (!bar) {
      bar = segBar(slot($("fs-bars")), { label: `Disk ${fs.mountpoint}`, total: to(fs.total, GB) || 0 });
      fsBars.set(fs.mountpoint, bar);
    }
    const usedGB = to(fs.used, GB);
    const p = pct(fs.used, fs.total);
    const z = zone(p, { warn: 80, hot: 92 });
    bar(
      usedGB === null || z === "na" ? null : [{ value: usedGB, color: `var(--${z})`, name: "used" }],
      p === null ? null : `${fmtBytes(fs.used)} / ${fmtBytes(fs.total)} · ${p.toFixed(0)} %`);
  }
  const bytesPerSec = (st.disks || [])
    .reduce((sum, d) => sum + (num(d.read_bps) || 0) + (num(d.write_bps) || 0), 0);
  const mbs = bytesPerSec / MB;
  ioSpark(mbs);
  $("io-spark-v").textContent = `${mbs.toFixed(1)} MB/s`;
}

/* ------------------------------------------------------------------ wiring */
poll("/api/system", 2000, (sys) => {
  renderCpu(sys);
  renderMemory(sys);
  const up = num(sys.uptime_s);
  if (up !== null) $("upline").textContent = `up ${fmtUptime(up)}`;
});
poll("/api/gpus", 2000, (data) => {
  const gpus = data.gpus || [];
  for (const gpu of gpus) {
    renderGpu(gpu);
    renderPcie(gpu);
  }
  orderGpuCards(gpus);
});
// B-002: biggest VRAM first (R9700 ahead of the display GPU). GPUs whose
// vram_total is unknown sort last, keeping arrival order among themselves.
const vramRank = (v) => (num(v) === null ? -1 : num(v));
function orderGpuCards(gpus) {
  const sorted = gpus.slice().sort((a, b) => vramRank(b.vram_total) - vramRank(a.vram_total));
  const anchor = $("storage-card");
  for (const gpu of sorted) {
    for (const card of [gpuCards.get(gpu.id), pcieCards.get(gpu.id)]) {
      if (card) $("grid").insertBefore(card.section, anchor);
    }
  }
}
poll("/api/storage", 5000, renderStorage);

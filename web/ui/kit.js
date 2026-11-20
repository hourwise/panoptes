// DOM component kit for the dashboard panels. Styling lives in theme.css, drawing maths in geom.js;
// nothing here re-implements either. Every constructor builds inside the host element and returns an
// update function that takes fresh values (null always means "not available", never a fake 0).

import { DIAL, arcPath, fraction, zone, heatColor, RingBuffer, fmtNum } from "./geom.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const DIAL_GEOM = { cx: 66, cy: 62, r: 48, track: 11, band: 3 };
const RING_GEOM = { size: 46, r: 19, width: 6 };
const SPARK = { w: 300, h: 64 };
const CHAIN = { boxW: 150, boxH: 58, y: 30, minGap: 60, viewW: 860 };
const LAMP_STATES = ["ok", "busy", "warn", "error", "off"];
const THEME_KEY = "wsd-theme";

const svgEl = (tag, attrs, parent) => {
  const node = document.createElementNS(SVG_NS, tag);
  for (const key in attrs) node.setAttribute(key, attrs[key]);
  if (parent) parent.appendChild(node);
  return node;
};

const htmlEl = (tag, cls, parent) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (parent) parent.appendChild(node);
  return node;
};

// Colours are theme tokens ("--ok") or any CSS colour ("var(--violet)", "#fff").
const paint = (color) => (typeof color === "string" && color.startsWith("--") ? `var(${color})` : color);
const num = (v) => typeof v === "number" && Number.isFinite(v);

/** Gauge: track, thin threshold band, value arc coloured by zone, value + unit, label below. */
export function dial(host, o) {
  const { cx, cy, r, track, band } = DIAL_GEOM;
  host.classList.add("dial");
  const svg = svgEl("svg", { viewBox: "0 0 132 112", role: "img", "aria-label": o.label }, host);
  svgEl("path", {
    d: arcPath(cx, cy, r, DIAL.start, DIAL.start + DIAL.sweep),
    stroke: "var(--track)", "stroke-width": track, fill: "none", "stroke-linecap": "round",
  }, svg);
  // thin band ring at the outer edge showing where the thresholds sit
  const at = (v) => DIAL.start + DIAL.sweep * fraction(v, o.min, o.max);
  const bands = o.invert
    ? [[o.min, o.hot, "--hot"], [o.hot, o.warn, "--warn"], [o.warn, o.max, "--ok"]]
    : [[o.min, o.warn, "--ok"], [o.warn, o.hot, "--warn"], [o.hot, o.max, "--hot"]];
  for (const [a, b, color] of bands) {
    if (!num(a) || !num(b) || a === b) continue;
    svgEl("path", {
      d: arcPath(cx, cy, r + 10, at(a), at(b) - 0.5),
      stroke: `var(${color})`, "stroke-width": band, fill: "none", opacity: 0.7,
    }, svg);
  }
  const value = svgEl("path", { stroke: "var(--ok)", "stroke-width": track, fill: "none", "stroke-linecap": "round" }, svg);
  const text = svgEl("text", { x: cx, y: cy + 4, "text-anchor": "middle", class: "v" }, svg);
  const unit = svgEl("text", { x: cx, y: cy + 20, "text-anchor": "middle", class: "u" }, svg);
  htmlEl("div", "lbl", host).textContent = o.label;

  return (v) => {
    if (!num(v)) {
      value.setAttribute("d", "");
      text.textContent = "—";
      unit.textContent = "not exposed";
      unit.setAttribute("class", "na");
      return;
    }
    value.setAttribute("d", arcPath(cx, cy, r, DIAL.start, DIAL.start + DIAL.sweep * Math.max(fraction(v, o.min, o.max), 0.004)));
    const z = zone(v, o);
    value.setAttribute("stroke", `var(--${z})`);
    text.textContent = fmtNum(v, o.dp || 0) + (z === "hot" ? " ▲" : "");
    unit.textContent = o.unit;
    unit.setAttribute("class", "u");
  };
}

/** Area chart over the last `points` pushed values. Non-numeric pushes are ignored. */
export function sparkline(svg, o) {
  const points = o.points || 150;
  const max = o.max > 0 ? o.max : 1;
  const color = paint(o.color || "--info");
  const data = new RingBuffer(points);
  svg.classList.add("spark");
  svg.setAttribute("viewBox", `0 0 ${SPARK.w} ${SPARK.h}`);
  svg.setAttribute("preserveAspectRatio", "none");
  const area = svgEl("path", { fill: color, opacity: 0.16 }, svg);
  const line = svgEl("path", {
    fill: "none", stroke: color, "stroke-width": 1.6, "vector-effect": "non-scaling-stroke",
  }, svg);
  const xs = (i) => (i / (points - 1)) * SPARK.w;
  const ys = (v) => SPARK.h - 2 - fraction(v, 0, max) * (SPARK.h - 6);
  return (v) => {
    if (!num(v)) return;
    data.push(v);
    const vals = data.values();
    const d = vals.map((y, i) => `${i ? "L" : "M"}${xs(i + points - vals.length)} ${ys(y)}`).join(" ");
    line.setAttribute("d", d);
    area.setAttribute("d", `${d} L${SPARK.w} ${SPARK.h} L${xs(points - vals.length)} ${SPARK.h} Z`);
  };
}

/** Segmented bar: update([{value, color, name}], rightHandText). */
export function segBar(host, o) {
  host.classList.add("bar");
  const top = htmlEl("div", "top", host);
  htmlEl("span", "", top).textContent = o.label;
  const right = htmlEl("span", "", top);
  const track = htmlEl("div", "track", host);
  const total = o.total > 0 ? o.total : 1;
  return (segs, text) => {
    track.textContent = "";
    for (const seg of segs || []) {
      if (!num(seg.value) || seg.value <= 0) continue;
      const cell = htmlEl("div", "seg", track);
      cell.title = seg.name || "";
      cell.style.width = `${(seg.value / total) * 100}%`;
      cell.style.background = paint(seg.color);
    }
    right.textContent = text == null ? "—" : text;
  };
}

/** Per-core heat strip: update(values[]) as percentages; null cells stay grey and say "not exposed". */
export function heatStrip(host, n, o = {}) {
  const unit = o.unit || "%";
  const name = o.name || "cell";
  host.classList.add("heat");
  const cells = Array.from({ length: n }, () => htmlEl("div", "", host));
  return (values) => {
    const list = values || [];
    cells.forEach((cell, i) => {
      const v = list[i];
      if (!num(v)) {
        cell.style.background = "";
        cell.dataset.t = cell.title = `${name} ${i}: not exposed`;
        return;
      }
      cell.style.background = heatColor(v);
      const label = `${name} ${i}: ${v.toFixed(0)}${unit}`;
      cell.dataset.t = cell.title = label;
    });
  };
}

/** Status pill: update("ok" | "busy" | "warn" | "error" | "off", text). */
export function lamp(host) {
  host.classList.add("lamp", "off");
  htmlEl("i", "", host);
  const label = htmlEl("span", "", host);
  let state = "off";
  return (next, text) => {
    const clean = LAMP_STATES.includes(next) ? next : "off";
    if (clean !== state) {
      host.classList.remove(state);
      host.classList.add(clean);
      state = clean;
    }
    if (text !== undefined) label.textContent = text;
  };
}

/** Determinate ring for model loading: update(fraction) or update(null) for an unknown share. */
export function progressRing(host) {
  const { size, r, width } = RING_GEOM;
  const circumference = 2 * Math.PI * r;
  const svg = svgEl("svg", { viewBox: `0 0 ${size} ${size}`, width: size, height: size, class: "ring", role: "img" }, host);
  svgEl("circle", { cx: size / 2, cy: size / 2, r, fill: "none", stroke: "var(--track)", "stroke-width": width }, svg);
  const arcEl = svgEl("circle", {
    cx: size / 2, cy: size / 2, r, fill: "none", stroke: "var(--ok)", "stroke-width": width,
    "stroke-dasharray": circumference, "stroke-dashoffset": circumference,
    transform: `rotate(-90 ${size / 2} ${size / 2})`,
  }, svg);
  return (f) => {
    const known = num(f);
    arcEl.setAttribute("stroke-dashoffset", known ? circumference * (1 - Math.min(1, Math.max(0, f))) : circumference);
    arcEl.setAttribute("stroke", known ? "var(--ok)" : "var(--muted)");
    svg.setAttribute("aria-label", known ? `${Math.round(f * 100)}% done` : "progress unknown");
  };
}

const HOP_LABELS = { root_port: "Root port", bridge: "Switch / bridge", endpoint: "Endpoint" };

const linkLabel = (link) => {
  const parts = [];
  if (num(link.gen)) parts.push(`Gen${link.gen}`);
  if (num(link.width)) parts.push(`x${link.width}`);
  return parts.join(" ") || "not reported";
};

/**
 * PCIe path drawn root port → endpoint (the reverse of `pcie.chain`).
 * Returns an update function taking a fresh `pcie` object from /api/gpus.
 */
export function pcieChain(svg, pcie, o = {}) {
  const draw = (p) => {
    svg.textContent = "";
    const hops = (p.chain || []).slice().reverse();
    const count = hops.length;
    const gap = Math.max(CHAIN.minGap, (CHAIN.viewW - count * CHAIN.boxW) / Math.max(1, count - 1));
    const viewW = count > 1 ? count * CHAIN.boxW + (count - 1) * gap : CHAIN.viewW;
    svg.setAttribute("viewBox", `0 0 ${viewW} 150`);
    if (!count) {
      const x = (viewW - CHAIN.boxW) / 2;
      hopBox(x, HOP_LABELS.endpoint, p.endpoint ? `max ${linkLabel(p.endpoint)}` : "no device", "endpoint");
      svgEl("text", { x: viewW / 2, y: CHAIN.y + CHAIN.boxH + 18, "text-anchor": "middle", class: "m", fill: "var(--muted)", "font-size": "10.5" }, svg)
        .textContent = "upstream path not available";
      return;
    }
    hops.forEach((hop, i) => hopBox(i * (CHAIN.boxW + gap), HOP_LABELS[hop.role] || hop.role, hop.slot, hop.role));
    hops.forEach((hop, i) => {
      if (i === count - 1) return;
      const host = i === 0 && p.host_link;
      const below = hops[i + 1];
      const link = host ? p.host_link : { gen: below.gen, width: below.width, max_gen: below.max_gen };
      const x0 = i * (CHAIN.boxW + gap) + CHAIN.boxW;
      const x1 = x0 + gap;
      const mid = (x0 + x1) / 2;
      const color = host ? (p.below_max ? "var(--warn)" : "var(--ok)") : "var(--muted)";
      svgEl("line", {
        x1: x0, y1: CHAIN.y + CHAIN.boxH / 2, x2: x1, y2: CHAIN.y + CHAIN.boxH / 2,
        stroke: color, "stroke-width": host ? 5 : 3, "stroke-linecap": "round",
        "stroke-dasharray": host ? "" : "2 6",
      }, svg);
      const gen = svgEl("text", { x: mid, y: CHAIN.y - 8, "text-anchor": "middle" }, svg);
      gen.textContent = linkLabel(link);
      if (host) gen.setAttribute("style", `font-weight:700;fill:${color}`);
      const max = num(link.max_gen) ? `max Gen${link.max_gen}` : "max unknown";
      svgEl("text", { x: mid, y: CHAIN.y + CHAIN.boxH + 18, "text-anchor": "middle", class: "m", fill: "var(--muted)", "font-size": "10.5" }, svg)
        .textContent = `${host ? "HOST LINK" : "internal"} · ${max}`;
    });

    function hopBox(x, label, sub, role) {
      const stroke = role === "root_port" ? "var(--info)" : role === "endpoint" ? "var(--violet)" : "var(--line)";
      const name = role === "endpoint" && o.name ? o.name : label;
      svgEl("rect", { x, y: CHAIN.y, width: CHAIN.boxW, height: CHAIN.boxH, rx: 10,
        fill: "var(--card2)", stroke, "stroke-width": 1.5 }, svg);
      svgEl("text", { x: x + 12, y: CHAIN.y + 22 }, svg).textContent = name;
      svgEl("text", { x: x + 12, y: CHAIN.y + 40, class: "m", fill: "var(--muted)", "font-size": "10.5" }, svg).textContent = sub;
    }
  };
  // theme.css has no `.chain` rules yet, so the sizing and token colours the mockup gives
  // `.chain` / `.chain text` / `.chain .m` travel as presentation attributes instead.
  svg.classList.add("chain");
  svg.setAttribute("width", "100%");
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  svg.setAttribute("font-size", "11.5");
  svg.setAttribute("fill", "var(--text)");
  svg.style.display = "block";
  draw(pcie);
  return draw;
}

/** Theme switch for a button: flips data-theme on <html>, remembered in localStorage when available. */
export function themeToggle(button) {
  const root = document.documentElement;
  const stored = () => {
    try { return localStorage.getItem(THEME_KEY); } catch { return null; }
  };
  const remember = (theme) => {
    try { localStorage.setItem(THEME_KEY, theme); } catch { /* private mode: the flip still works */ }
  };
  const apply = (theme) => {
    root.dataset.theme = theme;
    button.textContent = theme === "dark" ? "Light theme" : "Dark theme";
  };
  const saved = stored();
  apply(saved === "light" || saved === "dark" ? saved : root.dataset.theme === "light" ? "light" : "dark");
  const toggle = () => {
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    apply(next);
    remember(next);
  };
  button.addEventListener("click", toggle);
  return toggle;
}

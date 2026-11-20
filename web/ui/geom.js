// Pure drawing maths and formatters for the dashboard. No DOM access: Node tests this directly.

// Dial geometry from the approved mockup: gauge starts at 150° and sweeps 240° clockwise.
export const DIAL = { start: 150, sweep: 240 };

export function polar(cx, cy, r, deg) {
  return [cx + r * Math.cos(deg * Math.PI / 180), cy + r * Math.sin(deg * Math.PI / 180)];
}

// SVG path for a circular arc from a0 to a1 degrees (as the mockup's `arc`).
export function arcPath(cx, cy, r, a0, a1) {
  const [x0, y0] = polar(cx, cy, r, a0);
  const [x1, y1] = polar(cx, cy, r, a1);
  return `M${x0} ${y0} A${r} ${r} 0 ${a1 - a0 > 180 ? 1 : 0} 1 ${x1} ${y1}`;
}

// Position of v within [min, max], clamped to 0..1.
export function fraction(v, min, max) {
  if (min === max) return 0;
  return Math.min(1, Math.max(0, (v - min) / (max - min)));
}

// Threshold zone for a value: "ok" | "warn" | "hot" | "na".
// With invert, low is bad (e.g. throughput): v <= hot -> "hot", v <= warn -> "warn".
export function zone(v, { warn, hot, invert } = {}) {
  if (v === null || v === undefined || Number.isNaN(v)) return "na";
  if (invert) return v <= hot ? "hot" : v <= warn ? "warn" : "ok";
  return v >= hot ? "hot" : v >= warn ? "warn" : "ok";
}

// Teal -> amber -> rose heat colour, readable in both themes (mockup's three stops).
export function heatColor(pct) {
  const p = Math.min(100, Math.max(0, pct));
  const stops = [[0, [52, 179, 160]], [60, [240, 162, 59]], [100, [229, 72, 107]]];
  let a = stops[0], b = stops[2];
  for (let i = 0; i < 2; i++) if (p >= stops[i][0] && p <= stops[i + 1][0]) { a = stops[i]; b = stops[i + 1]; }
  const t = (p - a[0]) / (b[0] - a[0] || 1);
  const c = a[1].map((x, i) => Math.round(x + (b[1][i] - x) * t));
  return `rgba(${c[0]},${c[1]},${c[2]},${0.25 + 0.75 * Math.min(1, p / 70 + .1)})`;
}

// Fixed-capacity FIFO of the last n pushed values.
export class RingBuffer {
  constructor(n) {
    this.n = n;
    this.buf = [];
  }
  push(v) {
    this.buf.push(v);
    if (this.buf.length > this.n) this.buf.shift();
  }
  values() { return this.buf.slice(); }
  last() { return this.buf.length ? this.buf[this.buf.length - 1] : undefined; }
  get length() { return this.buf.length; }
}

// Human byte size, base 1024: "512 B", "1.5 KB", "2.0 GB" (up to TB).
export function fmtBytes(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n, i = 0;
  while (Math.abs(v) >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return i === 0 ? `${Math.round(v)} B` : `${v.toFixed(1)} ${units[i]}`;
}

export function fmtNum(v, dp = 0) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toFixed(dp);
}

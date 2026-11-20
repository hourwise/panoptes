/* Panoptes Lab: the pure formatting half of the comparison screen (T-036).
   No DOM here so `node --test` can pin it: a variant side, a signed
   difference, the row colour and the #lab? query. */

/*: A side that never measured shows this, never a 0. */
export const NO_DATA = "no data";

/*: The honest line a within-noise difference carries. */
export const NOISE_NOTE = "not distinguishable from run-to-run variation";

/** A number with at most two decimals, trailing zeros dropped, no "0.00". */
export function fmtNum(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return NO_DATA;
  const rounded = Math.round(value * 100) / 100;
  // -0.001 rounds to -0; print the plain zero, never "-0".
  return String(rounded === 0 ? 0 : rounded);
}

/** One side of a measurement as the table shows it: mean, n and min–max.
   n = 0 means the variant never reported this measurement: "no data".
   A "ratio" is shown as a percentage ("75 % (n=3, 0–100 %)"); a "count"
   as the bare number, without the unit word. */
export function fmtSide(stats, unit) {
  if (!stats || !stats.n || stats.mean === null || stats.mean === undefined) return NO_DATA;
  if (unit === "ratio") {
    return `${fmtNum(stats.mean * 100)} % (n=${stats.n}, `
      + `${fmtNum(stats.min * 100)}\u2013${fmtNum(stats.max * 100)} %)`;
  }
  const unitText = unit && unit !== "count" ? ` ${unit}` : "";
  return `${fmtNum(stats.mean)}${unitText} (n=${stats.n}, `
    + `${fmtNum(stats.min)}\u2013${fmtNum(stats.max)})`;
}

/** The modified-minus-baseline difference, sign always explicit, percent of
    the baseline in brackets — omitted when the API has no percent (baseline
    mean 0), and the whole cell is an em dash when a side has no value.
    A "ratio" difference is absolute in percentage points ("+25 pp"); the
    bracketed relative percent stays a percent of the baseline. */
export function fmtDiff(difference, unit) {
  if (!difference || difference.absolute === null || difference.absolute === undefined) {
    return "\u2014";
  }
  const value = unit === "ratio" ? difference.absolute * 100 : difference.absolute;
  const absolute = `${difference.absolute > 0 ? "+" : ""}${fmtNum(value)}`
    + (unit === "ratio" ? " pp" : "");
  if (difference.percent === null || difference.percent === undefined) return absolute;
  return `${absolute} (${difference.percent > 0 ? "+" : ""}${fmtNum(difference.percent)} %)`;
}

/** Colour class for a difference: only ever from the API's `better` field.
    A within-noise difference is neutral, however the means moved. */
export function diffClass(better, withinNoise) {
  if (withinNoise) return "";
  if (better === "modified") return "lab-ok";
  if (better === "baseline") return "lab-hot";
  return "";
}

/** The #lab? query string into the picked pair; absent keys are null. */
export function parseLabQuery(query) {
  const q = new URLSearchParams(query || "");
  return { baseline: q.get("baseline"), modified: q.get("modified") };
}

/** The hash the picked pair is stored in; empty when nothing is picked. */
export function buildLabHash(baseline, modified) {
  const q = new URLSearchParams();
  if (baseline) q.set("baseline", baseline);
  if (modified) q.set("modified", modified);
  const s = q.toString();
  return s ? `#lab?${s}` : "#lab";
}

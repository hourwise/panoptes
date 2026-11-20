/* Panoptes Lab trends: the pure maths behind the small multiples (T-045).
   No DOM and no Date-of-the-day here so `node --test` can pin it — the rolling
   median, the slot a config change falls on, the y scale, the summary line.
   Trust rule throughout (the same one wsd/jobs uses for its totals): a value
   only counts when its provenance is `measured` or `calculated`; a `partial`
   or `unavailable` one is reported as skipped, never folded into a median and
   never drawn as a zero. */

// The provenance classes that are trustworthy enough to average or mediate.
export const COMPLETE = ["measured", "calculated"];

const isNum = (v) => typeof v === "number" && Number.isFinite(v);

/** ISO-8601 stamp -> epoch ms, or null when it is not one.
   A stamp with no offset is read as UTC (the same rule wsd/trends uses), so a
   comparison never mixes a naive and an aware time. */
const STAMP_RE = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?(Z|[+-]\d{2}:?\d{2})?$/;

export function parseStamp(text) {
  const m = typeof text === "string" ? STAMP_RE.exec(text.trim()) : null;
  if (!m) return null;
  const msPart = m[7] ? Number(m[7].slice(0, 3).padEnd(3, "0")) : 0;
  const naive = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0), msPart);
  if (Number.isNaN(naive)) return null;
  const probe = new Date(naive);
  if (probe.getUTCFullYear() !== +m[1] || probe.getUTCMonth() !== +m[2] - 1
      || probe.getUTCDate() !== +m[3]) return null;   /* 2027-02-30 is not a day */
  let ms = naive;
  const offset = m[8];
  if (offset && offset !== "Z") {
    const sign = offset[0] === "-" ? -1 : 1;
    const digits = offset.slice(1).replace(":", "");
    const minutes = sign * (Number(digits.slice(0, 2)) * 60 + Number(digits.slice(2, 4)));
    ms -= minutes * 60000;
  }
  return ms;
}

/** Median of the numbers given; null when there is nothing to order.
   Even counts take the mean of the two middle values. */
export function median(values) {
  const nums = (Array.isArray(values) ? values : []).filter(isNum).sort((a, b) => a - b);
  if (!nums.length) return null;
  const mid = Math.floor(nums.length / 2);
  return nums.length % 2 ? nums[mid] : (nums[mid - 1] + nums[mid]) / 2;
}

/** One median per slot: for index i, the median of the non-null values in the
   last `window` slots up to and including i. A slot whose window holds no value
   at all is null — the line has a hole rather than a fake 0. */
export function rollingMedian(values, window) {
  const size = Math.max(1, Math.floor(window) || 1);
  const list = Array.isArray(values) ? values : [];
  return list.map((_value, index) =>
    median(list.slice(Math.max(0, index - size + 1), index + 1)));
}

/** Short label rows for the switches that land on one task slot: each profile
   once, counted. Four switches between the same two tasks are two profiles
   applied and restored twice, so the rows say that instead of drawing four
   labels over each other. `limit` caps the rows; past it the last row counts
   what is left out, so nothing is silently dropped. */
export function markerLabels(markers, limit) {
  const cap = Math.max(1, Math.floor(limit) || 1);
  const counts = new Map();
  for (const marker of Array.isArray(markers) ? markers : []) {
    const name = ((marker && marker.to) || "").replace(/\.ya?ml$/, "") || "unnamed profile";
    counts.set(name, (counts.get(name) || 0) + 1);
  }
  const rows = [...counts].map(([name, n]) => (n > 1 ? `${name} x${n}` : name));
  return rows.length <= cap
    ? rows
    : [...rows.slice(0, cap - 1), `+${rows.length - cap + 1} more`];
}

/* The gap a label keeps from its own switch line, in chart units. */
export const LABEL_PAD = 3;

/** Where every switch label goes: one row per label that would otherwise print
   over its neighbour, and an anchor that keeps it inside the chart.

   `labels` is `[{x, text, markers}]` — `x` is the switch line it belongs to,
   `markers` the switches behind that label (a merged label lists them all).
   `width` is the right edge of the drawing area, `charWidth` the width of one
   glyph of the label font, and `rows` how many rows there are above the plot.

   A label that would run past `width` is right-aligned to its line instead.
   When one fits on no row left, it and every label it collides with collapse
   into one "n switches" label, so a crowded stretch says how many switches
   happened and the tooltip (from `markers`) still lists them. Returns the
   labels left to right with `row`, `anchor`, `left` and `right` filled in. */
export function layoutMarkerLabels(labels, width, charWidth, rows) {
  const cw = isNum(charWidth) && charWidth > 0 ? charWidth : 4;
  const limit = isNum(width) && width > 0 ? width : Infinity;
  const maxRows = Math.max(1, Math.floor(rows) || 3);
  const queue = (Array.isArray(labels) ? labels : [])
    .filter((label) => label && isNum(label.x) && typeof label.text === "string" && label.text)
    .map((label) => ({ x: label.x, text: label.text, markers: label.markers || [] }))
    .sort((a, b) => a.x - b.x);
  const box = (label) => {
    const w = label.text.length * cw;
    if (label.x + LABEL_PAD + w <= limit) {
      return { anchor: "start", left: label.x + LABEL_PAD, right: label.x + LABEL_PAD + w };
    }
    if (label.x - LABEL_PAD - w >= 0) {
      return { anchor: "end", left: label.x - LABEL_PAD - w, right: label.x - LABEL_PAD };
    }
    return { anchor: "start", left: 0, right: Math.min(w, limit) };  /* wider than the chart */
  };
  const placed = [];
  const hits = (candidate) => placed.filter((one) => one.row === candidate.row
    && one.left < candidate.right && candidate.left < one.right);
  const freeRow = (candidate) => {
    for (let index = 0; index < maxRows; index += 1) {
      if (!hits({ ...candidate, row: index }).length) return index;
    }
    return -1;
  };
  const inBox = (a, b) => a.left < b.right && b.left < a.right;
  for (const label of queue) {
    const boxOf = box(label);
    const row = freeRow(boxOf);
    if (row >= 0) { placed.push({ ...label, ...boxOf, row }); continue; }
    /* the labels that blocked it: on the clamped box or on the place the label
       wanted before it was pushed off the edge */
    const w = label.text.length * cw;
    const want = { left: label.x + LABEL_PAD, right: label.x + LABEL_PAD + w };
    const crowd = placed.filter((one) => inBox(one, boxOf) || inBox(one, want));
    const markers = [...crowd.flatMap((one) => one.markers), ...label.markers];
    crowd.forEach((one) => placed.splice(placed.indexOf(one), 1));
    const merged = {
      x: Math.min(label.x, ...crowd.map((one) => one.x)),
      text: `${markers.length || crowd.length + 1} switches`,
      markers,
    };
    const mergedBox = box(merged);
    placed.push({ ...merged, ...mergedBox, row: Math.max(0, freeRow(mergedBox)) });
  }
  return placed.sort((a, b) => a.x - b.x);
}

/* A warning that names one task reads "<id>: <why>"; everything else (a missing
   records dir, an unusable journal entry) is already one line of its own. */
const WARN_ID = /^([A-Za-z]{1,4}-\d{2,})\s*:\s+(.+)$/;

/** "T-001", "T-002", "T-003", "B-9" -> "T-001–T-003, B-9". */
function idRange(ids) {
  const numeric = (id) => /^(\D*)(\d+)$/.exec(String(id));
  const sorted = [...ids].sort((a, b) => {
    const [x, y] = [numeric(a), numeric(b)];
    if (!x || !y) return String(a).localeCompare(String(b));
    return x[1] === y[1] ? Number(x[2]) - Number(y[2]) : x[1].localeCompare(y[1]);
  });
  const runs = [];
  for (const id of sorted) {
    const parts = numeric(id);
    const last = runs[runs.length - 1];
    if (parts && last && last.prefix === parts[1] && Number(last.number) + 1 === Number(parts[2])) {
      last.to = id;
      last.number = parts[2];
      last.length += 1;
    } else {
      runs.push({
        prefix: parts ? parts[1] : "", from: String(id), to: String(id),
        number: parts ? parts[2] : "", length: 1,
      });
    }
  }
  return runs.map((run) => (run.length >= 3 ? `${run.from}–${run.to}`
    : run.length === 2 ? `${run.from}, ${run.to}` : run.from)).join(", ");
}

/** Collapse the warnings that say the same thing about many tasks into one
   line, so ten early tasks do not push the charts a screen down.

   Returns `[{text, ids, detail}]` in first-seen order: `text` is the line to
   show, `ids` the tasks it covers (empty when the line stands alone) and
   `detail` the original messages, for a `<details>`. Warnings of different
   kinds never merge, and a kind that covers one task keeps its own wording. */
export function groupWarnings(warnings) {
  const kinds = new Map();
  for (const warning of Array.isArray(warnings) ? warnings : []) {
    if (typeof warning !== "string") continue;
    const parts = WARN_ID.exec(warning);
    const key = parts ? parts[2] : `\u0000${warning}`;
    if (!kinds.has(key)) kinds.set(key, []);
    kinds.get(key).push({ id: parts ? parts[1] : null, text: warning });
  }
  return [...kinds.values()].map((items) => {
    const ids = items.map((item) => item.id);
    if (items.length < 2 || ids.some((id) => id === null)) {
      return { text: items[0].text, ids: [], detail: [] };
    }
    const why = WARN_ID.exec(items[0].text)[2];
    return {
      text: `${items.length} tasks (${idRange(ids)}): ${why}`,
      ids,
      detail: items.map((item) => item.text),
    };
  });
}

/** The slot a config change is drawn at: the first task accepted at or after
   `markerTime`, or null when nothing was accepted after it (the change sits
   past the last point, so there is no line to draw). */
export function markerSlot(points, markerTime) {
  const at = parseStamp(markerTime);
  if (at === null) return null;
  const list = Array.isArray(points) ? points : [];
  for (let index = 0; index < list.length; index += 1) {
    const accepted = parseStamp(list[index] && list[index].accepted_at);
    if (accepted !== null && accepted >= at) return index;
  }
  return null;
}

/** The y scale for one chart: {min, max} with a 0 floor (counts, minutes, Wh:
   nothing here goes below zero) and `headroom` (default 10 %) above the top.
   All-null -> a unit scale; a single value or a flat line -> padded so the
   line is never drawn on the axis. Pass {zeroFloor: false} for a measure whose
   band matters (tok/s), where the floor would flatten it. */
export function scaleY(values, options) {
  const o = options || {};
  const zeroFloor = o.zeroFloor !== false;
  const headroom = isNum(o.headroom) && o.headroom > 0 ? o.headroom : 0.1;
  const nums = (Array.isArray(values) ? values : []).filter(isNum);
  if (!nums.length) return { min: 0, max: 1 };
  const low = Math.min(...nums);
  const high = Math.max(...nums);
  const min = zeroFloor ? Math.min(0, low) : low;
  const span = high - min;
  if (span > 0) return { min, max: high + span * headroom };
  const pad = (Math.abs(high) || 1) * headroom;
  return { min: zeroFloor ? min : high - pad, max: high + pad };
}

/** The number one point holds for a measure, or null when it has none.
   `plain` measures (rounds) are plain job figures with no provenance to carry,
   so they are always complete. */
export function pointValue(point, measure) {
  if (!point) return null;
  const cell = measure.plain ? { value: point[measure.key] } : point[measure.key];
  if (!cell || !isNum(cell.value)) return null;
  return cell.value * (measure.factor || 1);
}

/** Whether a point's value may be drawn solid (measured) or only hollow. */
export function pointComplete(point, measure) {
  if (!point) return false;
  if (measure.plain) return isNum(point[measure.key]);
  const cell = point[measure.key];
  return Boolean(cell) && COMPLETE.includes(cell.provenance);
}

/** Median over the values of one measure, plus how many points were skipped. */
function mediate(points, measure) {
  const list = Array.isArray(points) ? points : [];
  const used = list
    .filter((point) => pointValue(point, measure) !== null && pointComplete(point, measure))
    .map((point) => pointValue(point, measure));
  return { median: median(used), count: used.length, skipped: list.length - used.length };
}

/** The summary line's numbers: tasks shown, first-time-accept rate, and the
   median worker minutes and median Wh over the last 10 tasks against all
   tasks. `skipped` in each entry is how many tasks of that span contributed no
   trustworthy value, so the line can say what it left out. */
export function summary(points, recent) {
  const list = Array.isArray(points) ? points : [];
  const span = isNum(recent) && recent > 0 ? list.slice(-Math.floor(recent)) : list;
  const known = list
    .map((point) => point && point.first_time_accept)
    .filter((value) => typeof value === "boolean");
  const yes = known.filter(Boolean).length;
  return {
    tasks: list.length,
    first_time: {
      count: yes,
      of: known.length,
      rate: known.length ? yes / known.length : null,
    },
    worker_minutes: mediate(list, MEASURE_KEYS.worker_seconds),
    worker_minutes_recent: mediate(span, MEASURE_KEYS.worker_seconds),
    energy_wh: mediate(list, MEASURE_KEYS.total_energy_wh),
    energy_wh_recent: mediate(span, MEASURE_KEYS.total_energy_wh),
    recent: isNum(recent) && recent > 0 ? Math.min(Math.floor(recent), list.length) : list.length,
  };
}

/* The two measures the summary mediate, in the shape `mediate` needs: the
   caller never passes a measure object, so they are spelled out here rather
   imported from the DOM module. */
const MEASURE_KEYS = {
  worker_seconds: { key: "worker_seconds", factor: 1 / 60 },
  total_energy_wh: { key: "total_energy_wh", factor: 1 },
};

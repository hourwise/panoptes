import test from "node:test";
import assert from "node:assert/strict";

import {
  COMPLETE, parseStamp, median, rollingMedian, markerSlot, scaleY, summary,
  pointValue, pointComplete, markerLabels, layoutMarkerLabels, groupWarnings,
} from "../../web/lab-trends-format.js";

const point = (id, acceptedAt, seconds, provenance = "measured") => ({
  id,
  accepted_at: acceptedAt,
  first_time_accept: true,
  rounds: 1,
  worker_seconds: { value: seconds, unit: "s", provenance },
  total_energy_wh: { value: seconds / 10, unit: "Wh", provenance },
});

const MINUTES = { key: "worker_seconds", factor: 1 / 60 };

test("parseStamp reads the offsets the records and the journal write", () => {
  assert.equal(parseStamp("2026-11-20T06:19:05+0000"), Date.UTC(2026, 10, 20, 6, 19, 5));
  assert.equal(parseStamp("2026-11-20T06:19:05+00:00"), Date.UTC(2026, 10, 20, 6, 19, 5));
  assert.equal(parseStamp("2026-11-20T06:19:05Z"), Date.UTC(2026, 10, 20, 6, 19, 5));
  assert.equal(parseStamp("2026-11-20T08:19:05+02:00"), Date.UTC(2026, 10, 20, 6, 19, 5));
  assert.equal(parseStamp("2026-11-20T04:19:05-0200"), Date.UTC(2026, 10, 20, 6, 19, 5));
  assert.equal(parseStamp("2026-11-20T06:19:05"), Date.UTC(2026, 10, 20, 6, 19, 5),
    "a stamp with no offset is read as UTC, never as the machine's zone");
  assert.equal(parseStamp("2026-11-20T06:19:05.250+0000"), Date.UTC(2026, 10, 20, 6, 19, 5, 250));
});

test("parseStamp answers null for anything that is not a stamp", () => {
  for (const bad of [null, undefined, "", "  ", "yesterday", "2026-13-45T99:99:99+0000", 123, {}]) {
    assert.equal(parseStamp(bad), null, String(bad));
  }
});

test("median orders the values and splits the two middle ones", () => {
  assert.equal(median([3, 1, 2]), 2);
  assert.equal(median([4, 1, 2, 3]), 2.5);
  assert.equal(median([7]), 7);
  assert.equal(median([0]), 0, "a zero median is a zero, not null");
});

test("median ignores what is not a number and says null when nothing is left", () => {
  assert.equal(median([5, null, 7, undefined, "8", Number.NaN]), 6);
  assert.equal(median([null, null]), null);
  assert.equal(median([]), null);
  assert.equal(median(null), null);
});

test("rollingMedian takes the last window slots ending at each point", () => {
  assert.deepEqual(rollingMedian([1, 2, 3, 4, 5], 3), [1, 1.5, 2, 3, 4]);
  assert.deepEqual(rollingMedian([1, 2, 3], 5), [1, 1.5, 2], "a window longer than the data");
});

test("rollingMedian ignores nulls and stays null until its window holds a value", () => {
  assert.deepEqual(rollingMedian([null, null, 4, null], 5), [null, null, 4, 4]);
  assert.deepEqual(rollingMedian([null, 2, null, 6, null], 2), [null, 2, 2, 6, 6],
    "the window is the last slots, not the last values: a valueless slot keeps the median beside it");
  assert.deepEqual(rollingMedian([null, null], 5), [null, null]);
});

test("rollingMedian copes with an empty or missing list and a silly window", () => {
  assert.deepEqual(rollingMedian([], 5), []);
  assert.deepEqual(rollingMedian(null, 5), []);
  assert.deepEqual(rollingMedian([3, 9], 0), [3, 9], "a window of zero means one slot");
});

test("markerSlot is the first task accepted at or after the marker", () => {
  const points = [point("T-001", "2026-11-19T10:00:00+0000", 60),
                  point("T-002", "2026-11-19T12:00:00+0000", 60),
                  point("T-003", "2026-11-19T14:00:00+0000", 60)];
  assert.equal(markerSlot(points, "2026-11-19T10:00:00+0000"), 0, "an exact tie counts as after");
  assert.equal(markerSlot(points, "2026-11-19T11:00:00+0000"), 1);
  assert.equal(markerSlot(points, "2026-11-19T13:30:00+0000"), 2);
});

test("markerSlot is null when the change came after the last task", () => {
  const points = [point("T-001", "2026-11-19T10:00:00+0000", 60)];
  assert.equal(markerSlot(points, "2026-11-20T06:19:05+0000"), null);
  assert.equal(markerSlot([], "2026-11-19T10:00:00+0000"), null);
  assert.equal(markerSlot(null, "2026-11-19T10:00:00+0000"), null);
  assert.equal(markerSlot(points, "not a time"), null);
  assert.equal(markerSlot(points, null), null);
});

test("markerSlot ignores a point it cannot place", () => {
  const points = [{ id: "T-001", accepted_at: null }, point("T-002", "2026-11-19T12:00:00+0000", 60)];
  assert.equal(markerSlot(points, "2026-11-19T11:00:00+0000"), 1);
});

test("scaleY floors counts at zero and leaves headroom above the top", () => {
  assert.deepEqual(scaleY([2, 4, 6]), { min: 0, max: 6.6 });
  assert.deepEqual(scaleY([0, 3]), { min: 0, max: 3.3 });
});

test("scaleY keeps a band that never touches zero when the floor is off", () => {
  assert.deepEqual(scaleY([23, 41], { zeroFloor: false }), { min: 23, max: 42.8 });
  assert.deepEqual(scaleY([-5, 5], { zeroFloor: false }), { min: -5, max: 6 });
});

test("scaleY handles all-null, single-value and flat series", () => {
  assert.deepEqual(scaleY([null, null]), { min: 0, max: 1 });
  assert.deepEqual(scaleY([]), { min: 0, max: 1 });
  assert.deepEqual(scaleY([4]), { min: 0, max: 4.4 });
  assert.deepEqual(scaleY([3, 3, 3]), { min: 0, max: 3.3 });
  assert.deepEqual(scaleY([28, 28], { zeroFloor: false }), { min: 25.2, max: 30.8 },
    "a flat band is padded on both sides so the line is not glued to the axis");
  const flat = scaleY([0, 0]);
  assert.ok(flat.max > flat.min, "an all-zero series still has a scale to draw on");
});

test("pointValue applies the unit factor and refuses a value that is not there", () => {
  assert.equal(pointValue(point("T-001", "2026-11-19T10:00:00+0000", 1200), MINUTES), 20);
  assert.equal(pointValue({ id: "T-009", worker_seconds: { value: null, provenance: "unavailable" } },
    MINUTES), null);
  assert.equal(pointValue({}, MINUTES), null);
  assert.equal(pointValue(null, MINUTES), null);
  const rounds = { key: "rounds", plain: true };
  assert.equal(pointValue({ rounds: 3 }, rounds), 3, "a plain job figure needs no metric cell");
  assert.equal(pointValue({ rounds: null }, rounds), null);
});

test("pointComplete is false for the provenances that must be drawn hollow", () => {
  for (const provenance of COMPLETE) {
    assert.equal(pointComplete({ worker_seconds: { value: 1, provenance } }, MINUTES), true);
  }
  for (const provenance of ["partial", "unavailable"]) {
    assert.equal(pointComplete({ worker_seconds: { value: 1, provenance } }, MINUTES), false);
  }
  assert.equal(pointComplete({}, MINUTES), false);
  assert.equal(pointComplete({ rounds: 2 }, { key: "rounds", plain: true }), true);
});

test("summary counts tasks and the first-time-accept rate", () => {
  const points = [point("T-001", "2026-11-19T10:00:00+0000", 600),
    { ...point("T-002", "2026-11-19T11:00:00+0000", 600), first_time_accept: false },
    { ...point("T-003", "2026-11-19T12:00:00+0000", 600), first_time_accept: null }];
  const s = summary(points, 10);
  assert.equal(s.tasks, 3);
  assert.deepEqual(s.first_time, { count: 1, of: 2, rate: 0.5 },
    "an unknown first_time_accept is neither a yes nor a no");
});

test("summary mediates worker minutes and Wh over all tasks and the last 10", () => {
  const points = Array.from({ length: 12 }, (_v, i) =>
    point(`T-${String(i + 1).padStart(3, "0")}`, `2026-11-19T${String(10 + (i % 8)).padStart(2, "0")}:00:00+0000`,
      (i + 1) * 600));
  const s = summary(points, 10);
  assert.equal(s.recent, 10);
  assert.equal(s.worker_minutes.median, 65, "median of 10..120 min over 12 tasks");
  assert.equal(s.worker_minutes.count, 12);
  assert.equal(s.worker_minutes.skipped, 0);
  assert.equal(s.worker_minutes_recent.median, 75, "the last 10 tasks only");
  assert.equal(s.energy_wh.median, 390);
  assert.equal(s.energy_wh_recent.median, 450);
});

test("summary mediate only complete values and says what was skipped", () => {
  const points = [
    point("T-001", "2026-11-19T10:00:00+0000", 600),
    point("T-002", "2026-11-19T11:00:00+0000", 1200),
    point("T-003", "2026-11-19T12:00:00+0000", 1800, "partial"),
    point("T-004", "2026-11-19T13:00:00+0000", 2400, "calculated"),
    { id: "T-005", accepted_at: "2026-11-19T14:00:00+0000", first_time_accept: true,
      worker_seconds: { value: null, unit: "s", provenance: "unavailable" },
      total_energy_wh: { value: null, unit: "Wh", provenance: "unavailable" } },
  ];
  const s = summary(points, 10);
  assert.equal(s.worker_minutes.count, 3, "partial is not a measurement, calculated is");
  assert.equal(s.worker_minutes.skipped, 2);
  assert.equal(s.worker_minutes.median, 20, "the median of 10, 20 and 40 minutes");
  assert.equal(s.energy_wh.skipped, 2);
});

test("summary has nothing to say when there are no points", () => {
  const s = summary([], 10);
  assert.equal(s.tasks, 0);
  assert.deepEqual(s.first_time, { count: 0, of: 0, rate: null });
  assert.equal(s.worker_minutes.median, null);
  assert.equal(s.worker_minutes.skipped, 0);
  assert.equal(s.energy_wh_recent.median, null);
  assert.equal(summary(null, 10).tasks, 0);
});

test("summary honours a recent span longer than the data", () => {
  const points = [point("T-001", "2026-11-19T10:00:00+0000", 600)];
  const s = summary(points, 10);
  assert.equal(s.recent, 1);
  assert.equal(s.worker_minutes_recent.median, s.worker_minutes.median);
});

test("markerLabels count repeats and cap what will not fit", () => {
  const m = (to) => ({ to });
  assert.deepEqual(markerLabels([m("flashnext.yaml")], 3), ["flashnext"]);
  assert.deepEqual(
    markerLabels([m("a.yaml"), m("b.yaml"), m("a.yaml"), m("b.yaml")], 3),
    ["a x2", "b x2"],
    "two profiles applied and restored twice read as two counted rows");
  assert.deepEqual(markerLabels([m("a"), m("b"), m("c"), m("d")], 3), ["a", "b", "+2 more"],
    "the rows that will not fit are counted, not dropped");
  assert.deepEqual(markerLabels([m("a"), m("b")], 1), ["+2 more"], "a one-row cap says so");
  assert.deepEqual(markerLabels([m(""), m(null)], 3), ["unnamed profile x2"]);
  assert.deepEqual(markerLabels([], 3), []);
  assert.deepEqual(markerLabels(null, 3), []);
});

const CHAR = 8;          /* a generous glyph width so overlaps are found */
const spot = (label) => `${label.anchor}@${label.row}:${label.left}-${label.right}`;

test("layoutMarkerLabels puts labels that would collide on the next row", () => {
  const labels = layoutMarkerLabels([
    { x: 100, text: "flashnext-131k x2" },
    { x: 110, text: "flashnext-131k" },
  ], 400, CHAR);
  assert.deepEqual(labels.map((label) => label.row), [0, 1],
    "the later label moves down instead of printing over its neighbour");
  assert.deepEqual(labels.map((label) => label.anchor), ["start", "start"],
    "both fit inside 400, so both keep the anchor right of their line");
});

test("layoutMarkerLabels leaves labels that have room on the same row", () => {
  const labels = layoutMarkerLabels([
    { x: 40, text: "flashnext" },
    { x: 200, text: "flashnext-131k" },
  ], 400, CHAR);
  assert.deepEqual(labels.map((label) => label.row), [0, 0]);
  assert.ok(labels[0].right < labels[1].left, "no overlap, so no wasted row");
});

test("layoutMarkerLabels right-aligns a label that would run past the width", () => {
  const labels = layoutMarkerLabels([{ x: 360, text: "flashnext-131k" }], 400, CHAR);
  assert.equal(labels[0].anchor, "end");
  assert.ok(labels[0].right <= 400 && labels[0].left >= 0, `inside 400: ${spot(labels[0])}`);
  assert.equal(labels[0].right, 357, "it ends just left of its own switch line");
});

test("layoutMarkerLabels merges the labels when three rows are not enough", () => {
  const near = [100, 108, 116, 124];
  const labels = layoutMarkerLabels(
    near.map((x, index) => ({ x, text: `profile-${index}`, markers: [{ to: `p${index}` }] })),
    400, CHAR, 3);
  assert.equal(labels.length, 1, "a crowded stretch says one thing, four times stacked");
  assert.equal(labels[0].text, "4 switches");
  assert.equal(labels[0].row, 0);
  assert.ok(labels[0].left >= 0 && labels[0].right <= 400,
    `the merged label is still inside the chart: ${spot(labels[0])}`);
  assert.deepEqual(labels[0].markers, [{ to: "p0" }, { to: "p1" }, { to: "p2" }, { to: "p3" }],
    "the tooltip still lists every switch");
});

test("layoutMarkerLabels counts a merge of already-counted labels once", () => {
  const labels = layoutMarkerLabels([
    { x: 100, text: "aaa", markers: [{ to: "a" }, { to: "b" }] },
    { x: 102, text: "bbb", markers: [{ to: "c" }] },
    { x: 104, text: "ccc", markers: [{ to: "d" }] },
    { x: 106, text: "ddd", markers: [{ to: "e" }] },
  ], 120, CHAR, 1);
  assert.equal(labels.length, 1);
  assert.equal(labels[0].text, "5 switches", "x2 of a profile is still two switches");
});

test("layoutMarkerLabels copes with a label wider than the chart and with nothing", () => {
  const wide = layoutMarkerLabels([{ x: 10, text: "a-very-long-profile-name-indeed" }], 60, CHAR);
  assert.deepEqual([wide[0].left, wide[0].right], [0, 60], "clamped, never drawn off the edge");
  assert.deepEqual(layoutMarkerLabels([], 400, CHAR), []);
  assert.deepEqual(layoutMarkerLabels(null, 400, CHAR), []);
  assert.deepEqual(layoutMarkerLabels([{ x: null, text: "x" }, { x: 5, text: "" }], 400, CHAR), [],
    "a label with no place or no text is not drawn");
});

test("groupWarnings collapses tasks that were skipped for the same reason", () => {
  const missing = [];
  for (let n = 1; n <= 10; n += 1) {
    missing.push(`T-0${String(n).padStart(2, "0")}: accepted_at missing; left out of the trends`);
  }
  const groups = groupWarnings(missing);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].text,
    "10 tasks (T-001–T-010): accepted_at missing; left out of the trends");
  assert.equal(groups[0].ids.length, 10);
  assert.deepEqual(groups[0].detail, missing, "the full list survives for the details");
});

test("groupWarnings keeps other kinds on their own line", () => {
  const groups = groupWarnings([
    "T-004: accepted_at missing; left out of the trends",
    "T-006: accepted_at missing; left out of the trends",
    "T-008: accepted_at yesterday; left out of the trends",
    "engine flashnext: switch entry s7 is applied but has no finished time, skipped",
    "no [records] dir in the config; no task trends to show",
  ]);
  assert.deepEqual(groups.map((group) => group.text), [
    "2 tasks (T-004, T-006): accepted_at missing; left out of the trends",
    "T-008: accepted_at yesterday; left out of the trends",
    "engine flashnext: switch entry s7 is applied but has no finished time, skipped",
    "no [records] dir in the config; no task trends to show",
  ], "a task on its own keeps its wording, and so does anything that names no task");
  assert.deepEqual(groups.map((group) => group.ids.length), [2, 0, 0, 0]);
});

test("groupWarnings ranges only unbroken runs and handles nothing at all", () => {
  const why = "is left out of the trends";
  const warn = (id) => `${id}: ${why}`;
  assert.equal(groupWarnings([warn("T-001"), warn("T-002"), warn("T-003"), warn("T-009")])[0].text,
    `4 tasks (T-001–T-003, T-009): ${why}`);
  assert.equal(groupWarnings([warn("T-009"), warn("T-003")])[0].text,
    `2 tasks (T-003, T-009): ${why}`, "the ids are ordered even when they arrive backwards");
  assert.deepEqual(groupWarnings([]), []);
  assert.deepEqual(groupWarnings(null), []);
  assert.deepEqual(groupWarnings([null, 7]), [], "what is not a string is not a warning");
});

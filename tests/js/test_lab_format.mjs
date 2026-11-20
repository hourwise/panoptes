import test from "node:test";
import assert from "node:assert/strict";

import {
  NO_DATA, NOISE_NOTE, fmtNum, fmtSide, fmtDiff, diffClass, parseLabQuery, buildLabHash,
} from "../../web/lab-format.js";

test("fmtNum keeps at most two decimals and drops trailing zeros", () => {
  assert.equal(fmtNum(24.0), "24");
  assert.equal(fmtNum(24.567), "24.57");
  assert.equal(fmtNum(0.5), "0.5");
  assert.equal(fmtNum(-3), "-3");
  assert.equal(fmtNum(0), "0");
  assert.equal(fmtNum(-0.001), "0", "a rounded zero is never printed as -0");
});

test("fmtNum falls back to the no-data word for values that are not numbers", () => {
  assert.equal(fmtNum(Number.NaN), NO_DATA);
  assert.equal(fmtNum(Infinity), NO_DATA);
  assert.equal(fmtNum(null), NO_DATA);
});

test("fmtSide shows mean, unit, n and the min-max span", () => {
  assert.equal(fmtSide({ n: 4, mean: 24, min: 23, max: 25 }, "tok/s"),
    "24 tok/s (n=4, 23\u201325)");
  assert.equal(fmtSide({ n: 2, mean: 1.234, min: 1.2, max: 1.27 }, ""), "1.23 (n=2, 1.2\u20131.27)");
  assert.equal(fmtSide({ n: 3, mean: -2.5, min: -4, max: -1 }, "s"), "-2.5 s (n=3, -4\u2013-1)");
});

test("fmtSide reads no data for a side that never measured, never a 0", () => {
  assert.equal(fmtSide({ n: 0, mean: null, min: null, max: null }, "Wh"), NO_DATA);
  assert.equal(fmtSide({ n: 3, mean: null, min: null, max: null }, "Wh"), NO_DATA);
  assert.equal(fmtSide(null, "Wh"), NO_DATA);
  assert.equal(fmtSide(undefined, "Wh"), NO_DATA);
});

test("fmtSide shows a ratio as a percentage with a percent range", () => {
  assert.equal(fmtSide({ n: 4, mean: 0.75, min: 0, max: 1 }, "ratio"),
    "75 % (n=4, 0\u2013100 %)");
  assert.equal(fmtSide({ n: 2, mean: 0.8333, min: 0.5, max: 1 }, "ratio"),
    "83.33 % (n=2, 50\u2013100 %)");
  assert.equal(fmtSide({ n: 0, mean: null, min: null, max: null }, "ratio"), NO_DATA,
    "a ratio side that never measured still reads no data");
});

test("fmtSide shows a count as the bare number, no unit word", () => {
  assert.equal(fmtSide({ n: 2, mean: 2.5, min: 1, max: 4 }, "count"), "2.5 (n=2, 1\u20134)");
});

test("fmtDiff shows a ratio difference in percentage points", () => {
  assert.equal(fmtDiff({ absolute: 0.25, percent: 50 }, "ratio"), "+25 pp (+50 %)");
  assert.equal(fmtDiff({ absolute: -0.125, percent: -16.67 }, "ratio"),
    "-12.5 pp (-16.67 %)", "a negative ratio difference keeps its sign");
});

test("fmtDiff keeps the plain number for counts and ordinary units", () => {
  assert.equal(fmtDiff({ absolute: -1.5, percent: -37.5 }, "count"), "-1.5 (-37.5 %)");
  assert.equal(fmtDiff({ absolute: 2.5, percent: 11.36 }, "tok/s"), "+2.5 (+11.36 %)");
});

test("fmtDiff signs the absolute value and puts the percent in brackets", () => {
  assert.equal(fmtDiff({ absolute: 2.5, percent: 11.36 }), "+2.5 (+11.36 %)");
  assert.equal(fmtDiff({ absolute: -4, percent: -15.2 }), "-4 (-15.2 %)");
  assert.equal(fmtDiff({ absolute: 0, percent: 0 }), "0 (0 %)");
});

test("fmtDiff drops the brackets when the API has no percent", () => {
  assert.equal(fmtDiff({ absolute: 2.5, percent: null }), "+2.5");
});

test("fmtDiff is an em dash when a side has no value", () => {
  assert.equal(fmtDiff({ absolute: null, percent: null }), "\u2014");
  assert.equal(fmtDiff(null), "\u2014");
});

test("diffClass colours from better only", () => {
  assert.equal(diffClass("modified", false), "lab-ok");
  assert.equal(diffClass("baseline", false), "lab-hot");
  assert.equal(diffClass("tie", false), "");
});

test("diffClass stays neutral while the difference is within noise", () => {
  assert.equal(diffClass("modified", true), "");
  assert.equal(diffClass("baseline", true), "");
});

test("the noise sentence is the exact one the table shows", () => {
  assert.equal(NOISE_NOTE, "not distinguishable from run-to-run variation");
});

test("parseLabQuery reads the picked pair from a #lab query", () => {
  assert.deepEqual(parseLabQuery("baseline=base&modified=mod"),
    { baseline: "base", modified: "mod" });
  assert.deepEqual(parseLabQuery("modified=mod"), { baseline: null, modified: "mod" });
  assert.deepEqual(parseLabQuery(""), { baseline: null, modified: null });
  assert.deepEqual(parseLabQuery(null), { baseline: null, modified: null });
});

test("buildLabHash stores the pair and round-trips through parseLabQuery", () => {
  assert.equal(buildLabHash("base", "mod"), "#lab?baseline=base&modified=mod");
  assert.equal(buildLabHash("v 1", "v/2"), "#lab?baseline=v+1&modified=v%2F2");
  assert.equal(buildLabHash(null, null), "#lab");
  const hash = buildLabHash("nightly run", "exp#7");
  assert.deepEqual(parseLabQuery(hash.slice(hash.indexOf("?") + 1)),
    { baseline: "nightly run", modified: "exp#7" });
});

import test from "node:test";
import assert from "node:assert/strict";

import { polar, arcPath, DIAL, fraction, zone, heatColor, RingBuffer, fmtBytes, fmtNum } from "../../web/ui/geom.js";

const nums = (path) => path.match(/-?\d+(?:\.\d+)?(?:e-?\d+)?/gi).map(Number);

test("polar maps degrees to points on the circle", () => {
  assert.deepEqual(polar(0, 0, 1, 0).map(Math.round), [1, 0]);
  const [x, y] = polar(66, 62, 48, 150);
  assert.ok(Math.abs(x - (66 - 48 * Math.cos(Math.PI / 6))) < 1e-9);
  assert.ok(Math.abs(y - (62 + 24)) < 1e-9);
});

test("arcPath endpoints match polar at 0% and 100% of the dial sweep", () => {
  const full = nums(arcPath(66, 62, 48, DIAL.start, DIAL.start + DIAL.sweep));
  const [x0, y0] = polar(66, 62, 48, DIAL.start);
  const [x1, y1] = polar(66, 62, 48, DIAL.start + DIAL.sweep);
  assert.ok(Math.abs(full[0] - x0) < 1e-9 && Math.abs(full[1] - y0) < 1e-9);
  assert.ok(Math.abs(full[7] - x1) < 1e-9 && Math.abs(full[8] - y1) < 1e-9);
});

test("arcPath sets the large-arc flag only above 180 degrees", () => {
  assert.equal(nums(arcPath(66, 62, 48, 150, 150 + 240))[5], 1);
  assert.equal(nums(arcPath(66, 62, 48, 150, 150 + 180))[5], 0);
  assert.equal(nums(arcPath(66, 62, 48, 150, 150 + 90))[5], 0);
});

test("fraction clamps and guards min === max", () => {
  assert.equal(fraction(50, 0, 100), 0.5);
  assert.equal(fraction(-5, 0, 100), 0);
  assert.equal(fraction(105, 0, 100), 1);
  assert.equal(fraction(30, 30, 90), 0);
  assert.equal(fraction(7, 5, 5), 0);
});

test("zone classifies normal thresholds", () => {
  const th = { warn: 70, hot: 90 };
  assert.equal(zone(30, th), "ok");
  assert.equal(zone(70, th), "warn");
  assert.equal(zone(89.9, th), "warn");
  assert.equal(zone(90, th), "hot");
});

test("zone inverts so low values are bad", () => {
  const th = { warn: 60, hot: 30, invert: true };
  assert.equal(zone(94, th), "ok");
  assert.equal(zone(60, th), "warn");
  assert.equal(zone(29.9, th), "hot");
  assert.equal(zone(30, th), "hot");
});

test("zone returns na for missing or NaN values", () => {
  const th = { warn: 70, hot: 90 };
  assert.equal(zone(null, th), "na");
  assert.equal(zone(undefined, th), "na");
  assert.equal(zone(NaN, th), "na");
});

test("heatColor interpolates the three mockup stops", () => {
  assert.match(heatColor(0), /^rgba\(52,179,160,/);
  assert.match(heatColor(60), /^rgba\(240,162,59,/);
  assert.equal(heatColor(100), "rgba(229,72,107,1)");
  const alpha = Number(heatColor(0).match(/,([\d.]+)\)$/)[1]);
  assert.ok(alpha > 0 && alpha < 1);
});

test("heatColor clamps out-of-range percentages", () => {
  assert.equal(heatColor(-10), heatColor(0));
  assert.equal(heatColor(150), heatColor(100));
});

test("RingBuffer keeps the newest n values on overflow", () => {
  const rb = new RingBuffer(3);
  assert.equal(rb.length, 0);
  assert.equal(rb.last(), undefined);
  for (const v of [1, 2, 3, 4, 5]) rb.push(v);
  assert.equal(rb.length, 3);
  assert.deepEqual(rb.values(), [3, 4, 5]);
  assert.equal(rb.last(), 5);
});

test("RingBuffer values returns an independent copy", () => {
  const rb = new RingBuffer(3);
  rb.push(1); rb.push(2);
  const snapshot = rb.values();
  snapshot.push(99);
  assert.deepEqual(rb.values(), [1, 2]);
  rb.push(3);
  assert.deepEqual(snapshot, [1, 2, 99]);
});

test("fmtBytes formats with base 1024 and one decimal above bytes", () => {
  assert.equal(fmtBytes(512), "512 B");
  assert.equal(fmtBytes(1023), "1023 B");
  assert.equal(fmtBytes(5 * 1024 ** 2), "5.0 MB");
  assert.equal(fmtBytes(1536), "1.5 KB");
  assert.equal(fmtBytes(2 * 1024 ** 3), "2.0 GB");
  assert.equal(fmtBytes(3 * 1024 ** 4), "3.0 TB");
  assert.equal(fmtBytes(null), "—");
  assert.equal(fmtBytes(1024 ** 5), "1024.0 TB");
});

test("fmtNum respects dp and shows a dash for missing values", () => {
  assert.equal(fmtNum(3.7), "4");
  assert.equal(fmtNum(3.14159, 2), "3.14");
  assert.equal(fmtNum(0), "0");
  assert.equal(fmtNum(null), "—");
  assert.equal(fmtNum(undefined), "—");
  assert.equal(fmtNum(NaN), "—");
});

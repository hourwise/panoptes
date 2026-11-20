/* The Guardian chip's pure display logic (T-050): run with
   node --test "tests/js/*.mjs"                                            */
import test from "node:test";
import assert from "node:assert/strict";

import {
  CHIP_CLASS,
  LAMP_STATE,
  LEVELS,
  NO_DATA,
  UNKNOWN_MARK,
  chipClass,
  chipDetail,
  chipLabel,
  chipLevel,
  chipText,
  chipView,
} from "../../web/guardian-chip-format.js";

const display = (over) => ({ level: "ok", label: "Guardian · GREEN", detail: "", ...over });

test("every level the backend can report has its own class", () => {
  for (const level of LEVELS) {
    assert.equal(chipClass(display({ level })), `${CHIP_CLASS}-${level}`);
  }
  assert.deepEqual(LEVELS, ["off", "ok", "warn", "hot", "unknown"]);
});

test("a level with no class falls back to the unknown class", () => {
  assert.equal(chipClass(display({ level: "green" })), "gchip-unknown");
  assert.equal(chipClass(display({ level: null })), "gchip-unknown");
  assert.equal(chipClass(null), "gchip-unknown");
});

test("the label is the backend's own wording", () => {
  for (const label of [
    "Guardian · not installed",
    "Guardian · unreadable",
    "Guardian · unsupported version",
    "Guardian · not responding",
    "Guardian · AMBER (sim)",
    "E-STOP · all",
  ]) {
    assert.equal(chipLabel(display({ label })), label);
    assert.equal(chipText(display({ label })), label);
  }
});

test("the unknown level carries the ? mark", () => {
  assert.equal(chipText(display({ level: "unknown", label: "Guardian · BROKEN" })),
    "Guardian · BROKEN ?");
  assert.equal(UNKNOWN_MARK, "?");
  // and no other level does
  assert.equal(chipText(display({ level: "hot", label: "E-STOP" })), "E-STOP");
});

test("missing or unusable pieces cost the label, not the chip", () => {
  for (const empty of [null, undefined, {}, { label: "" }, { label: "   " },
                      { label: 42 }, { label: {} }, display({ label: null })]) {
    assert.equal(chipLabel(empty), NO_DATA.label, JSON.stringify(empty));
  }
});

test("with nothing at all the chip says there is no data", () => {
  assert.equal(chipLabel(null), "Guardian · no data");
  assert.equal(NO_DATA.label, "Guardian · no data");
  assert.equal(NO_DATA.level, "warn");
});

test("the detail is only ever the backend's detail text", () => {
  assert.equal(chipDetail(display({ detail: "cpu.tctl 86.4 C (85)" })), "cpu.tctl 86.4 C (85)");
  assert.equal(chipDetail(display()), "");
  assert.equal(chipDetail(null), "");
  assert.equal(chipDetail(display({ detail: 96 })), "");
});

test("a level this file does not know is shown like unknown", () => {
  for (const level of ["green", "", 0, "OK", ["ok"], null, undefined]) {
    assert.equal(chipLevel(display({ level })), "unknown", String(level));
    assert.equal(chipLevel({}), "unknown");
  }
});

test("each level maps to a lamp state the kit knows", () => {
  assert.deepEqual(LAMP_STATE, { off: "off", ok: "ok", warn: "warn", hot: "error", unknown: "off" });
  for (const level of LEVELS) assert.equal(chipView(display({ level })).lamp, LAMP_STATE[level]);
});

test("chipView is everything the DOM glue needs", () => {
  assert.deepEqual(chipView(display({
    level: "hot", label: "E-STOP · worker-7", detail: "gpu.nvidia.core 91 C (90)",
  })), {
    level: "hot",
    lamp: "error",
    text: "E-STOP · worker-7",
    cls: "gchip-hot",
    tooltip: "gpu.nvidia.core 91 C (90)",
  });
  assert.deepEqual(chipView(null), {
    level: "unknown",
    lamp: "off",
    text: "Guardian · no data ?",
    cls: "gchip-unknown",
    tooltip: "",
  });
});

test("an e-stop scope is passed through as text, whatever it says", () => {
  // The scope is a worker id from the Guardian configuration, or "all"; this
  // file knows no ids. Nothing rewrites the text it is handed, so what the
  // backend sent — including markup-shaped text — is exactly the plain string
  // the DOM glue will assign to `textContent`.
  for (const scope of ["all", "worker-7", "W-9_x", "<img src=x onerror=alert(1)>", "a".repeat(32)]) {
    const label = `E-STOP · ${scope}`;
    const view = chipView(display({ level: "hot", label }));
    assert.equal(chipLabel(display({ level: "hot", label })), label);
    assert.equal(chipText(display({ level: "hot", label })), label);
    assert.equal(view.text, label);
    assert.equal(view.cls, "gchip-hot");
  }
});

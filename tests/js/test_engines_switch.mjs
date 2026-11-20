/* Tests for the pure ENGINES switching logic (web/engines-switch.js, T-041).
 * Run with: node --test tests/js   (node:test + node:assert only, no npm). */
import test from "node:test";
import assert from "node:assert/strict";
import {
  WORKER_LOCK_NOTE,
  RUNNING_NOTE,
  INVALID_NOTE,
  ZERO_CHANGES_NOTE,
  localTime,
  outcomeBanner,
  outcomeClass,
  stepListModel,
  formatSide,
  formatHistoryRow,
  buttonStates,
  applyConfirmText,
  restoreConfirmText,
} from "../../web/engines-switch.js";

const STEP = (step, ok, extra = {}) => ({ step, at: "2026-11-18T13:07:00+00:00", ok, ...extra });
const TIME_SUB = /\d{4}-\d{2}-\d{2} \d{2}:\d{2}/;
const TIME_RE = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/;

/* ---- localTime ---------------------------------------------------------- */

test("localTime renders local YYYY-MM-DD HH:MM or a dash", () => {
  const d = new Date("2026-11-18T13:07:00Z");
  const p = (n) => String(n).padStart(2, "0");
  const want = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  assert.equal(localTime("2026-11-18T13:07:00Z"), want);
  assert.equal(localTime(null), "\u2014");
  assert.equal(localTime(""), "\u2014");
  assert.equal(localTime("not a date"), "not a date");
});

/* ---- outcomeBanner ------------------------------------------------------ */

const base = {
  started: "2026-11-18T13:07:00Z",
  finished: "2026-11-18T13:09:00Z",
  from: { name: "flashnext-default.yaml", sha256: "2b39cc2f11111111111111111111111111111111111111111111111111111111" },
  to: { name: "flashnext-spec6.yaml", sha256: "8614ece822222222222222222222222222222222222222222222222222222222" },
  errors: [],
};

test("applied banner is ok-toned and names time and both profiles", () => {
  const b = outcomeBanner({ ...base, outcome: "applied" });
  assert.equal(b.cls, "ok");
  assert.match(b.text, TIME_SUB);
  assert.match(b.text, /^applied at \d{4}-\d{2}-\d{2} \d{2}:\d{2}: /, "banner starts with applied + time");
  assert.ok(
    b.text.includes("flashnext-default.yaml \u00b7 2b39cc2f \u2192 flashnext-spec6.yaml \u00b7 8614ece8"),
    "each side carries the first 8 sha256 digits",
  );
  assert.ok(!b.text.includes("dry run"));
});

test("applied banner flags a dry run", () => {
  const b = outcomeBanner({ ...base, outcome: "applied", dry_run: true });
  assert.ok(b.text.endsWith("(dry run)"));
});

test("refused banner is neutral, says nothing was changed and quotes errors", () => {
  const b = outcomeBanner({
    ...base,
    outcome: "refused",
    from: null,
    errors: ["a supervised worker task is running"],
  });
  assert.equal(b.cls, "");
  assert.ok(b.text.includes("nothing was changed"));
  assert.ok(b.text.includes("a supervised worker task is running"));
  assert.ok(b.text.includes("\u2014 \u2192 flashnext-spec6.yaml"), "missing from shows a dash");
});

test("rolled_back banner is warn-toned with the exact promise sentence", () => {
  const b = outcomeBanner({
    ...base,
    outcome: "rolled_back",
    errors: ["radiance never became ready within 600 s"],
  });
  assert.equal(b.cls, "warn");
  assert.ok(
    b.text.includes(
      "flashnext-spec6.yaml \u00b7 8614ece8 did not start; flashnext-default.yaml \u00b7 2b39cc2f was restored automatically",
    ),
    b.text,
  );
  assert.ok(b.text.includes("radiance never became ready within 600 s"));
  assert.match(b.text, TIME_SUB);
});

test("rollback_failed banner is hot-toned and quotes the error verbatim", () => {
  const err = "backup file 20261118T130700Z-ab.yaml is missing";
  const b = outcomeBanner({ ...base, outcome: "rollback_failed", errors: [err] });
  assert.equal(b.cls, "err");
  assert.ok(b.text.startsWith("ROLLBACK FAILED at "), b.text);
  assert.ok(b.text.includes("flashnext-default.yaml \u00b7 2b39cc2f \u2192 flashnext-spec6.yaml \u00b7 8614ece8"));
  assert.ok(b.text.includes(err));
});

test("outcomeClass maps the four outcomes", () => {
  assert.equal(outcomeClass("applied"), "ok");
  assert.equal(outcomeClass("refused"), "");
  assert.equal(outcomeClass("rolled_back"), "warn");
  assert.equal(outcomeClass("rollback_failed"), "hot");
  assert.equal(outcomeClass("whatever"), "");
});

/* ---- formatSide ---------------------------------------------------------- */

test("formatSide shows the first 8 sha256 digits beside the name", () => {
  assert.equal(
    formatSide({ name: "flashnext.yaml", sha256: "2b39cc2f11111111111111111111111111111111111111111111111111111111" }),
    "flashnext.yaml \u00b7 2b39cc2f",
  );
});

test("formatSide shows the name alone when the sha is missing", () => {
  assert.equal(formatSide({ name: "flashnext.yaml" }), "flashnext.yaml");
  assert.equal(formatSide({ name: "flashnext.yaml", sha256: "" }), "flashnext.yaml");
  assert.equal(formatSide({ name: "flashnext.yaml", sha256: "   " }), "flashnext.yaml");
  assert.equal(formatSide(null), "\u2014");
});

/* ---- stepListModel ------------------------------------------------------ */

test("a finished applied switch lists all steps done", () => {
  const rows = stepListModel(
    [STEP("preconditions", true), STEP("validate", true), STEP("backup", true), STEP("write", true), STEP("restart", true), STEP("ready", true)],
    false,
  );
  assert.deepEqual(rows.map((r) => [r.step, r.state]), [
    ["preconditions", "done"], ["validate", "done"], ["backup", "done"],
    ["write", "done"], ["restart", "done"], ["ready", "done"],
  ]);
});

test("a rolled-back switch keeps the duplicate restart/ready rows and their failures", () => {
  const steps = [
    STEP("preconditions", true), STEP("validate", true, { detail: { warnings: [] } }),
    STEP("backup", true), STEP("write", true),
    STEP("restart", false, { error: "restart failed" }),
    STEP("rollback", false, { error: "restart failed" }),
    STEP("restore", true, { detail: "20261118T130700Z-ab.yaml" }),
    STEP("restart", true), STEP("ready", true),
  ];
  const rows = stepListModel(steps, false);
  assert.deepEqual(rows.map((r) => [r.step, r.state]), [
    ["preconditions", "done"], ["validate", "done"], ["backup", "done"], ["write", "done"],
    ["restart", "failed"], ["rollback", "failed"], ["restore", "done"],
    ["restart", "done"], ["ready", "done"],
  ]);
  assert.equal(rows[4].error, "restart failed");
  assert.equal(rows[6].detail, "20261118T130700Z-ab.yaml");
});

test("a finished rolled-back entry has no current step and keeps the failed one", () => {
  const steps = [
    STEP("preconditions", true), STEP("validate", true), STEP("backup", true), STEP("write", true),
    STEP("restart", true),
    STEP("ready", false, { error: "radiance never became ready within 600 s" }),
    STEP("rollback", true), STEP("restore", true, { detail: "20261118T130700Z-ab.yaml" }),
    STEP("restart", true), STEP("ready", true),
  ];
  const rows = stepListModel(steps, false); // the journal is closed
  assert.ok(!rows.some((r) => r.state === "current"), "no step may be current once the switch is over");
  assert.deepEqual(rows.map((r) => r.step), [
    "preconditions", "validate", "backup", "write", "restart", "ready",
    "rollback", "restore", "restart", "ready",
  ], "the list keeps the journal's order");
  assert.equal(rows[5].state, "failed");
  assert.equal(rows[5].error, "radiance never became ready within 600 s", "the failed step still carries its error");
});

test("running switches append the step the journal has not recorded yet", () => {
  const done = (names) => names.map((n) => STEP(n, true));
  const last = (rows) => rows[rows.length - 1];

  assert.deepEqual(stepListModel([], true).map((r) => [r.step, r.state]), [["preconditions", "current"]]);
  assert.equal(last(stepListModel(done(["preconditions", "validate", "backup"]), true)).step, "write");
  assert.equal(last(stepListModel(done(["preconditions", "validate", "backup", "write"]), true)).step, "restart");
  assert.equal(last(stepListModel(done(["preconditions", "validate", "backup", "write", "restart"]), true)).step, "ready");
  // a failed first start means the rollback is what runs next
  assert.equal(last(stepListModel([...done(["preconditions", "validate", "backup", "write"]), STEP("restart", false)], true)).step, "rollback");
  assert.equal(last(stepListModel([...done(["preconditions", "validate", "backup", "write"]), STEP("restart", false), STEP("rollback", false)], true)).step, "restore");
  // the second start after a restore is back on the normal ladder
  assert.equal(last(stepListModel([...done(["restart", "ready"]), STEP("restart", false), STEP("rollback", false), ...done(["restore"])], true)).step, "restart");
  assert.equal(last(stepListModel([...done(["restart", "ready"]), STEP("restart", false), STEP("rollback", false), ...done(["restore", "restart"])], true)).step, "ready");
  assert.equal(last(stepListModel([], true)).state, "current");
});

/* ---- formatHistoryRow ---------------------------------------------------- */

test("history rows format action, change, outcome and restore availability", () => {
  const row = formatHistoryRow({
    id: "sw-20261118T130700Z-ab12cd",
    action: "restore",
    restore_of: "sw-20261117T090000Z-ff00aa",
    started: "2026-11-18T13:07:00Z",
    from: { name: "flashnext-default.yaml", sha256: "2b39cc2f11111111111111111111111111111111111111111111111111111111" },
    to: { name: "flashnext-spec6.yaml", sha256: "8614ece822222222222222222222222222222222222222222222222222222222" },
    outcome: "rolled_back",
    backup: "20261118T130700Z-ab.yaml",
  });
  assert.equal(row.action, "restore of sw-20261117T090000Z-ff00aa");
  assert.match(row.started, TIME_RE);
  assert.equal(row.change, "flashnext-default.yaml \u00b7 2b39cc2f \u2192 flashnext-spec6.yaml \u00b7 8614ece8");
  assert.equal(row.outcomeCls, "warn");
  assert.equal(row.backup, "20261118T130700Z-ab.yaml");
  assert.equal(row.canRestore, true);

  const applyRow = formatHistoryRow({ action: "apply", outcome: "applied", backup: null });
  assert.equal(applyRow.action, "apply");
  assert.equal(applyRow.canRestore, false);
  assert.equal(applyRow.backup, "\u2014");
  assert.equal(applyRow.change, "\u2014 \u2192 \u2014");

  const noSha = formatHistoryRow({ action: "apply", outcome: "applied", from: { name: "a.yaml" }, to: { name: "b.yaml" } });
  assert.equal(noSha.change, "a.yaml \u2192 b.yaml", "a missing sha shows the name alone");
});

/* ---- buttonStates -------------------------------------------------------- */

test("buttons are all enabled when nothing is going on", () => {
  assert.deepEqual(buttonStates({ valid: true, changeCount: 3 }), { preview: null, apply: null, restore: null });
});

test("the worker lock disables apply and restore but not preview", () => {
  const s = buttonStates({ workerActive: true, valid: true, changeCount: 3 });
  assert.equal(s.preview, null);
  assert.equal(s.apply, WORKER_LOCK_NOTE);
  assert.equal(s.restore, WORKER_LOCK_NOTE);
});

test("a running switch disables preview, apply and restore", () => {
  const s = buttonStates({ switchRunning: true, valid: true, changeCount: 3 });
  assert.deepEqual(s, { preview: RUNNING_NOTE, apply: RUNNING_NOTE, restore: RUNNING_NOTE });
});

test("the apply column folds in the preview verdict", () => {
  assert.equal(buttonStates({ active: true, valid: true, changeCount: 1 }).apply, "This is the active profile.");
  assert.equal(buttonStates({ valid: false, changeCount: 4 }).apply, INVALID_NOTE);
  assert.equal(buttonStates({ valid: true, changeCount: 0 }).apply, ZERO_CHANGES_NOTE);
  // the lock still wins over the preview verdict
  assert.equal(buttonStates({ workerActive: true, valid: false, changeCount: 0 }).apply, WORKER_LOCK_NOTE);
});

/* ---- confirm texts ------------------------------------------------------- */

test("the apply confirm asks by name and states the automatic restore", () => {
  assert.equal(
    applyConfirmText("flashnext-spec6.yaml"),
    "Switch Radiance to flashnext-spec6.yaml? Radiance restarts. If it does not come back healthy and ready, the previous profile is restored automatically.",
  );
});

test("the restore confirm names the profile and its local start time", () => {
  const t = restoreConfirmText("flashnext-default.yaml", "2026-11-18T13:07:00Z");
  assert.ok(t.startsWith("Put back flashnext-default.yaml as it was before "), t);
  assert.match(t, TIME_SUB);
  assert.ok(t.endsWith("? Radiance restarts."), t);
});

/* Pure logic behind the ENGINES tab's profile switching (T-041): outcome
 * banners, the in-flight step list, history rows, button enablement and the
 * confirm texts. No DOM and no imports, so `node --test` can exercise it
 * directly. web/engines.js owns the drawing. */

export const WORKER_LOCK_NOTE = "A supervised worker task is running: profile switching is locked";
export const RUNNING_NOTE = "A profile switch is running.";
export const RESTART_NOTE = "Radiance restarts to apply this (model reload, readiness check up to 10 min).";
export const QUALITY_NOTE = "changes model limits or output quality: results before and after are not directly comparable";
export const INVALID_NOTE = "This profile cannot be applied: nothing will be changed.";
export const ZERO_CHANGES_NOTE = "Identical to the active profile.";
export const EMPTY_HISTORY_NOTE = "No profile switches yet.";

const DASH = "\u2014";
const ARROW = " \u2192 ";

/** ISO timestamp -> local "YYYY-MM-DD HH:MM"; missing -> "—", odd input verbatim. */
export function localTime(iso) {
  if (typeof iso !== "string" || !iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* Banner classes follow .op-result (.ok/.warn/.err — .err paints --hot);
 * history badges follow .badge (.ok/.warn/.hot, refused stays unstyled). */
const OUTCOME_CLASS = { applied: "ok", refused: "", rolled_back: "warn", rollback_failed: "hot" };

export function outcomeClass(outcome) {
  return OUTCOME_CLASS[outcome] ?? "";
}

const dryTail = (e) => (e.dry_run ? " (dry run)" : "");

/* One switch side as it is shown to the user: the profile name plus the
 * first 8 hex digits of its sha256 ("flashnext.yaml · 2b39cc2f"), so a row
 * says which content was in effect, not just which file was written. The
 * file is always flashnext.yaml; the hash is what changed. These 8 digits
 * match the profile table's SHA256_12 column. Missing sha -> name alone. */
export function formatSide(side) {
  const name = (side && side.name) || DASH;
  const sha = side && typeof side.sha256 === "string" ? side.sha256.trim() : "";
  return name === DASH || !sha ? name : `${name} \u00b7 ${sha.slice(0, 8)}`;
}

/**
 * The outcome banner for a finished switch entry: `{cls, text}` where cls is
 * "" | "ok" | "warn" | "err". Every banner carries the time and the from → to
 * names; refused says nothing was changed, rolled_back names both profiles,
 * rollback_failed quotes the errors verbatim.
 */
export function outcomeBanner(entry) {
  const e = entry || {};
  const at = localTime(e.finished || e.started);
  const time = at === DASH ? "" : ` at ${at}`;
  const from = formatSide(e.from);
  const to = formatSide(e.to);
  const errors = (e.errors || []).join("\n");
  switch (e.outcome) {
    case "applied":
      return { cls: "ok", text: `applied${time}: ${from}${ARROW}${to}${dryTail(e)}` };
    case "refused":
      return {
        cls: "",
        text: `refused${time}: ${from}${ARROW}${to} \u2014 nothing was changed, the profile file was not touched.${errors ? `\n${errors}` : ""}`,
      };
    case "rolled_back":
      return {
        cls: "warn",
        text: `rolled back${time}: ${to} did not start; ${from} was restored automatically${dryTail(e)}.${errors ? `\n${errors}` : ""}`,
      };
    case "rollback_failed":
      return {
        cls: "err",
        text: `ROLLBACK FAILED${time}: ${from}${ARROW}${to} needs attention:\n${errors || "the dashboard server returned no reason"}`,
      };
    default:
      return { cls: "", text: `switch ${e.outcome ?? "unknown"}${time}: ${from}${ARROW}${to}${errors ? `\n${errors}` : ""}` };
  }
}

/* The step still in flight when the journal shows `lastDone` finished ok.
 * restart/ready each appear twice on a rollback (once per start attempt);
 * restore follows rollback, matching wsd/controls/apply.py. */
const NEXT_AFTER_OK = {
  preconditions: "validate",
  validate: "backup",
  backup: "write",
  write: "restart",
  restart: "ready",
  restore: "restart",
};

/**
 * The step list for a running or finished switch: one row per journaled step
 * (done / failed) plus, while `running`, a current row for the step the
 * journal has not recorded yet (the pipeline appends a step when it ends).
 */
export function stepListModel(steps, running) {
  const rows = (Array.isArray(steps) ? steps : []).map((s) => ({
    step: String(s && s.step != null ? s.step : "?"),
    state: s && s.ok ? "done" : "failed",
    detail: (s && s.detail) ?? null,
    error: (s && s.error) ?? null,
  }));
  if (!running) return rows;
  const last = rows[rows.length - 1];
  let next = null;
  if (!last) next = "preconditions";
  else if (last.state === "done") next = NEXT_AFTER_OK[last.step] ?? null;
  else if (last.step === "restart" || last.step === "ready") next = "rollback";
  else if (last.step === "rollback") next = "restore";
  if (next) rows.push({ step: next, state: "current", detail: null, error: null });
  return rows;
}

/**
 * One history row as display strings, newest-first order being the caller's
 * job (the journal arrives newest first and is never re-sorted). A restore
 * names the entry it puts back.
 */
export function formatHistoryRow(entry) {
  const e = entry || {};
  return {
    started: localTime(e.started),
    action: e.action === "restore" ? `restore of ${e.restore_of}` : String(e.action ?? DASH),
    change: `${formatSide(e.from)}${ARROW}${formatSide(e.to)}`,
    outcome: e.outcome ?? DASH,
    outcomeCls: outcomeClass(e.outcome),
    backup: e.backup || DASH,
    canRestore: Boolean(e.backup),
  };
}

/**
 * Why each switching button is disabled (null = enabled). Preview stays
 * clickable while a worker task runs (it only reads); everything else locks
 * for the worker and for any switch in flight. The apply column also folds in
 * the preview verdict: an invalid profile or an identical one must not apply.
 */
export function buttonStates({
  workerActive = false,
  switchRunning = false,
  active = false,
  valid = true,
  changeCount = 0,
} = {}) {
  const worker = workerActive ? WORKER_LOCK_NOTE : null;
  const running = switchRunning ? RUNNING_NOTE : null;
  const lock = worker || running;
  const apply =
    lock ||
    (active ? "This is the active profile." : null) ||
    (!valid ? INVALID_NOTE : null) ||
    (changeCount === 0 ? ZERO_CHANGES_NOTE : null);
  return { preview: running, apply, restore: lock };
}

export const applyConfirmText = (name) =>
  `Switch Radiance to ${name}? Radiance restarts. If it does not come back healthy and ready, the previous profile is restored automatically.`;

export const restoreConfirmText = (fromName, started) =>
  `Put back ${fromName} as it was before ${localTime(started)}? Radiance restarts.`;

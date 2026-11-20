// Panoptes Engines tab — engine and profile viewer with profile switching.
// Polls /api/engines every 5 s (never overlapping) and draws one card per
// engine: lamp, label, type badge, base_url, model id and capability chips
// (exposed = teal, missing = muted "not exposed"). Engines that report
// has_profiles fetch /api/engines/<id>/profiles once and draw a table;
// clicking a row opens a viewer under the table with two view toggles,
// "Diff vs active" and "Full file". Switching: "Preview switch" on each
// non-active row fills a preview panel; Apply… and Restore… fire only after
// the in-page confirm, always POST {"confirm": true} with the page token
// (the control.js pattern), follow the synchronous POST with a 2 s
// GET .../switch poll that draws the step lamps, and finish with an outcome
// banner and the entry's final step list plus refreshed profiles and history.
// Switching restarts Radiance.

import { lamp } from "./ui/kit.js";
import {
  WORKER_LOCK_NOTE,
  RESTART_NOTE,
  QUALITY_NOTE,
  INVALID_NOTE,
  ZERO_CHANGES_NOTE,
  EMPTY_HISTORY_NOTE,
  outcomeBanner,
  stepListModel,
  formatHistoryRow,
  buttonStates,
  applyConfirmText,
  restoreConfirmText,
} from "./engines-switch.js";

const page = document.getElementById("page-engines");

const token = document.querySelector('meta[name="wsd-token"]')?.content || "";

let workerActive = false; // worker_active from /api/control/status
let postsInFlight = 0; // switch/restore POSTs running anywhere on the page

function syncAll() {
  for (const st of cards.values()) st.syncLocks();
}

function pollWorker() {
  fetch("/api/control/status")
    .then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    })
    .then(
      (p) => {
        workerActive = !!(p && p.worker_active);
        syncAll();
      },
      () => {},
    );
}

/* The single POST site in this file: a switch must never fire without the
 * in-page confirm, so the body is always {"confirm": true} (control.js rule,
 * pinned by tests/test_ui_static.py). */
async function postSwitch(url) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-WSD-Token": token },
    body: JSON.stringify({ confirm: true }),
  });
  return resp.json();
}

const num = (v) => typeof v === "number" && Number.isFinite(v);

const LAMP_FOR = { ready: "ok", loading: "busy", down: "off", error: "error" };

const CAP_LABELS = {
  per_request: "per-request stats",
  speculative: "speculative decode",
  experts: "experts",
  transfers: "transfers",
  latency_histograms: "latency histograms",
  throughput_levels: "throughput levels",
  prefix_cache: "prefix cache",
  lifecycle: "lifecycle",
  controls: "controls",
};

/* "2026-11-18T13:07:00+00:00" → "2026-11-18 13:07 UTC"; anything odder is
   shown verbatim rather than mangled. */
function fmtModified(iso) {
  if (typeof iso !== "string") return "—";
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(iso);
  return m ? `${m[1]} ${m[2]} UTC` : iso;
}

function fmtSize(bytes) {
  if (!num(bytes)) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

const el = (tag, cls, parent) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (parent) parent.appendChild(n);
  return n;
};

/* Step details are strings or small objects (validate returns {warnings});
   objects are flattened to something readable, never JSON-dumped. */
function detailText(d) {
  if (d == null) return "";
  if (typeof d === "object") {
    const w = Array.isArray(d.warnings) ? d.warnings : [];
    return w.length ? `warnings: ${w.join("; ")}` : "";
  }
  return String(d);
}

/* ------------------------------------------------------------- profile view */

/* Classify one unified-diff line: file headers and @@ hunk markers are
   muted, additions teal, deletions rose, everything else context. The
   header check runs first so "--- file" is not painted as a deletion. */
const diffLineClass = (line) =>
  line.startsWith("@@") || line.startsWith("---") || line.startsWith("+++")
    ? "hunk"
    : line.startsWith("+")
      ? "add"
      : line.startsWith("-")
        ? "del"
        : "ctx";

function fillDiff(pre, text) {
  pre.textContent = "";
  for (const line of text.split("\n")) {
    el("span", `ln ${diffLineClass(line)}`, pre).textContent = line;
  }
}

/* --------------------------------------------------------------- engine card */

function makeCard(id) {
  const card = el("section", "card span12 eng-card", page);
  card.id = `eng-card-${id}`;

  const head = el("h2", "", card);
  const lampHost = el("div", "", head);
  const setLamp = lamp(lampHost);
  const labelEl = el("span", "sub", head);
  const typeBadge = el("span", "badge info", head);
  const dryBadge = el("span", "badge info", head);
  dryBadge.textContent = "dry run";
  dryBadge.hidden = true;

  const kv = el("dl", "kv", card);
  const urlRow = el("div", "", kv);
  el("dt", "", urlRow).textContent = "base URL";
  const urlDd = el("dd", "", urlRow);
  const modelRow = el("div", "", kv);
  el("dt", "", modelRow).textContent = "model";
  const modelDd = el("dd", "", modelRow);

  const chips = el("div", "cap-chips", card);

  const tableWrap = el("div", "prof-wrap", card);
  tableWrap.hidden = true;
  const table = el("table", "eng-table", tableWrap);
  const thead = el("thead", "", table);
  const hr = el("tr", "", thead);
  for (const h of ["name", "", "sha256_12", "size", "modified", "", "switch"])
    el("th", "", hr).textContent = h;
  const tbody = el("tbody", "", table);
  const tableNote = el("p", "note", tableWrap);
  tableNote.hidden = true;

  const view = el("div", "prof-view", card);
  view.hidden = true;
  const toggle = el("div", "view-toggle", view);
  const diffBtn = el("button", "active", toggle);
  diffBtn.type = "button";
  diffBtn.textContent = "Diff vs active";
  const fullBtn = el("button", "", toggle);
  fullBtn.type = "button";
  fullBtn.textContent = "Full file";
  const pre = el("pre", "diff", view);
  const viewNote = el("p", "note", view);
  viewNote.hidden = true;

  diffBtn.addEventListener("click", () => {
    st.mode = "diff";
    showViewer(currentProfile());
  });
  fullBtn.addEventListener("click", () => {
    st.mode = "full";
    showViewer(currentProfile());
  });

  // Shown only on cards that actually list profiles (backlog B-004).
  const roNote = el("p", "note eng-ro-note", card);
  roNote.hidden = true;
  roNote.textContent =
    "Switching restarts Radiance. Every switch is previewed, backed up and journalled; a failed start restores the previous profile.";

  // ---- switching --------------------------------------------------------
  const workerNote = el("p", "warn-note", card);
  workerNote.hidden = true;
  workerNote.textContent = WORKER_LOCK_NOTE;

  const preview = el("div", "sw-preview", card);
  preview.hidden = true;
  const pvTitle = el("h3", "", preview);
  const pvRestart = el("p", "note", preview);
  pvRestart.textContent = RESTART_NOTE;
  const pvErrors = el("p", "sw-errors", preview);
  pvErrors.hidden = true;
  const pvInvalid = el("p", "note", preview);
  pvInvalid.hidden = true;
  pvInvalid.textContent = INVALID_NOTE;
  const pvWarns = el("p", "warn-note", preview);
  pvWarns.hidden = true;
  const pvQs = el("p", "warn-note", preview);
  pvQs.hidden = true;
  pvQs.textContent = `Quality-sensitive: ${QUALITY_NOTE}`;
  const pvTable = el("table", "eng-table", preview);
  const pvHeadRow = el("tr", "", el("thead", "", pvTable));
  for (const h of ["setting", "from", "to", "unit"]) el("th", "", pvHeadRow).textContent = h;
  const pvBody = el("tbody", "", pvTable);
  const pvZero = el("p", "note", preview);
  pvZero.hidden = true;
  pvZero.textContent = ZERO_CHANGES_NOTE;
  const pvActions = el("div", "sw-actions", preview);
  const applyBtn = el("button", "sw-btn", pvActions);
  applyBtn.type = "button";
  applyBtn.dataset.role = "apply";
  applyBtn.textContent = "Apply…";

  const confirmBox = el("div", "confirm-step", card);
  confirmBox.hidden = true;
  const confirmText = el("p", "", confirmBox);
  const confirmRow = el("div", "sw-actions", confirmBox);
  const yesBtn = el("button", "", confirmRow);
  yesBtn.type = "button";
  yesBtn.textContent = "Confirm";
  const noBtn = el("button", "", confirmRow);
  noBtn.type = "button";
  noBtn.textContent = "Cancel";

  const progress = el("div", "sw-progress", card);
  progress.hidden = true;
  const progressNote = el("p", "note", progress);
  progressNote.textContent = "Profile switch in progress — following the journal…";
  const stepsHost = el("div", "", progress);

  const outcome = el("p", "op-result sw-result", card);
  outcome.hidden = true;

  const histEl = el("div", "sw-history", card);
  histEl.hidden = true;
  const histTitle = el("h3", "", histEl);
  histTitle.textContent = "Profile switch history";
  const histTable = el("table", "eng-table", histEl);
  const histHeadRow = el("tr", "", el("thead", "", histTable));
  for (const h of ["started", "action", "from → to", "outcome", "backup", ""])
    el("th", "", histHeadRow).textContent = h;
  const histBody = el("tbody", "", histTable);
  const histNote = el("p", "note", histEl);
  histNote.textContent = EMPTY_HISTORY_NOTE;
  const histWarn = el("p", "warn-note", histEl);
  histWarn.hidden = true;

  const st = {
    id,
    card,
    profiles: null, // last fetched list, null until the first fetch resolves
    profilesFailed: false,
    profilesFetching: false,
    capKey: null, // JSON of the last capabilities object, to skip chip rebuilds
    selected: null, // profile name shown in the viewer
    mode: "diff", // "diff" | "full"
    fullCache: new Map(), // name -> text
    fullPending: false,
    hasProfiles: false,
    previewName: null, // profile shown in the preview panel
    previewData: null, // its preview payload, null until fetched
    previewFetching: false,
    history: null, // journal entries, newest first, null until fetched
    historyFetching: false,
    switchChecked: false, // the running-switch check has been made once
    switchRunning: false, // GET .../switch says a switch is in flight here
    inFlight: false, // this card's POST is running
    pending: null, // {kind, target} awaiting the in-page confirm
    dryRun: false,
  };

  function currentProfile() {
    if (!st.profiles || !st.selected) return null;
    return st.profiles.find((p) => p.name === st.selected) || null;
  }

  function showViewer(profile) {
    view.hidden = false;
    diffBtn.classList.toggle("active", st.mode === "diff");
    fullBtn.classList.toggle("active", st.mode === "full");
    viewNote.hidden = true;
    if (st.mode === "diff") {
      if (profile && profile.active) {
        pre.textContent = "";
        viewNote.hidden = false;
        viewNote.textContent = "This is the active profile — the engine is running it, so there is nothing to diff.";
      } else if (!profile || !profile.diff_vs_active) {
        pre.textContent = "";
        viewNote.hidden = false;
        viewNote.textContent = "No diff available: no readable active profile to compare against.";
      } else {
        fillDiff(pre, profile.diff_vs_active);
        if (!pre.textContent.trim()) {
          pre.textContent = "";
          viewNote.hidden = false;
          viewNote.textContent = "Identical to the active profile.";
        }
      }
      return;
    }
    // Full file: served per profile, cached after the first view.
    const cached = profile && st.fullCache.get(profile.name);
    if (cached != null) {
      pre.textContent = cached;
      return;
    }
    pre.textContent = profile ? "loading…" : "";
    if (!profile || st.fullPending) return;
    st.fullPending = true;
    fetch(`/api/engines/${encodeURIComponent(st.id)}/profiles/${encodeURIComponent(profile.name)}`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(
        (data) => {
          st.fullCache.set(profile.name, data.text);
          st.fullPending = false;
          if (st.mode === "full" && st.selected === profile.name) showViewer(currentProfile());
        },
        () => {
          st.fullPending = false;
          if (st.mode === "full" && st.selected === profile.name) {
            pre.textContent = "";
            viewNote.hidden = false;
            viewNote.textContent = "Could not read this profile.";
          }
        },
      );
  }

  function selectRow(name) {
    st.selected = name;
    for (const tr of tbody.children) tr.classList.toggle("sel", tr.dataset.name === name);
    showViewer(currentProfile());
  }

  function renderRows() {
    const list = st.profiles || [];
    const active = list.find((p) => p.active) || null;
    tbody.textContent = "";
    for (const p of list) {
      const tr = el("tr", "", tbody);
      tr.dataset.name = p.name;
      tr.tabIndex = 0;
      el("td", "name", tr).textContent = p.name;
      const flagTd = el("td", "", tr);
      if (p.active) el("span", "badge ok", flagTd).textContent = "ACTIVE";
      el("td", "mono", tr).textContent = p.sha256_12 || "—";
      el("td", "", tr).textContent = fmtSize(p.size);
      el("td", "", tr).textContent = fmtModified(p.modified);
      const noteTd = el("td", "muted", tr);
      if (!p.active && active && p.sha256_12 && p.sha256_12 === active.sha256_12)
        noteTd.textContent = "identical to active";
      const swTd = el("td", "", tr);
      if (!p.active) {
        const btn = el("button", "sw-btn", swTd);
        btn.type = "button";
        btn.dataset.role = "preview";
        btn.textContent = "Preview switch";
        btn.addEventListener("click", (ev) => {
          ev.stopPropagation();
          openPreview(p.name);
        });
      }
      tr.addEventListener("click", () => selectRow(p.name));
      tr.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          selectRow(p.name);
        }
      });
      if (p.name === st.selected) tr.classList.add("sel");
    }
    if (st.selected && !list.some((p) => p.name === st.selected)) {
      st.selected = null;
      view.hidden = true;
    }
    if (st.selected) showViewer(currentProfile());
    syncLocks();
  }

  function renderProfiles() {
    st.profilesFetching = false;
    roNote.hidden = st.profilesFailed || !st.profiles || st.profiles.length === 0;
    if (st.profilesFailed) {
      table.hidden = true;
      tableWrap.hidden = false;
      tableNote.hidden = false;
      tableNote.textContent = "Profile list unavailable for this engine.";
      view.hidden = true;
      return;
    }
    if (!st.profiles) return;
    tableNote.hidden = true;
    if (st.profiles.length === 0) {
      table.hidden = true;
      tableWrap.hidden = false;
      tableNote.hidden = false;
      tableNote.textContent = "No profile files in this engine's profile directory.";
      view.hidden = true;
      return;
    }
    table.hidden = false;
    tableWrap.hidden = false;
    renderRows();
  }

  function fetchProfiles() {
    fetch(`/api/engines/${encodeURIComponent(st.id)}/profiles`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(
        (data) => {
          st.profiles = Array.isArray(data && data.profiles) ? data.profiles : [];
          st.profilesFailed = false;
          renderProfiles();
        },
        () => {
          st.profilesFailed = true;
          renderProfiles();
        },
      );
  }

  /* ---------------------------------------------------------- switching */

  function setDryRun() {
    st.dryRun = true;
    dryBadge.hidden = false;
  }

  function showWarn(msg) {
    outcome.className = "op-result sw-result warn";
    outcome.textContent = msg;
    outcome.hidden = false;
  }

  function showOutcome(entry) {
    const b = outcomeBanner(entry || {});
    outcome.className = `op-result sw-result${b.cls ? ` ${b.cls}` : ""}`;
    outcome.textContent = b.text;
    outcome.hidden = false;
    if (entry && entry.dry_run) setDryRun();
  }

  /* Why preview / apply / restore are disabled right now (null = enabled).
   * Any switch POST anywhere on the page locks every button (item 7); the
   * pure helper folds the preview verdict into the apply column. */
  function currentReasons() {
    const d = st.previewData;
    return buttonStates({
      workerActive,
      switchRunning: st.switchRunning || postsInFlight > 0,
      valid: d ? d.valid !== false : true,
      changeCount: d && Array.isArray(d.changes) ? d.changes.length : 1,
    });
  }

  function syncLocks() {
    const reasons = currentReasons();
    workerNote.hidden = !(workerActive && st.hasProfiles);
    for (const b of card.querySelectorAll(".sw-btn")) {
      const reason = reasons[b.dataset.role] || null;
      b.disabled = reason !== null;
      b.title = reason || "";
    }
  }
  st.syncLocks = syncLocks;

  function renderPreview() {
    preview.hidden = !st.previewName;
    if (!st.previewName) return;
    pvTitle.textContent = st.previewFetching
      ? `Preview of ${st.previewName}…`
      : `Preview: switch to ${st.previewName}`;
    pvRestart.hidden = st.previewFetching;
    pvErrors.hidden = true;
    pvInvalid.hidden = true;
    pvWarns.hidden = true;
    pvQs.hidden = true;
    pvZero.hidden = true;
    pvTable.hidden = true;
    pvBody.textContent = "";
    const d = st.previewData;
    if (!d) return;
    const changes = Array.isArray(d.changes) ? d.changes : [];
    const errors = Array.isArray(d.errors) ? d.errors : [];
    const warnings = Array.isArray(d.warnings) ? d.warnings : [];
    if (errors.length) {
      pvErrors.hidden = false;
      pvErrors.textContent = errors.join("\n");
    }
    if (warnings.length) {
      pvWarns.hidden = false;
      pvWarns.textContent = warnings.join("\n");
    }
    if (d.valid === false) pvInvalid.hidden = false;
    if (d.valid !== false && changes.length === 0) pvZero.hidden = false;
    if (changes.length) {
      pvTable.hidden = false;
      for (const c of changes) {
        const tr = el("tr", c.quality_sensitive ? "sw-qs" : "", pvBody);
        const settingTd = el("td", "", tr);
        settingTd.textContent = String(c.path ?? "").split(".").pop() || "—";
        settingTd.title = String(c.path ?? "");
        el("td", "mono", tr).textContent = String(c.from ?? "—");
        el("td", "mono", tr).textContent = String(c.to ?? "—");
        el("td", "muted", tr).textContent = c.unit || "—";
      }
      pvQs.hidden = !changes.some((c) => c.quality_sensitive);
    }
  }

  function clearPreview() {
    st.previewName = null;
    st.previewData = null;
    st.previewFetching = false;
    renderPreview();
  }

  function openPreview(name) {
    const reason = currentReasons().preview;
    if (reason) return showWarn(`refused: preview — ${reason}`);
    st.previewName = name;
    st.previewData = null;
    st.previewFetching = true;
    outcome.hidden = true;
    renderProgress(null); // a new Preview starts: drop the last switch's steps
    renderPreview();
    syncLocks();
    fetch(`/api/engines/${encodeURIComponent(id)}/profiles/${encodeURIComponent(name)}/preview`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(
        (d) => {
          st.previewFetching = false;
          st.previewData = d;
          if (st.previewName === name) renderPreview();
          syncLocks();
        },
        () => {
          st.previewFetching = false;
          st.previewData = { valid: false, errors: [`Could not read the preview for ${name}.`], warnings: [], changes: [] };
          if (st.previewName === name) renderPreview();
          syncLocks();
        },
      );
  }

  /* `final` renders a finished switch's journal steps: the note changes and
   * the list stays up until the next Preview/Apply/Restore replaces it, so a
   * rollback still shows which step failed after the banner appeared. */
  function renderProgress(rows, final = false) {
    if (!rows) {
      progress.hidden = true;
      stepsHost.textContent = "";
      return;
    }
    progress.hidden = false;
    progressNote.textContent = final
      ? "Steps (from the journal)"
      : "Profile switch in progress — following the journal\u2026";
    stepsHost.textContent = "";
    for (const r of rows) {
      const row = el("div", "sw-step", stepsHost);
      const setStep = lamp(el("div", "", row));
      setStep(r.state === "done" ? "ok" : r.state === "current" ? "busy" : "error", r.step);
      const det = detailText(r.detail);
      if (det) el("span", "sw-step-detail", row).textContent = det;
      if (r.error) el("span", "sw-step-error", row).textContent = r.error;
    }
  }

  function pollSwitch() {
    fetch(`/api/engines/${encodeURIComponent(id)}/switch`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(
        (d) => {
          const running = !!(d && d.running);
          const entry = (d && d.entry) || null;
          if (entry && entry.dry_run) setDryRun();
          const was = st.switchRunning;
          st.switchRunning = running;
          if (running) {
            outcome.hidden = true;
            renderProgress(stepListModel((entry && entry.steps) || [], true));
          } else if (was) {
            if (st.inFlight) {
              // our own POST answers next and renders the final step list
              renderProgress(null);
            } else {
              // the switch ended while this page was not watching
              if (entry && entry.outcome) showOutcome(entry);
              const steps = entry && Array.isArray(entry.steps) ? stepListModel(entry.steps, false) : [];
              renderProgress(steps.length ? steps : null, true);
              clearPreview();
              refreshAfterSwitch();
            }
          }
          syncLocks();
        },
        () => {},
      );
  }

  // Follow a running switch every 2 s — only while one is actually going on.
  const swTimer = setInterval(() => {
    if (cards.get(id) !== st) {
      clearInterval(swTimer);
      return;
    }
    if (st.switchRunning || st.inFlight) pollSwitch();
  }, 2000);

  function refreshAfterSwitch() {
    st.profiles = null;
    st.profilesFailed = false;
    st.profilesFetching = false;
    st.fullCache = new Map();
    if (st.hasProfiles) {
      st.profilesFetching = true;
      fetchProfiles();
      fetchHistory();
    }
  }

  function startSwitch(kind, target, text) {
    const reasons = currentReasons();
    const reason = kind === "apply" ? reasons.apply : reasons.restore;
    if (reason) return showWarn(`refused: ${kind} — ${reason}`);
    st.pending = { kind, target };
    confirmText.textContent = text;
    confirmBox.hidden = false;
    outcome.hidden = true;
    syncLocks();
  }

  applyBtn.addEventListener("click", () => {
    if (!st.previewName) return;
    startSwitch("apply", st.previewName, applyConfirmText(st.previewName));
  });

  yesBtn.addEventListener("click", () => {
    const p = st.pending;
    if (!p) return;
    // re-check the lock at confirm time, exactly like control.js
    const reasons = currentReasons();
    const reason = p.kind === "apply" ? reasons.apply : reasons.restore;
    st.pending = null;
    confirmBox.hidden = true;
    if (reason) return showWarn(`refused: ${p.kind} — ${reason}`);
    runSwitch(p);
  });

  noBtn.addEventListener("click", () => {
    st.pending = null;
    confirmBox.hidden = true;
    syncLocks();
  });

  async function runSwitch(p) {
    const url =
      p.kind === "apply"
        ? `/api/engines/${encodeURIComponent(id)}/profiles/${encodeURIComponent(p.target)}/apply`
        : `/api/engines/${encodeURIComponent(id)}/history/${encodeURIComponent(p.target)}/restore`;
    st.inFlight = true;
    postsInFlight += 1;
    st.switchRunning = true;
    outcome.hidden = true;
    renderProgress(stepListModel([], true));
    syncAll();
    pollSwitch();
    let finalRows = null;
    try {
      const entry = await postSwitch(url);
      if (entry && typeof entry === "object" && entry.outcome) showOutcome(entry);
      else showWarn("The switch request came back without an answer.");
      // The journal's final steps stay on screen under the banner: after a
      // rollback the page must still show which step failed and that the
      // rollback and restore ran. Only a missing entry hides the list.
      const steps = entry && Array.isArray(entry.steps) ? stepListModel(entry.steps, false) : [];
      if (steps.length) finalRows = steps;
    } catch {
      showOutcome({ outcome: "refused", errors: ["Could not deliver the switch request to the dashboard server."] });
    } finally {
      st.inFlight = false;
      postsInFlight -= 1;
      st.switchRunning = false;
      renderProgress(finalRows, true);
      clearPreview();
      refreshAfterSwitch();
      syncAll();
    }
  }

  function fetchHistory() {
    if (st.historyFetching) return;
    st.historyFetching = true;
    fetch(`/api/engines/${encodeURIComponent(id)}/history`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(
        (d) => {
          st.historyFetching = false;
          st.history = Array.isArray(d && d.entries) ? d.entries : [];
          if (Array.isArray(d && d.warnings) && d.warnings.length) {
            histWarn.hidden = false;
            histWarn.textContent = d.warnings.join("\n");
          }
          if (st.history.some((e) => e && e.dry_run)) setDryRun();
          renderHistory();
        },
        () => {
          st.historyFetching = false;
        },
      );
  }

  function renderHistory() {
    if (!st.history) return;
    histEl.hidden = false;
    histTable.hidden = st.history.length === 0;
    histNote.hidden = st.history.length > 0;
    histBody.textContent = "";
    // The journal is newest first and stays that way; never re-sort it.
    for (const e of st.history) {
      const row = formatHistoryRow(e);
      const tr = el("tr", "", histBody);
      el("td", "", tr).textContent = row.started;
      el("td", "", tr).textContent = row.action;
      el("td", "name", tr).textContent = row.change;
      const outTd = el("td", "", tr);
      if (row.outcomeCls) el("span", `badge ${row.outcomeCls}`, outTd).textContent = row.outcome;
      else outTd.textContent = row.outcome;
      const backupTd = el("td", "mono", tr);
      backupTd.textContent = row.backup;
      if (row.backup !== "—") backupTd.title = row.backup;
      const actTd = el("td", "", tr);
      if (row.canRestore) {
        const btn = el("button", "sw-btn", actTd);
        btn.type = "button";
        btn.dataset.role = "restore";
        btn.textContent = "Restore…";
        btn.addEventListener("click", () =>
          startSwitch("restore", e.id, restoreConfirmText((e.from && e.from.name) || row.backup, e.started)),
        );
      }
    }
    syncLocks();
  }

  st.update = (e) => {
    setLamp(LAMP_FOR[e.status] || "off", e.status == null ? "—" : e.status);
    labelEl.textContent = e.label || id;
    typeBadge.textContent = e.type || "—";
    urlDd.textContent = e.base_url || "—";
    modelDd.textContent = e.model || "—";

    const caps = e.capabilities || {};
    const capKey = JSON.stringify(caps);
    if (capKey !== st.capKey) {
      st.capKey = capKey;
      chips.textContent = "";
      for (const [key, on] of Object.entries(caps)) {
        const chip = el("span", `chip ${on ? "on" : "off"}`, chips);
        chip.textContent = on ? CAP_LABELS[key] || key : `${CAP_LABELS[key] || key} · not exposed`;
      }
    }

    st.hasProfiles = !!e.has_profiles;
    if (e.has_profiles) {
      if (!st.profiles && !st.profilesFailed && !st.profilesFetching) {
        st.profilesFetching = true;
        fetchProfiles();
      }
      if (!st.history && !st.historyFetching) fetchHistory();
      if (!st.switchChecked) {
        st.switchChecked = true;
        pollSwitch(); // catch a switch that started before this page loaded
      }
    }
    if (!e.has_profiles) {
      tableWrap.hidden = true;
      view.hidden = true;
    }
    syncLocks();
  };

  return st;
}

/* ---------------------------------------------------------------------- poll */

const cards = new Map(); // engine id -> card state
let banner = null; // shown only while /api/engines is down or empty

function setBanner(msg) {
  if (!msg) {
    if (banner) banner.hidden = true;
    return;
  }
  if (!banner) banner = el("div", "card span12", page);
  banner.textContent = msg;
  banner.hidden = false;
  page.insertBefore(banner, page.firstChild);
}

function renderAll(data) {
  if (!data || !Array.isArray(data.engines)) {
    setBanner("Could not reach /api/engines — the dashboard server is not answering.");
    return;
  }
  if (data.engines.length === 0) {
    setBanner("No engines are configured.");
    return;
  }
  setBanner(null);
  const seen = new Set();
  for (const e of data.engines) {
    if (!e || typeof e.id !== "string") continue;
    seen.add(e.id);
    let st = cards.get(e.id);
    if (!st) {
      st = makeCard(e.id);
      cards.set(e.id, st);
    }
    st.update(e);
  }
  for (const [id, st] of cards) {
    if (!seen.has(id)) {
      st.card.remove();
      cards.delete(id);
    }
  }
}

let inFlight = false;
function tick() {
  if (inFlight) return;
  inFlight = true;
  fetch("/api/engines")
    .then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    })
    .then(
      (data) => {
        try {
          renderAll(data);
        } catch (err) {
          console.error("/api/engines: render failed", err);
        }
      },
      () => renderAll(null),
    )
    .finally(() => {
      inFlight = false;
    });
}

tick();
setInterval(tick, 5000);
pollWorker();
setInterval(pollWorker, 5000);

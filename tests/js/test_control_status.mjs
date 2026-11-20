import test from "node:test";
import assert from "node:assert/strict";

// control.js wires the page at import time, so give the module top level just
// enough of a fake page (kit.js only touches the DOM inside its functions).
const el = () => ({
  classList: { add() {}, remove() {} },
  style: {}, dataset: {}, hidden: false, className: "", textContent: "",
  scrollTop: 0, clientHeight: 0, scrollHeight: 0,
  setAttribute() {}, appendChild() {}, addEventListener() {},
});
globalThis.document = {
  getElementById: el,
  querySelector: (sel) => (sel.startsWith("button#") ? el() : null),
  createElement: el, createElementNS: el, documentElement: el(),
};
globalThis.setInterval = () => 0; // keep the 5 s status poll from holding node open

const { statusView, formatPorts, formatRestart, uptime } = await import("../../web/control.js");

// Fixture: exactly what the live helper (radiance-ctl status) returns —
// Docker inspect data (see wsd/lifecycle/control.py docstring, T-022 packet).
const LIVE_STATUS = {
  ok: true,
  op: "status",
  name: "/radiance",
  image: "radiance:local",
  state: {
    Status: "running",
    Running: true,
    StartedAt: "2026-11-18T08:41:07.123456789Z",
    FinishedAt: "0001-01-01T00:00:00Z",
    ExitCode: 0,
    Health: { Status: "healthy" },
  },
  network_mode: "host",
  ports: { "8000/tcp": [{ HostIp: "127.0.0.1", HostPort: "8000" }] },
  restart_policy: { Name: "unless-stopped", MaximumRetryCount: 0 },
};

// Fixture: the dry-run canned status in wsd/lifecycle/control.py (untouched).
const DRYRUN_STATUS = {
  ok: true, op: "status", dry_run: true,
  name: "/radiance-dryrun", image: "radiance:local",
  state: "running", started_at: "2026-11-18T08:41:07Z",
  network_mode: "host", ports: "", restart_policy: "unless-stopped",
};

test("live Docker inspect shape is read for state, start time, ports, restart", () => {
  const view = statusView(LIVE_STATUS);
  assert.equal(view.state, "running");
  assert.equal(view.started, "2026-11-18 08:41:07 UTC");
  assert.equal(view.ports, "127.0.0.1:8000 -> 8000/tcp");
  assert.equal(view.restart, "unless-stopped");
  assert.equal(view.startedIso, "2026-11-18T08:41:07.123456789Z");
});

test("dry-run canned shape keeps working", () => {
  const view = statusView(DRYRUN_STATUS);
  assert.equal(view.state, "running");
  assert.equal(view.started, "2026-11-18 08:41:07 UTC");
  assert.equal(view.ports, "");
  assert.equal(view.restart, "unless-stopped");
});

test("Docker zero time means never started: no start time, no uptime", () => {
  const never = {
    ...LIVE_STATUS,
    state: { Status: "created", Running: false, StartedAt: "0001-01-01T00:00:00Z" },
    ports: {},
  };
  const view = statusView(never);
  assert.equal(view.state, "created");
  assert.equal(view.started, "");
  assert.equal(view.startedIso, null);
  assert.equal(view.ports, "");
  assert.equal(uptime(view.startedIso), null);
});

test("state falls back to Running, and a stopped exit reports exited", () => {
  assert.equal(statusView({ state: { Running: true } }).state, "running");
  const exited = {
    ...LIVE_STATUS,
    state: { Status: "exited", Running: false, StartedAt: "0001-01-01T00:00:00Z", ExitCode: 137 },
    ports: {},
  };
  assert.equal(statusView(exited).state, "exited");
});

test("ports format one arrow per binding, unbound host as 0.0.0.0", () => {
  assert.equal(
    formatPorts({
      "8000/tcp": [{ HostIp: "127.0.0.1", HostPort: "8000" }, { HostIp: "", HostPort: "18000" }],
      "443/tcp": [{ HostIp: "0.0.0.0", HostPort: "8443" }],
    }),
    "127.0.0.1:8000 -> 8000/tcp, 0.0.0.0:18000 -> 8000/tcp, 0.0.0.0:8443 -> 443/tcp",
  );
  assert.equal(formatPorts(""), "");
  assert.equal(formatPorts(undefined), "");
  assert.equal(formatRestart({ Name: "on-failure", MaximumRetryCount: 3 }), "on-failure");
  assert.equal(formatRestart(undefined), "");
});

test("uptime counts from a real StartedAt and rejects junk", () => {
  const ago = new Date(Date.now() - (2 * 3600 + 61) * 1000).toISOString();
  assert.match(uptime(ago), /^2h 1m$/);
  assert.equal(uptime("not a date"), null);
});

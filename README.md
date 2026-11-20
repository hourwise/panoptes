# PANOPTES — AI workstation control centre

Panoptes is a local-only web dashboard for a self-hosted AI development
workstation. One small Python server (standard library only, no build step, no
CDN) collects hardware telemetry (CPU, memory, storage, GPU, PCIe), polls one
or more local inference engines (model, throughput, latency, queueing, KV
cache, speculative decoding), reads the activity of local agent harnesses
(tokens, compactions, tool calls — counts only, never message text), and shows
the supervisor's job history with per-task cost and energy. State-changing
actions are deliberately rare, confirmed twice and guarded; everything else is
strictly read-only. The server binds to the loopback interface only and never
talks to anything outside the machine.

Version 0.6.0. Python package `wsd`, product name Panoptes.

The two marked sections below are the bounded summary that the PCGsoft portfolio
sync reads; everything after them is the full documentation for a person
installing this. What may and may not go in them, and when they are reviewed, is
in [docs/PROJECT_MANIFEST.md](docs/PROJECT_MANIFEST.md).

<!-- pcgsoft:overview:start -->
Panoptes is a local, browser-based control centre for a self-hosted AI
workstation. One small Python server serves a plain HTML/CSS/JavaScript
dashboard; it binds to the loopback interface only, and nothing it reads leaves
the machine. There is no hosted instance and no account.

What it does today:

- **Hardware** — CPU load and clocks, memory and swap, storage I/O, and per-GPU
  utilisation, temperature, power, clocks and the PCIe link path each card is on
  (amdgpu sysfs, `nvidia-smi` where available).
- **Inference** — one card per configured engine behind per-type adapters
  (Radiance, llama.cpp and OpenAI-compatible): model and state, decode versus
  effective throughput, latency histograms, queue depth, speculative decoding,
  KV cache and host↔GPU traffic, each shown only when the engine advertises it.
- **Engine profiles** — preview and validate a candidate profile against the
  active one, switch only after an explicit confirm, watch the restart step by
  step, and get automatic rollback to the previous profile if the engine does
  not come back serving the new one. Only approved parameters can change, the
  model identity is locked, every switch is journalled and can be restored by
  hand.
- **Container controls** — status, logs, start, stop and restart of the
  configured engine, each confirmed twice and refused by a root helper while a
  supervised task is running or requests are in flight.
- **Agent telemetry** — live worker state from a local agent harness (OMP today:
  tokens, compactions and tool-call counts, never message text) plus job
  history with duration, tokens, cost and energy per task, every value tagged
  with its provenance and exportable as a redacted Markdown report.
- **Lab** — read-only comparison of one benchmark variant against another with
  run-to-run noise handled explicitly, per-task trends with configuration-change
  markers, and Markdown report export (shareable or private).
- **Honest data** — a reading the machine does not expose is reported as
  unavailable, never as zero, and derived figures are marked as calculated.

Linux only. Python 3.12 standard library for the backend; plain HTML, CSS and
JavaScript with no build step, no npm and no CDN; tests on `unittest` and Node's
built-in test runner; MIT licensed.
<!-- pcgsoft:overview:end -->

<!-- pcgsoft:status-notes:start -->
**Status: active development.** Everything the overview describes is
implemented in the current source tree, on Linux, and is used day to day
on the workstation it watches. The data path is the mature part: readings are
either real or marked unavailable. Engine coverage and UI polish are where the
rough edges are. Panoptes has no hosted site and no public URL — it runs on the
machine it monitors and listens on the loopback interface only, so there is
nothing to sign up to or try out remotely.

**Roadmap — not implemented, and not to be described as available:**

- Panoptes Guardian: hardware protection and emergency stop built on the
  interlocks the container controls already use;
- multi-GPU discovery on headless servers;
- Windows and macOS support;
- agent harnesses beyond OMP.

Deliberately out of scope, not merely unfinished: any telemetry out of the
machine, a build step or third-party front-end dependencies, and any
automation that publishes — a release is scanned, exported and published by
a person following the release guide.
<!-- pcgsoft:status-notes:end -->

## Screenshots

Not included yet — screenshots are planned with a later milestone; no image
files are referenced. The tabs are described in the feature list below.

## Features, per tab

- **Hardware** — CPU load and clocks, memory and swap, storage I/O, GPU
  utilisation, temperature, power and clocks (amdgpu sysfs; nvidia-smi where
  available), and the PCIe path of each GPU. Polled every 2 s
  (`/api/system`, `/api/gpus`) and 5 s (`/api/storage`). A `null` from the API
  renders as "not exposed", never as a zero.
- **Inference** — one section per configured engine: model and state, decode
  vs. effective request throughput, latency histograms since engine start,
  active and queued requests, speculative decoding, host↔GPU traffic and
  warnings. A section is drawn only when the engine advertises the capability;
  otherwise the UI says so explicitly.
- **Engines** — per engine: lamp, base URL, model id, capability chips and,
  where configured, a profile table ("Diff vs active", "Full file"). A
  profile can be previewed against the active one, then applied after an
  in-page confirm and watched live step by step; if the restarted engine
  fails to come up serving the new profile, the previous one is restored
  automatically. The history table lists every switch with a manual Restore.
  Only approved parameters change (strict parser, model identity locked),
  quality-sensitive changes are flagged, candidate files are never modified.
- **Agents** — a live workers card (which harness is running, working/idle,
  context usage, elapsed time, tokens, compactions and tool calls — failed
  tool calls and aborted requests counted under their real names, because
  the harness keeps no retry record) and the supervisor's job history: one
  row per finished task with duration, tokens, cost and energy, each value
  tagged with its provenance (measured / calculated / partial / unavailable).
  Each job can be downloaded as a redacted Markdown report.
- **Lab** — read-only comparison of one benchmark variant against another
  from the supervisor's records: only tasks both variants ran are scored,
  differences inside the run-to-run spread are marked as not distinguishable
  rather than coloured, and a side that never measured reads "no data".
  Experiment reports are downloadable from the same page.
- **Control** — container lifecycle for the configured engine: status, logs,
  start, stop, restart. Every action needs an explicit in-page confirm, goes
  out as a POST with a per-launch token, and is refused by the root helper
  while a supervised worker task is active or requests are in flight. A
  dry-run mode (`PANOPTES_CONTROL_DRYRUN=1`) exercises the UI without ever
  calling sudo.

## Design principles

- **Local-only.** The server binds `127.0.0.1` only. Engine base URLs are
  validated to be loopback; a non-loopback entry is skipped with a warning.
  Nothing leaves the machine.
- **Read-only by default.** Browsing the dashboard reads sensors, engine
  status endpoints and local files, and changes nothing. State-changing
  endpoints exist only under Control (container start/stop/restart) and
  Engines (profile apply and history restore); all are POST-only, need the
  per-launch token and an explicit body confirmation, and restart Radiance
  only through the same guarded root helper Control uses.
- **Engine, model and harness agnostic.** Engines sit behind per-type
  adapters (`radiance`, `openai-compatible`, `llama.cpp`) that all return one
  normalised snapshot plus a `capabilities` map; harness telemetry behind
  harness adapters (`omp` today). Models are discovered from the engine, never
  hard-coded. `config/panoptes.example.toml` is a commented example of the
  format, documented in [docs/INSTALL.md](docs/INSTALL.md).
- **Honest data with provenance.** Missing values stay missing (`None`,
  rendered as an em dash or "not exposed") — never guessed or zero-filled.
  Derived figures are marked as calculated, sampled ones as measured, and
  incomplete ones as partial. Downloaded reports redact home-directory paths
  and secret assignments, and state how many redactions were made.

## Quick start

```sh
git clone <panoptes-repository-url> && cd panoptes   # 1. get the code
python3 -m wsd                                       # 2. start the server (prints its URL)
xdg-open http://127.0.0.1:8765/                      # 3. open it in your browser
```

The server runs until Ctrl-C and prints `http://127.0.0.1:<port>` on startup
(`--port` to change it). Copy `config/panoptes.example.toml` to
`config/panoptes.toml` and edit it to point Panoptes at your own engines,
harnesses and job records; with the example left untouched on another machine,
those tabs simply show nothing rather than failing.

A published tree — a directory written by `tools/export_public.py`, or an
archive of one — needs nothing fetched: it is complete, and the tests, the
smoke test and the release scan all run inside it exactly as they do here.

## Documentation

- [docs/INSTALL.md](docs/INSTALL.md) — requirements, configuration, the
  environment variables, running, the desktop launcher, the optional root
  components, and how to run the tests.
- [docs/RELEASE.md](docs/RELEASE.md) — the release scan, building a clean
  export, validating it as a stranger would, and the publishing checklist.
- [docs/ROLLBACK.md](docs/ROLLBACK.md) — how to undo every change this project
  can make to a machine, with a check for each.
- [docs/PROJECT_MANIFEST.md](docs/PROJECT_MANIFEST.md) — what
  `.pcgsoft/project.yml` is, when the manifest and the two marked sections above
  are reviewed, and what may never appear in them.
- [docs/SECURITY.md](docs/SECURITY.md) — threat model, the POST protections
  (container control and profile switching), the root helper and its
  interlock, the CPU power publisher, and what Panoptes never sends anywhere.

## Status

Everything the feature list above describes is in this tree and runs: hardware
and inference monitoring, engine profiles with confirmed switching and automatic
rollback, agent history with live harness telemetry and redacted reports, the
guarded container controls, the optimisation lab, the CPU power publisher and
the desktop launcher. The optional root components install from `ops/`
([docs/INSTALL.md](docs/INSTALL.md) covers each, with its selftest).

What is not here yet is the roadmap, listed once in the `pcgsoft:status-notes`
section at the top; the same section names what is out of scope by design rather
than merely unfinished. [docs/RELEASE.md](docs/RELEASE.md) is the release
procedure a human runs.

## Contributing

Issues and pull requests are welcome. The rules that keep this project small:
the backend is Python 3.12 **standard library only** (no pip installs), the UI
is plain HTML/CSS/JavaScript with **no build step, no npm and no CDN**, and the
server binds to the loopback interface only.

```sh
python3 -m unittest discover -s tests -q   # Python suite
node --test "tests/js/*.mjs"               # pure-JS tests (optional: needs Node)
python3 tools/smoke.py                     # browser smoke test (needs headless Chrome)
python3 tools/release_scan.py              # no personal paths, host names or secrets
```

`tools/release_scan.py` checks the shipped files for machine-specific values; a
change that makes it report anything is not ready to publish.

`tools/export_public.py` writes the clean public tree from that product set —
`MANIFEST.sha256` included, private material and caches left behind — and
[docs/RELEASE.md](docs/RELEASE.md) is the checklist around a release: scan,
export, validate the export from scratch, publish by hand.

## Licence

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 PCGsoft.

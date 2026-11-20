# Installing Panoptes

Audience: a technically capable reader who has never seen this project. Every
command below either exists in this repository or is a standard tool; optional
steps are marked as optional.

## Table of contents

1. [Requirements](#requirements)
2. [Getting the code](#getting-the-code)
3. [Configuration](#configuration)
4. [Running the server](#running-the-server)
5. [Environment variables](#environment-variables)
6. [Desktop launcher (optional)](#desktop-launcher-optional)
7. [Optional: radiance-ctl, the engine lifecycle helper](#optional-radiance-ctl-the-engine-lifecycle-helper)
8. [Optional: panoptes-cpu-power, the CPU power publisher](#optional-panoptes-cpu-power-the-cpu-power-publisher)
9. [Optional: serving a second engine with llama.cpp](#optional-serving-a-second-engine-with-llamacpp)
10. [Running the tests](#running-the-tests)
11. [Building a release export](#building-a-release-export)

## Requirements

- Linux. The collectors read `/proc`, sysfs and hwmon paths; the optional
  components use systemd and sudo.
- Python 3.12 or newer, standard library only. There are **no pip
  dependencies**, no build step, no npm and no CDN — `web/` is plain HTML, CSS
  and JavaScript served straight from disk. Check with `python3 --version`.
- Any modern browser.
- Optional extras: Node.js (only for the JavaScript unit tests in `tests/js/`),
  headless Google Chrome (only for `tools/smoke.py`), Docker (only for
  radiance-ctl), systemd (only for the CPU power publisher).

## Getting the code

```sh
git clone <panoptes-repository-url> && cd panoptes
```

Without git, use the published tree: a directory written by
`tools/export_public.py` (or an archive of one, `MANIFEST.sha256` included).
Copy or unpack it wherever you like — there is nothing to fetch and nothing to
compile, and every command in this file, including the test suite, runs inside
it. A plain directory copied off another machine works the same way.

The layout: `wsd/` the Python server, `web/` the browser app,
`config/panoptes.example.toml` the commented configuration example, `ops/` the
optional privileged helpers and the launcher, `tools/smoke.py` the browser smoke
test, `tools/release_scan.py` the publish check, `tools/export_public.py` the
release export, `docs/` these guides, `tests/` the test suite.

## Configuration

One file: `config/panoptes.toml` (parsed with the stdlib `tomllib`). It is not
in the repository — copy the commented example and edit that copy:

```sh
cp config/panoptes.example.toml config/panoptes.toml   # then edit it
```

The server reads `$PANOPTES_CONFIG` when it is set, otherwise
`config/panoptes.toml` next to the repository root; there is no other place
machine-specific values come from. Treat every path, port and label in the
example as a value to change, not a default to keep.

With no configuration file at all — and with a file that has no `[[engines]]`
section — the dashboard watches one built-in engine entry: a Radiance engine at
`http://127.0.0.1:8000`, labelled "Radiance · R9700". It shows offline when
nothing answers there. Any `[[engines]]` entry you write replaces that default;
the two are never merged.

These sections, all optional — a missing section makes the matching tab show
nothing rather than fail, and **any invalid entry is skipped with a warning,
never a crash**: the warnings are listed on the Agents tab, under Live workers.

- `[[engines]]` — one entry per inference engine. Required: a unique `id`, a
  known `type` (`radiance`, `openai-compatible` or `llama.cpp`) and an
  `http(s)` `base_url` whose host **must be loopback** (`localhost`, any
  `127.0.0.0/8` address or `::1`); a non-loopback host is rejected at load
  time. Optional: `label`, and `profiles_dir` (absolute path) with
  `active_profile` (a `*.yaml`/`*.yml` file inside it): the engine's config
  profiles, which the Engines tab lists, diffs and — after an explicit
  confirm — switches. Switching rewrites only the active profile file,
  atomically; the other profile files in the directory are never modified.
  `journal_dir` (absolute path) says where that engine's switch journal and
  backups live; without it they go to `config-journal` under the
  `[records] dir`, and an engine with neither can list and diff profiles
  but cannot switch.
  When that directory does not exist it is dropped with a warning, and the
  engine's card on the Engines tab then lists no profiles and offers no
  switching — an honest empty state, not an error.
- `[[harnesses]]` — one entry per agent harness. `type` is `omp` today.
  `lock_file` is a small file the supervisor writes as `TASK START_EPOCH PID`;
  it drives the working/idle lamp. `sessions_dir` must be an existing absolute
  directory of session `*.jsonl` files; Panoptes reads only counts (tokens,
  tool names, timestamps) from the newest file, never message text.
- `[records]` — `dir` points at a supervisor's records (`<dir>/tasks` packets,
  `<dir>/reviews`, `<dir>/perf/tasks.jsonl` job rows, `<dir>/perf/bench.jsonl`
  benchmark runs). Read-only: it feeds the Agents tab's job history, the
  downloadable job reports and the Lab tab's comparison table. Without it those
  are empty, and nothing else is affected.
- `[guardian]` — `state_file` is where the optional Guardian service writes its
  state file, which the header chip reads. The file must **not** exist yet for
  the setting to be good: only the shape of the path is checked, and without it
  the default is `$XDG_STATE_HOME` or `~/.local/state` under `panoptes/`.

The example uses placeholders of the form `/path/to/...` and
`/home/YOUR_USER/...`: every value in it is an example, not this machine's.
With the placeholders left as they are, each path is reported as missing at
start-up and the matching tab simply stays empty.

## Running the server

```sh
python3 -m wsd                 # default port 8765
python3 -m wsd --port 9000     # any other loopback port
```

It prints its URL (`http://127.0.0.1:<port>`) and serves until Ctrl-C. The
server binds `127.0.0.1` only — it is not reachable from other machines by
design, so there is no option to change the bind address.

## Environment variables

All optional; each is read once at start-up unless noted.

- `PANOPTES_CONFIG` — path of the TOML configuration file, taking effect
  when none is given programmatically (default `config/panoptes.toml`).
- `PANOPTES_CONTROL_DRYRUN=1` — container control answers every operation
  with canned results and never calls sudo, and a profile switch runs the
  full pipeline against a sandboxed copy of the profiles directory with a
  private journal and switch lock. Every answer carries `dry_run: true`.
- `PANOPTES_CONTROL_DRYRUN_LOCKED=1` — dry run only: report the supervised
  worker lock as held instead of reading the real lock file, so both lock
  states of the Control and Engines UIs can be exercised.
- `PANOPTES_READY_TIMEOUT_S` — seconds a profile switch waits for the
  restarted engine to be healthy and serving the new profile (default
  600); a non-positive or unparseable value falls back to the default.
- `PANOPTES_ROLLBACK_TIMEOUT_S` — the switch's separate budget for the
  rollback's own readiness wait after a failed switch (default 600):
  shortening the forward budget never shortens recovery.

## Desktop launcher (optional)

A per-user app-grid launcher that starts the server if it is not already
running and opens the dashboard in the browser. No root needed.

- Install: `ops/desktop/install-launcher.sh` — it fills
  `ops/desktop/panoptes.desktop.in` with absolute paths of *this* checkout and
  installs the result to `${XDG_DATA_HOME:-~/.local/share}/applications/panoptes.desktop`.
- Verify: a `panoptes.desktop` file appears there (the script runs
  `desktop-file-validate` if that tool is installed); the entry shows in your
  application menu after a logout/login or an `update-desktop-database` run.
  Activating it opens the dashboard; the server log is at
  `${XDG_STATE_HOME:-~/.local/state}/panoptes/server.log`.
- Remove: `ops/desktop/install-launcher.sh --uninstall`.

Re-run the installer after moving the checkout: the installed file references
the old absolute paths.

## Optional: radiance-ctl, the engine lifecycle helper

**What it does.** The Control tab needs to start/stop/restart the engine
container, which requires Docker. Instead of group membership or broad sudo,
`ops/radiance-ctl/install.sh` installs a 90-line helper that accepts exactly
five invocations — `status`, `start`, `stop`, `restart`, `logs` — for one
container name, verified to run one image prefix. It never runs
`docker run/exec/rm/pull`, takes no free-form arguments, and refuses `stop`
and `restart` while a supervised worker task holds its lock file or the engine
reports running/waiting/in-flight requests. Every call is appended to an audit
log at `/var/log/radiance-ctl.log`.

**Why it needs root.** The dashboard user must reach the Docker daemon and
install a sudoers rule; both are root-owned. The helper itself runs as root
via `sudo -n`, invoked by the server with a fixed enum — never a shell.

**Install — edit first, then run it as root.** Two files carry machine values,
and the installer refuses to install while any placeholder is still in them
(it names the ones it found, exits non-zero and copies nothing):

- `ops/radiance-ctl/radiance-ctl.sudoers` grants the passwordless rule to one
  username, shipped as a placeholder — put the dashboard user's login name there.
- The `INSTALL-TIME SETTINGS` block at the top of `ops/radiance-ctl/radiance-ctl`
  holds the container name, the image prefix and the worker-lock path: three
  placeholders, and the only machine values the helper reads.

Nothing is substituted for you: a root helper that names the wrong user or the
wrong container is a security problem, so a human makes both edits. Then:

```sh
sudo ops/radiance-ctl/install.sh
```

The installer validates before and after (bash syntax check, `visudo -cf` on
the new rule, `visudo -c` on the whole sudo config — removing the rule again
if the whole-config check fails), installs root-owned copies to
`/usr/local/sbin/radiance-ctl` and `/etc/sudoers.d/radiance-ctl`, creates the
audit log, and prints SHA-256 hashes of what it installed.
[ops/radiance-ctl/README.md](../ops/radiance-ctl/README.md) says what each
setting guards and why nothing else can reach the privileged copy.

**Verify:** `ops/radiance-ctl/selftest.sh` (run as the dashboard user, not
root). It confirms file ownership/modes, that `sudo -n ... status` works
without a password, that extra arguments, shell metacharacters, unlisted
operations, plain `docker` and environment passing are all refused, and that
`restart` is refused while a worker lock is held — without ever stopping the
engine. It exits non-zero if any check fails.

**Remove:** see [docs/ROLLBACK.md](ROLLBACK.md#2-radiance-ctl--the-sudoers-rule).

## Optional: panoptes-cpu-power, the CPU power publisher

**What it does.** A systemd service that reads the CPU RAPL energy counters
once per second and publishes a whole-watt average to the world-readable file
`/run/panoptes/cpu-power.json` (`{"ts": ..., "package_w": ..., "core_w": ...}`),
replaced atomically. The dashboard integrates it for energy-per-task figures.

**Why it needs root.** The raw RAPL counters are deliberately `0400 root`
because high-resolution power reads enable a side-channel attack
(CVE-2020-8694, "Platypus"). This service keeps them that way and publishes
only 1-second, whole-watt values — useful for energy accounting, far too
coarse for the attack. The unit file sandboxes everything else: no
capabilities, private network, read-only system, no home, no new privileges.
Machines without RAPL (e.g. AMD desktops) get `null` fields, not crashes.

**Install** (as root): `sudo ops/cpu-power/install.sh` — syntax-checks the
script, verifies the unit file with `systemd-analyze verify`, installs root
copies to `/usr/local/libexec/panoptes-cpu-power` and
`/etc/systemd/system/panoptes-cpu-power.service`, enables and starts the
service, and prints hashes and the service hardening summary.

**Verify:** `ops/cpu-power/selftest.sh` (as the dashboard user): service
active, files root-owned, the raw counter still mode 400 and unreadable by
your user, the published file readable and its timestamp advancing, values
whole watts in a sane range.

**Remove:** see [docs/ROLLBACK.md](ROLLBACK.md#3-panoptes-cpu-power-service).

## Optional: serving a second engine with llama.cpp

`ops/llama-4060/serve.sh` runs one GGUF model on a designated CUDA GPU, bound
to `127.0.0.1:8001`, as a second engine (the script pins one GPU via
`CUDA_VISIBLE_DEVICES` so it cannot disturb the main engine). Usage:

```sh
ops/llama-4060/serve.sh [model.gguf] [context]     # env KV=q8_0 quantises the KV cache
```

Defaults: a 20B GGUF path and 32768 context that belong to the author's
machine (example only). It expects a llama.cpp CUDA build at
`~/Applications/llama.cpp/build-cuda/bin/llama-server` (edit the `BIN=` line
if yours lives elsewhere) and runs in the foreground — Ctrl-C stops it. Add
the engine to `config/panoptes.toml` with `type = "llama.cpp"` and
`base_url = "http://127.0.0.1:8001"`, then check `curl -s
http://127.0.0.1:8001/health` answers and the Engines tab shows the card.

## Running the tests

```sh
python3 -m unittest discover -s tests -q   # the Python suite
node --test "tests/js/*.mjs"               # pure-JS logic tests (optional: needs Node)
python3 tools/smoke.py                     # browser smoke test (needs headless Chrome)
python3 tools/release_scan.py              # no personal paths, host names or secrets
```

`tools/release_scan.py` walks the files that ship as the product (`wsd/`,
`web/`, `tests/`, `tools/`, `ops/`, `docs/`, `.pcgsoft/`, `config/` except this
machine's own `config/panoptes.toml`, plus `README.md`, `LICENSE`, `.gitignore`
and `pyproject.toml` if it exists) and prints `file:line: kind` for home-directory
paths that are not marked placeholders, this machine's host name and `*.local`
names, email addresses, secret-looking assignments, serial-number-like values,
private project names read from the optional local list
`config/release_scan_private.txt`, the user name and host name of whoever runs
the scan as whole words, and paths into the private project-management tree. It
exits 0 when the tree is clean and
1 otherwise; intended examples are listed with a reason in
`tools/release_scan_allow.txt`, and an entry that no longer matches its line is
itself reported. Run it before publishing anything.

`tools/smoke.py` starts the dashboard on a free loopback port, loads every tab
in headless Chrome and fails on console errors, failed requests, overflow and
broken widgets; it stops the server again on exit. Exit code 0 means every
check passed; 2 means the run itself could not complete.

## Building a release export

```sh
python3 tools/export_public.py ../panoptes-export
```

That writes a clean public tree outside this repository — the product set only,
no version-control or project-management material, no caches, no symbolic links
and never this machine's `config/panoptes.toml` — followed by
`../panoptes-export/MANIFEST.sha256`, one `<sha256>  <path>` line per file, so
`sha256sum -c MANIFEST.sha256` verifies any copy of it. The destination must not
exist and must not be inside the repository. The export is then scanned, and a
finding leaves it in place with exit status 1 so you can see what leaked.
[RELEASE.md](RELEASE.md) is the whole procedure: scan, export, validate the
export the way a stranger would, and the checklist a human runs before anything
is published. Publishing itself — remote, commit, tag, push — is never
automated here.


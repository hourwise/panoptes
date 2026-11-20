# Releasing Panoptes

How a tree of this project becomes something somebody else installs. There are
two tools and one checklist; both tools work entirely on your own disk. Nothing
in this repository pushes, uploads, tags or opens a network connection:
publishing stays a human act, done by a person who has read section 5.

## Table of contents

| # | Step | Command |
|---|------|---------|
| [1](#1-green-first) | Tests and smoke test green | `python3 -m unittest discover -s tests -q` |
| [2](#2-the-release-scan) | Nothing personal in the product | `python3 tools/release_scan.py` |
| [3](#3-building-the-export) | Write the clean tree | `python3 tools/export_public.py DEST` |
| [4](#4-validating-an-export) | Use the export as a stranger would | section 4 |
| [5](#5-the-publishing-checklist) | The checklist, then publish by hand | read it |
| [6](#6-when-a-release-was-wrong) | Undo | links to the rollback guide |
| [7](#7-troubleshooting) | Failure messages | section 7 |

## What ships

`tools/export_public.py` decides this and nothing else does:

- the trees `wsd/`, `web/`, `tests/`, `tools/`, `ops/`, `docs/`, `.pcgsoft/`;
- `config/panoptes.example.toml` — the commented example, never the live
  `config/panoptes.toml` and never `config/release_scan_private.txt`;
- `README.md`, `LICENSE`, `.gitignore`.

Never: the project-management and development directories, version-control
metadata, assistant configuration directories, byte-code and test caches
(`__pycache__/`, `*.pyc`, `.pytest_cache/`), this machine's live configuration
and its private scan-name list, and no symbolic link at all (a linked file is
not copied, a linked directory is not walked, so an export can never reach
outside the repository).

The export is not a copy of your checkout with a few things deleted: it is built
from that explicit list, so something new you add to the repository appears in
the export only once it belongs to one of those product paths.

## 1. Green first

From the repository root:

```sh
python3 -m unittest discover -s tests -q   # the Python suite
node --test "tests/js/*.mjs"               # pure-JS logic tests (needs Node)
python3 tools/smoke.py                     # browser smoke test (needs headless Chrome)
```

All three exit 0. `tools/smoke.py` prints `SMOKE OK`, or a `FAIL:` line per
broken page; exit 1 means a page is broken, exit 2 means the run could not
complete (no browser, no server) and is not a verdict on the code.

## 2. The release scan

```sh
python3 tools/release_scan.py
```

It walks the product set — the same paths as section 3 — and prints
`file:line: kind` for each problem:

| kind | what it found |
|------|---------------|
| `home-path` | a `/home/<name>/` path that is not a labelled example placeholder (`YOUR_USER`, `user`, `alice`, `bob`) |
| `hostname` | this machine's host name, or a `*.local` name |
| `email` | an e-mail address on a domain that is not `localhost`, `example.com` or `example.org` |
| `secret` | an assignment that looks like a real credential |
| `serial` | a serial-number-shaped value (`serial = ` with 12+ alphanumeric characters) |
| `private-name` | a name listed in `config/release_scan_private.txt`, as a whole word, any case |
| `username` | the login name of whoever runs the scan, as a whole word, any case |
| `private-path` | a path into the private project-management tree inside a shipped file |

The names that belong to one particular machine are not written in the scanner,
which is itself part of the product. They sit in
`config/release_scan_private.txt`: one name per line, `#` starting a comment.
That list is local and optional — a public checkout has none — and the export
never copies it, so it cannot leak by being published. A scan with no list still
adds the login name of whoever runs it and this machine's host name, which is
what catches a stranger's own names in their own checkout. Add a name when a
project, account or host name of yours would be out of place in a public
repository, and quote those names nowhere else: not in code, not in tests, not
in the allow file, not in a document.

Exit 0 means clean (`CLEAN: N files scanned, 0 problems`); exit 1 prints every
finding first. Fix each one — placeholders, `example.com` addresses, named
variables instead of literal values. Where a line genuinely must stay (a test
that plants a bad value to prove a detector fires, a documented pattern),
add a line to `tools/release_scan_allow.txt`:

```
path/in/repo:LINE-a-substring-of-that-line  # why this line is allowed
```

An entry whose line no longer matches is itself reported (`unused-allow`), so
the file cannot rot into a list of excuses for problems that were fixed months
ago. Never put a real name, host or secret in the allow file: it is scanned too.

## 3. Building the export

```sh
python3 tools/export_public.py ../panoptes-export
```

- `DEST` must **not** exist and must be **outside** the repository; otherwise the
  tool says so and writes nothing at all (exit 2, along with a missing or extra
  argument).
- The product set is copied file by file, then `DEST/MANIFEST.sha256` is written:
  one `<sha256>  <relative path>` line per exported file, sorted by path. Check
  any copy of the tree against it with `sha256sum -c MANIFEST.sha256` from
  inside it.
- The scan of section 2 then runs **over the export**, using the copy of the
  allow file inside it. Findings are printed and the exit code is 1; `DEST` is
  left exactly as written so you can see what leaked.
- No version-control command is run and no socket is opened, before, during or
  after.

## 4. Validating an export

Do this in the export, not in the repository: it is the only step that tests
what a stranger actually receives. Read `README.md` and
[INSTALL.md](INSTALL.md) from there and follow them literally.

```sh
cd ../panoptes-export
python3 -m unittest discover -s tests -q
node --test "tests/js/*.mjs"
python3 tools/smoke.py
python3 tools/release_scan.py
sha256sum -c MANIFEST.sha256 | tail -n 1
```

Then start it twice on a port that is not 8765, with the control plane faked and
any state it might write kept inside the export, and open every tab in a browser:

```sh
XDG_STATE_HOME="$PWD/.validation-state" PANOPTES_CONTROL_DRYRUN=1 \
  python3 -m wsd --port 8791                 # 1: no configuration at all
```

```sh
cp config/panoptes.example.toml config/panoptes.toml
XDG_STATE_HOME="$PWD/.validation-state" PANOPTES_CONTROL_DRYRUN=1 \
  python3 -m wsd --port 8792                 # 2: the example configuration
```

For each tab "good" means a real value, or an honest empty state that names
what is missing and how to supply it — never a blank cell, a spinner, a stack
trace, or a number that was invented. And no errors in the browser's developer
console. What the two runs should show:

| Tab | No configuration | Example configuration |
|-----|------------------|-----------------------|
| Hardware | local CPU, memory, GPU, disk and sensor readings — this tab needs no configuration at all | the same readings: the example describes no hardware |
| Inference | the one built-in default engine (a Radiance at `http://127.0.0.1:8000`), `offline` unless something answers there | one card per `[[engines]]` entry, `ready` or `offline` by whether its loopback URL answers |
| Engines | that same card; no profiles and no switching offered | cards as above; the example's placeholder `profiles_dir` is dropped, so no profile is listed and no switch is offered |
| Agents | "No agent harnesses configured", naming the config key, and "no job records" | the configuration warnings, each naming the placeholder path that was skipped; still no live worker and no job records |
| Lab | an empty comparison table, with the note about where the records come from | the same table, plus the warning naming the placeholder records directory |
| Control | with `PANOPTES_CONTROL_DRYRUN=1` the faked helper and its fake log lines; without it, the helper reported as not installed and every button disabled | same: the helper is a separate install (see section "radiance-ctl" in INSTALL) |

The header's Guardian chip reads "Guardian · not installed" in both runs and
names the state path it looked for, and `demo.html` — the UI-kit demo page,
fixture data only — loads clean as well. `tools/smoke.py` checks all seven pages
for console errors, failed requests and layout faults. The states above are what
a machine with no engine and no records shows; a machine that does run an engine
will legitimately show live numbers where this table says `offline`.

Stop the server (`Ctrl-C`), then delete the export. Never validate a release by
starting it inside the development checkout, and never let a validation run
leave files outside its own directory.

## 5. The publishing checklist

- [ ] section 1: Python suite, JS tests and smoke test all exit 0;
- [ ] section 2: `python3 tools/release_scan.py` exits 0 in the repository;
- [ ] section 3: the export built into a fresh directory outside the repository;
- [ ] section 4: every command green **inside** the export, manifest verified;
- [ ] both server starts show the honest states above, console clean;
- [ ] `README.md` says the version that `wsd/__init__.py` says, and does not
      describe shipped work as planned;
- [ ] the portfolio proposal reviewed against the tree: `.pcgsoft/project.yml`
      and the two `pcgsoft:` marker sections at the top of `README.md` still say
      what this version does, roadmap kept separate from the shipped list
      ([PROJECT_MANIFEST.md](PROJECT_MANIFEST.md) is the rule for when and how);
- [ ] the five documents read end to end: `README.md`, [INSTALL.md](INSTALL.md),
      [SECURITY.md](SECURITY.md), [ROLLBACK.md](ROLLBACK.md),
      [PROJECT_MANIFEST.md](PROJECT_MANIFEST.md);
- [ ] screenshots or recordings are optional and never part of the tree;
- [ ] a human has read the whole diff since the last published tree;
- [ ] every process started for validation is stopped and every temporary
      directory, local or in `/tmp`, is removed.

Then, and only then, by hand: choose the remote repository, commit, tag, push.
No script here does any of that — a tool that could publish has no business
deciding to.

## 6. When a release was wrong

The published tree is files: take it back, retag, republish; nothing on the
target machine has to change. Changes a *user* made to their own machine while
running it — the root helper, the power service, the desktop entry, profile
edits — are undone section by section in [ROLLBACK.md](ROLLBACK.md). What to
tell people who installed the bad tree, and what to fix first, is again a human
judgement; [SECURITY.md](SECURITY.md) says which findings are worth urgent
attention.

## 7. Troubleshooting

- **`refused: ... already exists`** — point the export at a new directory, or
  remove the old one deliberately. Nothing is ever merged into an existing tree.
- **`refused: ... is inside this repository`** — the export must leave the
  repository so that it cannot inherit anything the copy step does not name.
- **`NOT CLEAN: ... left for inspection`** — the export contains a finding. Fix
  the file in the repository, re-run section 2 there, and rebuild the export; do
  not edit the export, because the next build would undo the fix.
- **`unused-allow`** — the line an allow entry names no longer matches: the code
  moved or the problem was fixed. Delete or update the entry.
- **`SMOKE FAIL` with exit 2** — no headless Chrome, or the export could not
  start a server; this is not a verdict on the pages. Exit 1 is.
- **`sha256sum -c` reports a mismatch** — the tree was edited after the export
  (or corrupted in transit); rebuild rather than patching files in place.

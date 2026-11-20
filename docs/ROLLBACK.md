# Rolling Panoptes back

Every change this project can make to a machine, and how to undo it. Each
section: the commands, then how to confirm the undo took effect. Nothing here
needs the dashboard running. Paths shown as `…/…`, `~/…` or
`/home/YOUR_USER/…` are examples of the author's machine — substitute your own.

## Table of contents

| # | Change | Undo in one line |
|---|--------|------------------|
| [1](#1-desktop-launcher) | Desktop launcher | `ops/desktop/install-launcher.sh --uninstall` |
| [2](#2-radiance-ctl--the-sudoers-rule) | radiance-ctl + the sudoers rule | `sudo rm` the two installed files |
| [3](#3-panoptes-cpu-power-service) | panoptes-cpu-power service | `sudo systemctl disable --now …` + `sudo rm` the two files |
| [4](#4-agent-harness-profile-changes-omp) | Agent harness (OMP) profile changes | remove files added after the backup, then restore the tarball |
| [5](#5-engine-profile-files-radiance) | Engine profile files (Radiance) | history "Restore…", or copy a switch backup back |
| [6](#6-llamacpp-server) | llama.cpp server | stop the process; free port 8001 |
| [7](#7-removing-the-repository) | The repository itself | delete the checkout after 1–3 |

## 1. Desktop launcher

Installed per user by `ops/desktop/install-launcher.sh`; touches nothing else.

```sh
ops/desktop/install-launcher.sh --uninstall
```

**Confirm:** the script reports removal (or that there was nothing to remove)
and `ls "${XDG_DATA_HOME:-$HOME/.local/share}/applications"` no longer lists
`panoptes.desktop`. If a server was started from the launcher, stop it as in
section 7. Reverting the launcher does not delete its log directory; see
section 7 for that.

## 2. radiance-ctl + the sudoers rule

Two root-owned files plus an audit log were installed by
`sudo ops/radiance-ctl/install.sh`.

```sh
sudo rm /usr/local/sbin/radiance-ctl /etc/sudoers.d/radiance-ctl   # required
sudo rm /var/log/radiance-ctl.log                                  # optional: the audit log
```

**Confirm:**

- `sudo visudo -c` reports the sudo configuration as valid (the rule is gone,
  and removing a file from `/etc/sudoers.d` cannot break anything else).
- `sudo -n /usr/local/sbin/radiance-ctl status` now fails with a
  "command not found" style error instead of returning JSON.
- `ls /usr/local/sbin/radiance-ctl /etc/sudoers.d/radiance-ctl` reports both
  files as absent.

The Control tab then reports the helper as unavailable; it degrades, it does
not crash. Restart the dashboard afterwards if it had cached the helper state.

## 3. panoptes-cpu-power service

Installed by `sudo ops/cpu-power/install.sh`; the uninstall is the one printed
in that script's header:

```sh
sudo systemctl disable --now panoptes-cpu-power
sudo rm /usr/local/libexec/panoptes-cpu-power /etc/systemd/system/panoptes-cpu-power.service
sudo systemctl daemon-reload
```

**Confirm:**

- `systemctl status panoptes-cpu-power` reports the unit as not-found.
- `ls /run/panoptes` fails — the runtime directory is removed with the
  service (`RuntimeDirectoryPreserve=no`).
- `ls /usr/local/libexec/panoptes-cpu-power /etc/systemd/system/panoptes-cpu-power.service`
  reports both files as absent.

The CPU power figure in the dashboard disappears; the raw RAPL counters were
never re-permissioned (still `0400 root`), so there is nothing else to undo.

## 4. Agent harness profile changes (OMP)

Not part of Panoptes itself: before commissioning the harness profile used by
this project, the OMP profile `local` was privacy-hardened and re-pointed at
the local engine (journal entry CFG-000 in the experiment journal kept with the
development records, which are not part of the published product). The full
pre-change state was tarballed.

A tarball restore **only overwrites files the archive contains** — it never
deletes files created after the backup. On this machine the Radiance provider
registration `agent/models.yml` was added to the profile after the tarball was
made (the archive contains no `models.yml`; see CFG-000), so restoring alone
would leave the provider registered. General rule: list what the backup does
not contain and remove those files first.

```sh
rm ~/.omp/profiles/local/agent/models.yml    # created after the backup; see CFG-000
tar -xzf ~/.omp/backups/2026-11-18-pre-lab01/profile-local-agent.tgz -C ~/.omp/profiles/local
```

(the backup directory name `2026-11-18-pre-lab01` is this machine's example;
CFG-000 records the exact path, and it also holds `config.yml` and an
effective-config dump).

**Confirm:**

- `omp --profile local models` no longer lists the radiance provider.
- `omp --profile local config list | grep -E "telemetry|autoqa"` shows the
  original pre-change values (CFG-000's key list also covers the
  search/fetch tools).
- diff the restored tree against the archive (`tar -tzf` it and compare a few
  key files).

## 5. Engine profile files (Radiance)

The Engines tab can switch the active profile of an engine that has a
profiles directory and a journal directory (docs/INSTALL.md, Configuration):
the confirmed pipeline validates the candidate against the approved
parameter schema, copies the active file into the journal's `backups/`
directory, rewrites the active file atomically and restarts the engine.
Candidate profile files are never modified, and a switch whose restart
fails restores the backup by itself (if even that restore fails, the
history entry says so loudly). The journal and its backups live in the
engine's `journal_dir`, defaulting to `config-journal` under the
`[records] dir` — in this example `<records dir>/config-journal/`, holding backups
named `<stamp>-<hash>.yaml` (a pattern, not a file name to copy). Undo in
order of preference:

- Engines tab, switch history: "Restore…" on an earlier entry runs the
  same pipeline back and backs up the current state first.
- By hand while the engine is idle (no worker lock, no requests in
  flight): stop the engine (Control tab), copy the backup over the active
  profile file, start the engine again:

```sh
cp <journal_dir>/backups/<stamp>-<hash>.yaml <profiles_dir>/flashnext.yaml
```

The untouched originals of the YAML profiles as they stood before this project
are also kept in the development records (a private directory next to the
journal, not part of the published product); copying one of those back undoes
every change this project ever made to that file.

**Confirm:** `diff` of the file you restored against the active profile is
empty, the Engines tab's "Diff vs active" view shows no drift, and the
engine is healthy again (`curl -s http://127.0.0.1:8000/health`, and the
Inference tab shows it serving the expected profile). Changes inside a
running engine only take effect after restarting it (Control tab, or the
engine's own mechanism).

## 6. llama.cpp server

`ops/llama-4060/serve.sh` runs in the foreground and owns no service, lock or
boot-time state. Stop it with Ctrl-C in its terminal, or:

```sh
pkill -f llama-server        # if it was detached from its terminal
```

**Confirm:** `ss -ltn | grep ':8001 '` lists nothing (the socket is gone), and
`curl -s -m 3 http://127.0.0.1:8001/health` fails. The Engines tab marks the
engine offline within one poll. Nothing is installed, so there is no uninstall
— to remove the models themselves, delete the GGUF files.

## 7. Removing the repository

Do sections 1–3 first (the launcher and the two root components live outside
the checkout). Then stop the dashboard: Ctrl-C in its terminal, or
`pkill -f "python3 -m wsd"` if it was detached.

```sh
cd .. && rm -rf panoptes
```

**Confirm and leftovers:**

- `python3 -m wsd` from any remaining checkout of another copy aside, the repo
  directory is gone and port 8765 is free (`ss -ltn | grep ':8765 '` empty).
- The only state Panoptes keeps outside the checkout is its launcher log
  directory, `${XDG_STATE_HOME:-~/.local/state}/panoptes/` — remove it if you
  want no trace: `rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/panoptes"`.
- `/run/panoptes` is gone with the service (section 3). Nothing was ever
  written to system configuration beyond the files listed in sections 1–3.
  Outside the checkout, the only file Panoptes can modify is the active
  engine profile during a confirmed profile switch — its journal and
  backups sit under the journal directory (section 5). Harness and
  supervisor files are only ever read.

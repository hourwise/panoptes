# Panoptes security notes

Short version of the threat model and the controls. Written for a reader who
found this repo on the internet and wants to know what to trust before running
it.

## Threat model, in short

Panoptes assumes a **single-user (or trusted-users) Linux workstation** whose
browser is the risky surface: the owner visits web pages, and a hostile or
compromised page (or any other local process) may try to make the owner's
browser drive the dashboard — change state, restart the inference engine, or
read back private data. There is no authentication model beyond that: anyone
who can already act as your user account owns everything Panoptes can see, and
the dashboard deliberately offers no remote access to protect.

## Local-only binding

The server binds `127.0.0.1` only (there is no option to change the bind
address), so nothing on the LAN or internet can reach it. Engine `base_url`s
are validated at config load: a non-loopback host is rejected with a warning
and the engine skipped. The server makes no outbound connections at all —
every request goes to a configured loopback URL — and there is no telemetry,
update check or CDN reference anywhere in `wsd/` or `web/`.

## POST protections (Control and Engines) and why

State-changing endpoints are the container operations
(`/api/control/start|stop|restart`) and profile switching
(`/api/engines/<id>/profiles/<name>/apply`, `/api/engines/<id>/history/<entry>/restore`);
everything else is a read-only GET. Each browser-side protection closes a
specific hole:

- **POST-only, nothing state-changing on GET** — `<img>` tags, link
  prefetches and history cannot mutate anything.
- **`Host` header must be exactly `127.0.0.1:<port>` or `[::1]:<port>`** — a
  hostile page that rebinds a domain name to 127.0.0.1 (DNS rebinding) arrives
  with its own hostname in `Host` and is refused.
- **`Origin` header on POST must equal the dashboard's own origin** — blocks
  cross-site request forgery from any other page.
- **Per-launch random token** (`secrets.token_urlsafe`) embedded in the served
  page and sent back as `X-WSD-Token`, compared with `hmac.compare_digest` —
  a page that cannot read the token (i.e. any page that is not this origin)
  cannot forge the header.
- **`Content-Type: application/json` required on POST** — keeps every
  cross-origin POST out of the "simple request" class, forcing a CORS
  preflight; the server never answers `Access-Control-Allow-*`, so the
  preflight always fails for foreign origins.
- **Two-step confirmation** — the UI fires the POST only after an explicit
  in-page Confirm (body carries `{"confirm": true}`), so even a fully
  same-origin trigger needs a deliberate human click.

Server-side, the backend invokes the root helper via `subprocess.run` with an
argv list taken from a fixed enum — no shell, no interpolation — and supports
a dry-run mode (`PANOPTES_CONTROL_DRYRUN=1`) that returns canned results and
never calls sudo.

## Profile switching: what an apply will and will not do

Apply (`POST /api/engines/<id>/profiles/<name>/apply`) and restore
(`POST /api/engines/<id>/history/<entry>/restore`) are the only way Panoptes
writes an engine configuration, and each switch is layered:

- Every POST protection above applies unchanged: per-launch token,
  same-origin check, JSON content type, and `{"confirm": true}` in the body
  behind an in-page confirm step.
- Preconditions refuse the switch while touching nothing: a live supervised
  worker lock, requests in flight on the engine, or another switch holding
  its `profile-switch-active` lock (a dead pid counts as stale).
- No arbitrary values. The candidate must parse as a strict YAML subset and
  pass the approved-parameter schema — server and model identity cannot
  change at all, every knob has a type and a range, and a violation is
  refused naming the offending line or parameter. A change to a
  quality-sensitive setting is allowed but flagged: results before and
  after are not directly comparable.
- The active profile file is copied into the switch journal's backup
  directory before the write, which is atomic; candidate files in the
  profiles directory are never modified.
- Restart goes only through the existing root helper below — a switch adds
  no new privileged path — and readiness is checked against the new profile
  (health endpoint, expected context length, one completion). If the
  restarted engine does not come up serving it, the pipeline writes the
  backup back, restarts again and waits on its own budget
  (`PANOPTES_ROLLBACK_TIMEOUT_S`): shortening the forward budget
  (`PANOPTES_READY_TIMEOUT_S`) never shortens recovery.
- Every step lands in an append-only JSONL journal, so the outcome is
  auditable and any entry is reversible from the history table.

In dry run (`PANOPTES_CONTROL_DRYRUN=1`) the whole pipeline runs against a
sandboxed copy of the profiles directory with a private journal and switch
lock, and never touches the container.

## The narrow root helper and its interlock

The dashboard user is granted, via one `visudo`-validated sudoers file,
exactly five exact command lines: `sudo -n /usr/local/sbin/radiance-ctl
status|start|stop|restart|logs`. No wildcards, no arguments, `env_reset`;
anything else falls through to normal password sudo. The helper itself:

- acts only on one container name and refuses unless `docker inspect` confirms
  it runs the expected image prefix;
- never runs `docker run`, `exec`, `rm` or `pull` — the five verbs are the
  whole grammar;
- holds a flock so two lifecycle ops cannot interleave;
- **interlock:** refuses `stop`/`restart` while a supervised worker task is
  active (a lock file whose recorded PID is still alive; odd file types count
  as active, never followed or blocked on) or while the engine reports
  running/waiting/in-flight requests on its loopback `/stats` endpoint — so a
  forgotten browser tab cannot cut a running agent's legs mid-task;
- audits every call (time, sudo user, op, result) to a root/adm-owned log.

The machine values it acts on — container name, image prefix, worker-lock path
— are the `INSTALL-TIME SETTINGS` block at the top of
`ops/radiance-ctl/radiance-ctl` and nothing else: the helper reads **no**
configuration file, environment variable or other user-writable input, so the
dashboard user cannot widen what the privileged copy will touch. Editing those
three values is the only supported customisation, and the installed copy under
`/usr/local/sbin/` keeps whatever values it was installed with.

`ops/radiance-ctl/selftest.sh` checks all of this from the unprivileged side,
including that metacharacters and extra arguments are refused.

## Coarse CPU power publishing and the RAPL side channel

Raw RAPL energy counters are root-only (`0400`) on Linux because
microsecond-resolution power sampling leaks cryptographic keys — the
"Platypus" attack, CVE-2020-8694. Panoptes needs power only for energy-per-task
accounting, so instead of opening the counters it runs
`ops/cpu-power/panoptes-cpu-power`: a service that keeps the counters root-only
and publishes only a **1-second average rounded to whole watts** to a
world-readable file. That granularity is orders of magnitude too coarse for
the side channel (no per-process or per-phase signal survives) while energy
integrals stay accurate. The unit runs with no capabilities, no network,
read-only system view, no home access, and a syscall filter; the selftest
verifies the raw counter is *still* unreadable by your user.

## What Panoptes never sends anywhere

Nothing — but specifically: no code, test or documentation path performs any
outbound network access. Session transcripts from agent harnesses are read
locally and reduced to counts (tokens, tool names, timestamps); message text
is never exposed by the API, and the harness adapters read only the newest
session file's structure. Task packets, reviews and job records are read-only;
engine profiles are read at any time and written only as described above —
the active profile file, only during a confirmed switch. Secrets were never
put in `config/panoptes.toml` by
design: the config needs no credentials because every engine is unauthenticated
on loopback.

## Redaction in reports

The per-job Markdown report (`/api/jobs/<id>/report.md`) is meant to be shared
(e.g. pasted into a review or an issue), so it is redacted before you see it:
absolute paths under any home directory become `[path redacted]` (or
`[…]/<file name>` when the path names a file), and `token`/`secret`/`password`
assignments are blanked. Every replacement is counted and the count is printed
in the report's Data notes, so redaction is visible, never silent. Values are
otherwise printed exactly as stored — the report never rounds, coerces or
invents a number.

## Nothing about this machine in the shipped files

The files that ship as the product carry no home-directory path, host name,
`*.local` name, e-mail address, secret assignment, hardware serial or private
project name of the machine they were developed on — unless the line says it is
an example. `tools/release_scan.py` proves that and is re-run before any
publish; `tools/release_scan_allow.txt` holds every intended exception with its
reason. This machine's live `config/panoptes.toml` is deliberately **not** part
of that set and is never published: `config/panoptes.example.toml` is what
ships, with placeholders.

## Reporting issues

Use the repository issue tracker for ordinary bugs. Do not open a public issue
for anything that weakens the POST protections, the sudoers interlock or the
loopback assumption — use a private security advisory on the repository
instead, so the fix can ship before the details are public.

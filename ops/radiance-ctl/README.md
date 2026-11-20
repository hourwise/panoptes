# radiance-ctl — the engine lifecycle helper

`radiance-ctl` lets the dashboard user start, stop and restart **one** existing
Docker container without `docker` group membership and without broad sudo. It is
the only privileged component the Control tab and the profile-switching pipeline
use, and it is never run from this repository: `install.sh` copies it to
`/usr/local/sbin/radiance-ctl`, root-owned mode 0755, next to a sudoers rule at
`/etc/sudoers.d/radiance-ctl` (root:root 0440, validated with `visudo -cf`
before and `visudo -c` after).

- Grammar: exactly `status`, `start`, `stop`, `restart`, `logs` — one argument,
  no free-form input, no `docker run`/`exec`/`rm`/`pull`. Anything else exits 64.
- Interlock: `stop` and `restart` are refused while a supervised worker task is
  active or the engine reports running/waiting/in-flight requests (exit 75).
- Every call is appended to `/var/log/radiance-ctl.log`.
- Details and threat model: [../../docs/SECURITY.md](../../docs/SECURITY.md).

## INSTALL-TIME SETTINGS

The machine values live in one clearly marked block at the top of
[`radiance-ctl`](radiance-ctl):

| Setting | What it pins down |
| --- | --- |
| `CONTAINER` | the only container the helper may ever act on |
| `IMAGE_PREFIX` | the only image prefix `docker inspect` is allowed to confirm for it |
| `WORKER_LOCK` | absolute path of the supervised worker lock that blocks `stop`/`restart` |

**Those three lines are the helper's only machine values, and the only thing it
reads.** There is no configuration file, no environment variable (the script
clears `DOCKER_*` and `HOME` before it does anything) and no other input a
non-root user controls — so the dashboard user cannot widen what the
root-installed copy will touch. `WORKER_LOCK` must be an absolute path owned by
the dashboard user, never root-owned and never world-writable; the supervisor
writes it as `TASK START_EPOCH PID`, and a lock of any other file type counts as
active rather than being followed.

### Edit, then install

Nothing is filled in for you. Both files ship with placeholders, and
`install.sh` **refuses to install while any placeholder is still there**: it
names the ones it found, exits non-zero and copies nothing. The installer of a
root helper never guesses a username or a container — you edit them knowingly:

1. In [`radiance-ctl`](radiance-ctl), set the three `INSTALL-TIME SETTINGS` to
   this machine's values, so that none of `your-engine-container`, `your-org/`
   or `/home/YOUR_USER` is left in the file.
2. In [`radiance-ctl.sudoers`](radiance-ctl.sudoers), replace the placeholder
   user in the rule with the dashboard user's login name.
3. Install as root, from the repository root:

```sh
sudo ops/radiance-ctl/install.sh
```

Step 3 then runs the checks below and installs exactly what it validated.

The remaining constants down there (`DOCKER`, `AUDIT_LOG`, `OP_LOCK`) are not
settings: the binary, the audit log and the one-operation-at-a-time lock are
fixed by the design, and `STATS_URL` is the engine's loopback stats endpoint —
the same host and port as that engine's `base_url` in the dashboard
configuration, read with `curl` and never written.

This machine's installed copy keeps its own values: `/usr/local/sbin/radiance-ctl`
is a separate file, installed once, and is never re-read from or overwritten by
the repository. Editing the repository copy changes nothing until you run the
installer again — which is also why `install.sh` snapshots the sources, validates
the snapshot, and installs exactly what it validated, printing SHA-256 hashes.

## Verify after installing

```sh
ops/radiance-ctl/selftest.sh
```

`selftest.sh` checks that `sudo -n … status` needs no password, that extra
arguments, shell metacharacters, unlisted operations, plain `docker` and
environment passing are all refused, and that `restart` is refused while a
worker lock is held — for that check it writes its own lock (with its own pid)
at `$HOME/.local/state/lab01/worker-active`, so the engine is never stopped.
Keep that path equal to the installed helper's `WORKER_LOCK`, or the interlock
check tests nothing. It exits non-zero when a check fails.

To remove the helper again, see
[../../docs/ROLLBACK.md](../../docs/ROLLBACK.md#2-radiance-ctl--the-sudoers-rule).

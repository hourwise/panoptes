#!/bin/bash
# Non-root checks that the installed rule is as narrow as intended. Never stops Radiance:
# the only disruptive call is made while a worker lock held by this script is active, so it must be refused.
set -uo pipefail
pass=0; failn=0
ok()  { echo "PASS  $1"; pass=$((pass+1)); }
bad() { echo "FAIL  $1"; failn=$((failn+1)); }
C=/usr/local/sbin/radiance-ctl

[ "$(stat -c '%U:%G %a' $C)" = "root:root 755" ] && ok "script root:root 0755" || bad "script ownership/mode"
[ "$(sudo -n stat -c '%U:%G %a' /etc/sudoers.d/radiance-ctl 2>/dev/null || stat -c '%U:%G %a' /etc/sudoers.d/radiance-ctl 2>/dev/null)" = "root:root 440" ] \
  && ok "sudoers root:root 0440" || echo "INFO  sudoers file not readable as you (expected); install.sh printed its mode"
out="$(sudo -n $C status 2>&1)" && printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["ok"]' 2>/dev/null \
  && ok "status works without password" || bad "status: $out"
sudo -n $C status extra >/dev/null 2>&1 && bad "extra argument accepted" || ok "extra argument refused by sudo"
sudo -n $C 'status;id' >/dev/null 2>&1 && bad "shell metacharacters accepted" || ok "injected argument refused"
sudo -n $C pull >/dev/null 2>&1 && bad "unlisted operation accepted" || ok "unlisted operation refused"
sudo -n /usr/bin/docker ps >/dev/null 2>&1 && bad "plain docker allowed via sudo" || ok "plain docker not allowed"
sudo -n -E $C status >/dev/null 2>&1 && bad "environment passing allowed" || ok "environment passing refused"

# Interlock: hold a worker lock with this shell's PID, then ask for restart. It must be refused.
# The same path the installed helper's WORKER_LOCK setting names: the dashboard
# user's own state directory (here derived from $HOME, which is that user's home).
L="$HOME/.local/state/lab01/worker-active"; mkdir -p "$(dirname "$L")"
if [ -e "$L" ]; then
  echo "INFO  a real worker task is active; checking that restart is refused"
else
  echo "SELFTEST 0 $$" > "$L"; trap 'rm -f "$L"' EXIT
fi
out="$(sudo -n $C restart 2>&1)"
printf '%s' "$out" | grep -q 'worker task is active' && ok "restart refused during worker task" || bad "interlock: $out"
curl -s -m 3 http://127.0.0.1:8000/health | grep -q '"ok"' && ok "Radiance still up" || bad "Radiance not responding"

echo; echo "passed=$pass failed=$failn"; [ "$failn" -eq 0 ]

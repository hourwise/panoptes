#!/bin/bash
# Non-root checks for the CPU power publisher.
set -uo pipefail
pass=0; fail=0
ok()  { echo "PASS  $1"; pass=$((pass+1)); }
bad() { echo "FAIL  $1"; fail=$((fail+1)); }
F=/run/panoptes/cpu-power.json

systemctl is-active --quiet panoptes-cpu-power && ok "service active" || bad "service not active"
[ "$(stat -c '%U:%G %a' /usr/local/libexec/panoptes-cpu-power)" = "root:root 755" ] && ok "script root:root 0755" || bad "script ownership"
[ "$(stat -c '%a' /sys/class/powercap/intel-rapl:0/energy_uj)" = "400" ] && ok "raw RAPL counter still root-only" || bad "raw counter permissions changed"
cat /sys/class/powercap/intel-rapl:0/energy_uj >/dev/null 2>&1 && bad "raw counter readable as $(id -un)" || ok "raw counter not readable as $(id -un)"
t1=$(python3 -c "import json;print(json.load(open('$F'))['ts'])" 2>/dev/null); sleep 2.5
t2=$(python3 -c "import json;print(json.load(open('$F'))['ts'])" 2>/dev/null)
[ -n "$t1" ] && [ "$t1" != "$t2" ] && ok "published file readable and updating ($t1 -> $t2)" || bad "file not updating"
python3 - "$F" <<'EOF' && ok "values are whole watts in a sane range" || bad "values out of range"
import json, sys
d = json.load(open(sys.argv[1]))
for k in ("package_w", "core_w"):
    v = d.get(k)
    assert v is None or (isinstance(v, int) and 0 <= v <= 1000), (k, v)
assert d.get("package_w") is not None
print("      package", d["package_w"], "W, core", d["core_w"], "W")
EOF
systemctl show panoptes-cpu-power -p CapabilityBoundingSet -p PrivateNetwork -p ProtectSystem | sed 's/^/      /'
echo; echo "passed=$pass failed=$fail"; [ "$fail" -eq 0 ]

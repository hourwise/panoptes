#!/bin/bash
# Install the Panoptes CPU power publisher. Run with sudo:  sudo ops/cpu-power/install.sh
# Uninstall:  sudo systemctl disable --now panoptes-cpu-power && sudo rm /usr/local/libexec/panoptes-cpu-power \
#             /etc/systemd/system/panoptes-cpu-power.service && sudo systemctl daemon-reload
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
SRC="$(dirname "$(readlink -f "$0")")"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
cp "$SRC/panoptes-cpu-power" "$SRC/panoptes-cpu-power.service" "$TMP/"

echo "1/5 syntax check";            python3 -I -m py_compile "$TMP/panoptes-cpu-power"
echo "2/5 unit file check";         systemd-analyze verify "$TMP/panoptes-cpu-power.service" 2>&1 | grep -v 'ExecStart=.*not found\|No such file' || true
echo "3/5 installing root-owned copies"
install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "$TMP/panoptes-cpu-power" /usr/local/libexec/panoptes-cpu-power
install -o root -g root -m 0644 "$TMP/panoptes-cpu-power.service" /etc/systemd/system/panoptes-cpu-power.service
echo "4/5 starting the service"
systemctl daemon-reload
systemctl enable --now panoptes-cpu-power.service
sleep 3
echo "5/5 checks"
systemctl is-active panoptes-cpu-power.service
cat /run/panoptes/cpu-power.json; echo
ls -l /run/panoptes/cpu-power.json
stat -c '%a %U:%G %n' /sys/class/powercap/intel-rapl:0/energy_uj   # must still be 400 root:root
systemd-analyze security panoptes-cpu-power.service 2>/dev/null | tail -1 || true
sha256sum /usr/local/libexec/panoptes-cpu-power /etc/systemd/system/panoptes-cpu-power.service
echo "Installed."

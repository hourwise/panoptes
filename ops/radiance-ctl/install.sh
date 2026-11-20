#!/bin/bash
# Install radiance-ctl and its sudoers rule. Run with sudo:  sudo ops/radiance-ctl/install.sh
# Copies (not links) into root-owned locations outside the repository, validating before and after.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
SRC="$(dirname "$(readlink -f "$0")")"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# Pre-flight: the shipped files carry placeholders instead of machine values, and the
# installer of a root helper never guesses them. Edit them knowingly first — the
# username in radiance-ctl.sudoers and the INSTALL-TIME SETTINGS block in
# radiance-ctl (ops/radiance-ctl/README.md explains both). Nothing is substituted.
left=""
if grep -q YOUR_USER "$SRC/radiance-ctl.sudoers"; then
  left="$left YOUR_USER (radiance-ctl.sudoers)"
fi
for token in your-engine-container your-org/ /home/YOUR_USER; do
  if grep -q -- "$token" "$SRC/radiance-ctl"; then left="$left $token (radiance-ctl)"; fi
done
if [ -n "$left" ]; then
  echo "Refusing to install; these placeholders are still set:$left" >&2
  echo "Edit them to this machine's values and run again. Nothing was copied." >&2
  exit 1
fi

# Snapshot the sources first so the files validated are exactly the files installed.
cp "$SRC/radiance-ctl" "$SRC/radiance-ctl.sudoers" "$TMP/"

echo "1/5 syntax check of the script";        bash -n "$TMP/radiance-ctl"
echo "2/5 visudo check of the new rule";      visudo -cf "$TMP/radiance-ctl.sudoers"

echo "3/5 installing root-owned copies"
install -o root -g root -m 0755 "$TMP/radiance-ctl" /usr/local/sbin/radiance-ctl
install -o root -g root -m 0440 "$TMP/radiance-ctl.sudoers" /etc/sudoers.d/radiance-ctl

echo "4/5 visudo check of the whole sudo configuration"
if ! visudo -c >/dev/null; then
  rm -f /etc/sudoers.d/radiance-ctl
  echo "Whole-config check failed; the sudoers rule was removed again."; exit 1
fi

echo "5/5 audit log"
touch /var/log/radiance-ctl.log; chown root:adm /var/log/radiance-ctl.log; chmod 0640 /var/log/radiance-ctl.log

echo; sha256sum /usr/local/sbin/radiance-ctl /etc/sudoers.d/radiance-ctl
ls -l /usr/local/sbin/radiance-ctl /etc/sudoers.d/radiance-ctl
echo "Installed. Uninstall: sudo rm /usr/local/sbin/radiance-ctl /etc/sudoers.d/radiance-ctl"

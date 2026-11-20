#!/bin/bash
# Install the Panoptes desktop launcher for the current user. Run by the dashboard user:  ops/desktop/install-launcher.sh
# Fills ops/desktop/panoptes.desktop.in with absolute paths of THIS checkout and installs it to
# ${XDG_DATA_HOME:-~/.local/share}/applications/panoptes.desktop.  --uninstall removes it again.
set -euo pipefail

SRC="$(dirname "$(readlink -f "$0")")"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DEST="$APP_DIR/panoptes.desktop"

if [ "${1:-}" = "--uninstall" ]; then
    [ "$#" -eq 1 ] || { echo "Usage: $0 [--uninstall]" >&2; exit 2; }
    if [ -e "$DEST" ]; then
        rm -f "$DEST"
        echo "Removed $DEST"
    else
        echo "Nothing to remove: $DEST does not exist"
    fi
    exit 0
fi
[ "$#" -eq 0 ] || { echo "Usage: $0 [--uninstall]" >&2; exit 2; }

[ -f "$SRC/panoptes.desktop.in" ] || { echo "Missing $SRC/panoptes.desktop.in" >&2; exit 1; }
[ -x "$SRC/panoptes-launch.sh" ] || { echo "$SRC/panoptes-launch.sh is not executable; fix the repo first." >&2; exit 1; }
[ -f "$SRC/panoptes.svg" ] || { echo "Missing $SRC/panoptes.svg" >&2; exit 1; }

TMP="$(mktemp --suffix=.desktop)"; trap 'rm -f "$TMP"' EXIT
content="$(cat "$SRC/panoptes.desktop.in")"
content="${content//@LAUNCH@/$SRC/panoptes-launch.sh}"
content="${content//@ICON@/$SRC/panoptes.svg}"
printf '%s\n' "$content" > "$TMP"
case "$content" in
    *@LAUNCH@*|*@ICON@*) echo "Template placeholders were not filled."; exit 1 ;;
esac

if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "$TMP"
    echo "desktop-file-validate: OK"
else
    echo "desktop-file-validate not found; skipped validation."
fi

mkdir -p "$APP_DIR"
install -m 0644 "$TMP" "$DEST"
echo "Installed $DEST"
echo "  Exec = $SRC/panoptes-launch.sh"
echo "  Icon = $SRC/panoptes.svg"
echo "Log out and in again (or run update-desktop-database) if the launcher does not appear immediately."
echo "Uninstall: $0 --uninstall"

#!/bin/bash
# Installs (or updates) the Cockpit "Image updates" plugin and its root helpers.
#
#   sudo ./install.sh              install / update
#   sudo ./install.sh --dry-run    only show what would be done
#   DESTDIR=/tmp/stage ./install.sh   install into a staging root (packaging, tests)
#
# What it does:
#   plugin/*  -> /usr/share/cockpit/diun/           (644)
#   lib/*     -> /usr/local/lib/diun-radar/         (scripts 755, diunlib.py 644)
#   an existing installation is copied to /usr/local/lib/diun-radar/backup-<timestamp>/ first
#   if diun-radar.service exists, one radar run is started (rebuilds versions.json)
#
# What it does NOT do: install Diun, skopeo or the radar timer. The plugin
# offers that itself (panel "Missing requirements"), or run as root:
#   /usr/local/lib/diun-radar/diun-deps.sh check
#   /usr/local/lib/diun-radar/diun-deps.sh install skopeo|diun|radar
#
# Security: everything Cockpit or systemd executes as root lives in
# /usr/local/lib/diun-radar (root-owned). Never point it at /var/lib/diun -
# that directory is writable from inside the Diun container.
set -euo pipefail

SRC=$(cd "$(dirname "$0")" && pwd)
DEST=${DESTDIR:-}
PLUGIN_DIR=$DEST/usr/share/cockpit/diun
LIB_DIR=$DEST/usr/local/lib/diun-radar
PLUGIN_FILES=(manifest.json po.manifest.de.js index.html diun.js diun.css)
LIB_SCRIPTS=(diun-inventory.py diun-versions.py diun-update.py diun-deps.sh)
LIB_MODULES=(diunlib.py)

DRY=0
case "${1:-}" in
    "") ;;
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (--dry-run, --help)" >&2; exit 64 ;;
esac

if [ -z "$DEST" ] && [ "$(id -u)" != 0 ] && [ $DRY = 0 ]; then
    echo "Run as root (sudo ./install.sh) or set DESTDIR." >&2
    exit 77
fi

run() {
    echo "+ $*"
    if [ $DRY = 0 ]; then "$@"; fi
}

# Owner root only when we are root (DESTDIR staging as a normal user works too).
OWN=()
if [ "$(id -u)" = 0 ]; then OWN=(-o root -g root); fi

# 0. Sanity: all source files present, Python files parse.
for f in "${PLUGIN_FILES[@]}"; do [ -f "$SRC/plugin/$f" ] || { echo "missing: plugin/$f" >&2; exit 1; }; done
for f in "${LIB_SCRIPTS[@]}" "${LIB_MODULES[@]}"; do [ -f "$SRC/lib/$f" ] || { echo "missing: lib/$f" >&2; exit 1; }; done
python3 - "$SRC"/lib/*.py <<'PY'
import ast, sys
for f in sys.argv[1:]:
    ast.parse(open(f).read(), f)
PY
bash -n "$SRC/lib/diun-deps.sh"

# 1. Backup of what is installed now.
if [ -d "$PLUGIN_DIR" ] || [ -d "$LIB_DIR" ]; then
    BK=$LIB_DIR/backup-$(date +%Y%m%d-%H%M%S)
    run install -d -m 700 "${OWN[@]}" "$BK"
    for f in "${PLUGIN_FILES[@]}"; do
        if [ -f "$PLUGIN_DIR/$f" ]; then run cp -a "$PLUGIN_DIR/$f" "$BK/"; fi
    done
    for f in "${LIB_SCRIPTS[@]}" "${LIB_MODULES[@]}"; do
        if [ -f "$LIB_DIR/$f" ]; then run cp -a "$LIB_DIR/$f" "$BK/"; fi
    done
fi

# 2. Install.
run install -d -m 755 "${OWN[@]}" "$PLUGIN_DIR" "$LIB_DIR"
for f in "${PLUGIN_FILES[@]}"; do
    run install -m 644 "${OWN[@]}" "$SRC/plugin/$f" "$PLUGIN_DIR/$f"
done
for f in "${LIB_MODULES[@]}"; do
    run install -m 644 "${OWN[@]}" "$SRC/lib/$f" "$LIB_DIR/$f"
done
for f in "${LIB_SCRIPTS[@]}"; do
    run install -m 755 "${OWN[@]}" "$SRC/lib/$f" "$LIB_DIR/$f"
done

# 3. Refresh the radar data (its format may have changed with this version).
if [ -z "$DEST" ] && command -v systemctl >/dev/null 2>&1 \
        && systemctl cat diun-radar.service >/dev/null 2>&1; then
    run systemctl start --no-block diun-radar.service
    echo "Version radar started (result: journalctl -u diun-radar.service)."
fi

echo
echo "Done. Reload the Cockpit page (menu: Image updates / Image-Updates)."
if [ -z "$DEST" ] && [ $DRY = 0 ] && [ -x "$LIB_DIR/diun-deps.sh" ]; then
    echo "Requirements:"
    "$LIB_DIR/diun-deps.sh" check || true
fi

#!/bin/bash
# Runs every offline test: no Docker, no registry, no root needed.
#   cd test && npm ci && ./run-all.sh
# The end-to-end test against a real Docker host is separate: e2e-server.sh
set -u
cd "$(dirname "$0")"

if [ ! -d node_modules/jsdom ]; then
    echo "jsdom fehlt – bitte zuerst: (cd test && npm ci)" >&2
    exit 2
fi

fail=0
run() {
    local name=$1; shift
    local log
    log=$(mktemp)
    if "$@" >"$log" 2>&1; then
        echo "PASS  $name"
    else
        echo "FAIL  $name"
        sed 's/^/      | /' "$log"
        fail=1
    fi
    rm -f "$log"
}

run "python: diunlib + Versions-Radar" python3 radar_test.py
run "python: Inventar"                 python3 inv_test.py
run "python: Syntax lib/*.py"          python3 -c 'import ast, glob, sys; [ast.parse(open(f).read(), f) for f in glob.glob("../lib/*.py")]'
run "shell:  Syntax lib/*.sh + e2e"    bash -c 'for f in ../lib/*.sh e2e-server.sh run-all.sh ../install.sh; do bash -n "$f" || exit 1; done'
run "plugin: Ladeanzeige"              node loading.js
run "plugin: Sprache + Voraussetzungen" node i18n-deps.js
run "plugin: Update, Rollback, Backups" node update-backups.js
run "plugin: Versions-Spalten"         node version-cols.js
run "plugin: Gesamtdarstellung (Smoke)" node harness.js

echo
if [ $fail = 0 ]; then echo "ALLE TESTS GRÜN"; else echo "ES GIBT FEHLER"; fi
exit $fail

#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Runs every offline test: no Docker, no registry, no root needed.
#   cd test && npm ci && ./run-all.sh
# The end-to-end test against a real Docker host is separate: e2e-server.sh
set -u
cd "$(dirname "$0")" || exit 2

if [ ! -d node_modules/jsdom ]; then
    echo "jsdom missing – run first: (cd test && npm ci)" >&2
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

run "python: diunlib + version radar"   python3 radar_test.py
run "python: release notes (GitHub)"    python3 notes_test.py
run "python: inventory"                 python3 inv_test.py
run "python: update/rollback/cleanup"   python3 update_test.py
run "python: database majors (stage 5)" python3 major_test.py
run "python: status for the Status Board" python3 status_test.py
run "python: time limits, signals"      python3 proc_test.py
run "python: upgrade base (CI)"        python3 upgrade_base_test.py
run "shell:  diun-deps.sh check (JSON)" sh -c '../lib/diun-deps.sh check | python3 deps_check_test.py'
run "python: syntax lib/*.py"           python3 -c 'import ast, glob, sys; [ast.parse(open(f).read(), f) for f in glob.glob("../lib/*.py")]'
run "shell:  syntax lib/*.sh + e2e"     bash -c 'for f in ../lib/*.sh e2e-server.sh run-all.sh ../packaging/scripts/* ../tools/*.sh; do bash -n "$f" || exit 1; done'
run "plugin: loading indicator"         node loading.js
run "plugin: language + requirements"   node i18n-deps.js
run "plugin: update, rollback, backups" node update-backups.js
run "plugin: change version"            node version-change.js
run "plugin: database major"           node db-major.js
run "plugin: release notes in dialogs"  node release-notes.js
run "plugin: a page missing a script"   node partial-load.js
run "plugin: radar now, cleanup, history" node radar-cleanup.js
run "plugin: version columns"           node version-cols.js
run "plugin: Diun events per tag"      node diun-events.js
run "plugin: status counts these rows"  node status-rows.js
run "plugin: status icon in the menu"   node menu-status.js
run "plugin: places of the status links" node places.js
run "plugin: page frame, tabs, labels"  node page-frame.js
run "design: contrast (light + dark)"   python3 contrast_test.py
run "design: fonts ship with the add-on" python3 fonts_test.py
run "plugin: whole table (smoke)"       node harness.js
run "repo:   version + changelog"       sh ../tools/check-version.sh
run "repo:   shared design files"       sh ../tools/check-design-files.sh

echo
if [ $fail = 0 ]; then echo "ALL TESTS PASSED"; else echo "THERE ARE FAILURES"; fi
exit $fail

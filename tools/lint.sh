#!/bin/sh
# Static checks: shellcheck for the shell scripts, pyflakes for the Python
# files, systemd-analyze for the unit files. The test workflow runs this on
# every push and pull request.
#   sh tools/lint.sh
# Needs shellcheck, pyflakes (python3 -m pip install pyflakes) and systemd.
# SPDX-License-Identifier: Apache-2.0
set -u
cd "$(dirname "$0")/.." || exit 1
fail=0

echo "== shellcheck"
shellcheck --severity=warning lib/*.sh install.sh test/*.sh tools/*.sh || fail=1

echo "== pyflakes"
python3 -m pyflakes lib test tools || fail=1

# systemd-analyze verify exits 0 even for unknown keys, so any output counts as
# a finding. The copies get mode 644: a checkout may carry other modes, which
# systemd-analyze would report as well.
echo "== systemd-analyze verify"
tmp=$(mktemp -d) || exit 1
cp systemd/*.service systemd/*.timer "${tmp:?}/"
chmod 644 "${tmp:?}"/*
out=$(systemd-analyze verify "${tmp:?}"/* 2>&1)
rm -rf "${tmp:?}"
if [ -n "$out" ]; then
    printf '%s\n' "$out"
    fail=1
fi

if [ "$fail" = 0 ]; then echo "LINT OK"; else echo "LINT FAILED"; fi
exit "$fail"

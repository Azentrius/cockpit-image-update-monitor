#!/bin/sh
# Checks the units in systemd/ with systemd-analyze verify. The test
# workflow runs it with the other lint steps.
#   sh tools/check-units.sh
# systemd-analyze verify exits 0 even for unknown keys, so any output counts as
# a finding. The copies get mode 644: a checkout may carry other modes, which
# systemd-analyze would report as well.
# SPDX-License-Identifier: Apache-2.0
set -u
cd "$(dirname "$0")/.." || exit 2
tmp=$(mktemp -d) || exit 2
cp systemd/*.service systemd/*.timer "${tmp:?}/"
chmod 644 "${tmp:?}"/*
out=$(systemd-analyze verify "${tmp:?}"/* 2>&1)
rm -rf "${tmp:?}"
if [ -n "$out" ]; then
    printf '%s\n' "$out"
    echo "UNITS: FINDINGS"
    exit 1
fi
echo "UNITS OK"

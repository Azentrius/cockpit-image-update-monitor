#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# The upgrade test of the packages, as root in a throw-away container (CI, test workflow, job
# upgrade):
#
#   VERSION=0.19.0 BASE=v0.18.1 sh test/package-upgrade.sh deb|rpm
#
# This build's package is in /dist; BASE names the release to upgrade from
# (tools/upgrade-base.py), its package in /prev, checked against its SHA256SUMS before.
# Installs that release, adds data a server has after some updates, installs this build over it
# and checks:
#   - the version went up, compared by the package manager's own rules,
#   - the package database names this build's version, and the files on disk are this build's,
#     byte for byte (not those of the release, nor a mix),
#   - the data is unchanged,
# then runs a helper as root and removes the package: its files go, with the __pycache__ the helper
# left in the package folder, the data stays.
# With BASE empty (no older release with packages) it says so and checks the same with a fresh
# install of this build; it never passes an upgrade it did not run.
set -eu
kind=${1:?deb or rpm}
name=cockpit-image-update-monitor
want=$(printf '%s' "${VERSION:?}" | tr '-' '~')-1
DATA="/var/backups/image-updates/operations.jsonl /var/backups/image-updates/dumps/app/20260101-000000_app-db.sql.gz
/etc/image-updates.conf /var/lib/diun-radar/versions.json"

case $kind in
    deb)
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        new=$(ls /dist/*.deb)
        old=$(ls /prev/*.deb 2> /dev/null || true)
        pkg_install() { apt-get install -y -qq "$1" > /dev/null; }
        pkg_remove() { apt-get remove -y -qq "$name" > /dev/null; }
        pkg_version() { dpkg-query -W -f='${Version}' "$name"; }
        pkg_older() { dpkg --compare-versions "$1" lt "$2"; }
        # dpkg --verify: the files match the package database; diff: and they are this build's.
        same_files() {
            rm -rf /tmp/build && mkdir /tmp/build && dpkg-deb -x "$new" /tmp/build
            dpkg --verify "$name"
            for d in /usr/lib/cockpit-image-updates /usr/share/cockpit/image-updates; do
                diff -r "/tmp/build$d" "$d"
            done
        } ;;
    rpm)
        new=$(ls /dist/*.rpm)
        old=$(ls /prev/*.rpm 2> /dev/null || true)
        pkg_install() { dnf install -y -q "$1"; }
        pkg_remove() { dnf remove -y -q "$name"; }
        pkg_version() { rpm -q --qf '%{VERSION}-%{RELEASE}' "$name"; }
        pkg_older() { test "$(rpm --eval "%{lua:print(rpm.vercmp(\"$1\", \"$2\"))}")" = -1; }
        # rpm -V: the files match the package database; the digests: and they are this build's.
        same_files() {
            rpm -V "$name"
            fmt='[%{FILENAMES} %{FILEDIGESTS} %{FILEMODES:octal}\n]'
            test "$(rpm -qp --qf "$fmt" "$new" | sort)" = "$(rpm -q --qf "$fmt" "$name" | sort)"
        } ;;
    *) echo "deb or rpm, not $kind" >&2; exit 64 ;;
esac

add_data() {
    mkdir -p /var/backups/image-updates/dumps/app /var/lib/diun-radar
    echo '{"id": "20260101-000000-app", "kind": "update", "status": "ok"}' > /var/backups/image-updates/operations.jsonl
    echo 'database dump' > /var/backups/image-updates/dumps/app/20260101-000000_app-db.sql.gz
    printf 'KEEP_DUMPS=5\nDOCKER_TIMEOUT=900\n' > /etc/image-updates.conf
    echo '{"generated": "2026-01-01T00:00:00Z", "images": {}}' > /var/lib/diun-radar/versions.json
    # shellcheck disable=SC2086
    sha256sum $DATA > /tmp/data.sha256
}

if [ -n "${BASE:-}" ]; then
    [ -n "$old" ] || { echo "BASE=$BASE, but no package of it in /prev" >&2; exit 1; }
    pkg_install "$old"
    from=$(pkg_version)
    echo "installed release $BASE: $from"
    add_data
    pkg_install "$new"
    to=$(pkg_version)
    echo "upgraded: $from -> $to"
    pkg_older "$from" "$to" || { echo "not an upgrade: $from -> $to" >&2; exit 1; }
else
    echo "No release older than $VERSION with packages: no upgrade to test, a fresh install of this build."
    add_data
    pkg_install "$new"
    to=$(pkg_version)
    echo "installed: $to"
fi
[ "$to" = "$want" ] || { echo "installed $to, this build is $want" >&2; exit 1; }
same_files
sha256sum -c --quiet /tmp/data.sha256
/usr/lib/cockpit-image-updates/diun-deps.sh check | python3 -c 'import json, sys; json.load(sys.stdin)'
# A helper as root, as the page and the timers run them on a server: Python leaves __pycache__ in
# the package folder, which no package owns; the removal takes it along.
/usr/lib/cockpit-image-updates/diun-update.py status --lang en | python3 -c 'import json, sys; json.load(sys.stdin)'
test -d /usr/lib/cockpit-image-updates/__pycache__
pkg_remove
test ! -e /usr/lib/cockpit-image-updates
test ! -e /usr/share/cockpit/image-updates
sha256sum -c --quiet /tmp/data.sha256
echo "ok: files of this build installed, data kept over the upgrade and the removal"

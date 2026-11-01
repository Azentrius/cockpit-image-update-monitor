#!/bin/sh
# dnf in a CI container, as root, that leaves a slow mirror and ends a try that hangs, instead of
# waiting until the job times out: on AlmaLinux the first dnf install of a job, which downloads
# the repository data, once took almost 14 minutes without a word under -q (normally well under a
# minute), and once ran into the job's 20-minute limit.
#
#   sh /dnf.sh install -y -q PACKAGE...   the repository data first, then the command
#   sh /dnf.sh makecache                  only the repository data, for a dnf of its own after it
#
# dnf leaves a mirror that sends less than 100 kB/s for 20 seconds and takes the next one (its own
# defaults: 1 kB/s, 30 seconds); the two settings go into /etc/dnf/dnf.conf and stay for every later
# dnf in the container. The repository data comes first and without -q, so the log shows each
# repository's size and speed. Each try may take 180 seconds; a failed or hung one gets a second
# and a third, 15 seconds apart.
# SPDX-License-Identifier: Apache-2.0
set -u
[ $# -gt 0 ] || { echo "usage: sh dnf.sh makecache | install -y -q PACKAGE..." >&2; exit 64; }
conf=/etc/dnf/dnf.conf
grep -qx 'minrate=100000' "$conf" 2> /dev/null ||
    sed -i -e '/^\[main\]/a minrate=100000' -e '/^\[main\]/a timeout=20' "$conf"
grep -qx 'minrate=100000' "$conf" || { echo "::error::no [main] section in $conf"; exit 1; }

tries() {
    for try in 1 2 3; do
        rc=0
        timeout --kill-after=10 180 dnf "$@" || rc=$?
        [ "$rc" != 0 ] || return 0
        case $rc in
            124 | 137) why="no end within 180 seconds" ;;
            *) why="exit code $rc" ;;
        esac
        echo "::warning::dnf $1 failed ($why, try $try of 3)"
        [ "$try" -lt 3 ] || { echo "::error::dnf $1 failed three times"; exit "$rc"; }
        sleep 15
    done
}

tries makecache
[ "$1" = makecache ] || tries "$@"

#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# The release an upgrade test starts from, downloaded and checked (CI, test workflow: jobs
# upgrade and cockpit).
#
#   GH_TOKEN=… GITHUB_REPOSITORY=owner/repo sh tools/upgrade-base.sh DIR
#
# Picks the newest release older than this build (tools/upgrade-base.py: Semantic Versioning,
# no draft or pre-release, with packages), downloads its .deb, .rpm and SHA256SUMS into DIR and
# checks them against SHA256SUMS and their build provenance. Prints BASE=<tag> and
# OLD_VERSION=<version> (KEY=value lines for $GITHUB_OUTPUT or $GITHUB_ENV), everything else
# to stderr. Without such a release it prints BASE= and OLD_VERSION= with a warning and exits
# 0: the caller then checks a fresh install and must say so.
set -eu
dir=${1:?usage: upgrade-base.sh DIR}
repo=${GITHUB_REPOSITORY:?}
version=$(sh tools/check-version.sh)
mkdir -p "$dir"
gh api "repos/$repo/releases?per_page=100" > "$dir/releases.json"
rc=0
base=$(python3 tools/upgrade-base.py "$version" < "$dir/releases.json") || rc=$?
rm "$dir/releases.json"
case $rc in
    0) ;;
    3) echo "::warning::No release older than $version with packages: no upgrade to test, a fresh install is checked instead." >&2
       echo "BASE="
       echo "OLD_VERSION="
       exit 0 ;;
    *) exit "$rc" ;;
esac
gh release download "$base" --repo "$repo" --dir "$dir" \
    --pattern 'cockpit-image-update-monitor_*.deb' --pattern 'cockpit-image-update-monitor-*.rpm' --pattern SHA256SUMS
(cd "$dir" && sha256sum --ignore-missing -c SHA256SUMS >&2)
for f in "$dir"/*.deb "$dir"/*.rpm; do
    gh attestation verify "$f" --repo "$repo" > /dev/null
done
echo "upgrade from $base to $version (this build)" >&2
echo "BASE=$base"
echo "OLD_VERSION=${base#v}"

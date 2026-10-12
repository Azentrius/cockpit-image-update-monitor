#!/bin/sh
# The shared Azentrius design files in plugin/ are byte-identical copies of
# src/ in Azentrius/cockpit-cronjobs; tools/design-files.sha256 pins them.
# Never edit them here: take the new versions from there and update the list.
#   sh tools/check-design-files.sh
# SPDX-License-Identifier: Apache-2.0
set -u
cd "$(dirname "$0")/.." || exit 2
if sha256sum --strict --quiet -c tools/design-files.sha256; then
    echo "design files: identical to the shared versions"
else
    echo "design files: differ from tools/design-files.sha256" >&2
    exit 1
fi

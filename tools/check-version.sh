#!/bin/sh
# The version lives in plugin/diun.js (const VERSION). plugin/index.html loads
# every script and stylesheet of the add-on (the shared design files too) with
# ?v=VERSION so browsers fetch new files after an update, and CHANGELOG.md needs
# a section for the version.
# Prints the version; exit 1 when anything disagrees.
# SPDX-License-Identifier: Apache-2.0
set -eu
cd "$(dirname "$0")/.."
v=$(sed -n 's/^ *const VERSION = "\(.*\)";.*/\1/p' plugin/diun.js)
if [ -z "$v" ]; then
    echo "plugin/diun.js has no const VERSION" >&2
    exit 1
fi
if ! printf '%s\n' "$v" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$'; then
    echo "VERSION \"$v\" is not a version like 0.6.1" >&2
    exit 1
fi
for f in azentrius-theme.js azentrius.css icons.js i18n.js menu-status.js diun.js diun.css; do
    if ! grep -q "\"$f?v=$v\"" plugin/index.html; then
        echo "plugin/index.html does not load $f?v=$v" >&2
        exit 1
    fi
done
# Nothing else either: every local src/href except Cockpit's own ../ files.
bad=$(grep -oE '(src|href)="[^"]+"' plugin/index.html | sed -E 's/^(src|href)="//; s/"$//' |
      grep -vE '^(\.\./|https?:)' | grep -v "?v=$v\$" || true)
if [ -n "$bad" ]; then
    echo "plugin/index.html loads without ?v=$v: $bad" >&2
    exit 1
fi
if ! sh tools/release-notes.sh "$v" > /dev/null; then
    echo "CHANGELOG.md has no section for $v" >&2
    exit 1
fi
echo "$v"

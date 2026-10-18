#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The release the upgrade test of the packages starts from (test workflow, job upgrade).

  python3 tools/upgrade-base.py VERSION < releases.json

releases.json is the GitHub API's list of releases (gh api repos/OWNER/REPO/releases).
Prints the tag of the newest release that is older than VERSION by Semantic Versioning
precedence (not by its text: 0.10.0 is newer than 0.9.0, 0.19.0-rc.1 older than 0.19.0), is
neither a draft nor a pre-release and has this package's .deb and .rpm and SHA256SUMS. So a build
of a version that is released already (a pull request after the release, before the next version)
upgrades from the release before it, never from itself. Exit 3 without output when there is no
such release (the caller says so), 2 for a VERSION that is not a version.
"""
import json
import re
import sys

NAME = "cockpit-image-update-monitor"
SEMVER = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
                    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
                    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")


def key(version):
    """Sort key by SemVer 2.0 precedence, None if `version` is not one. A
    release sorts after its pre-releases; pre-release fields compare numbers
    as numbers, below words; build metadata does not count."""
    m = SEMVER.match(version or "")
    if not m:
        return None
    major, minor, patch, pre = m.groups()
    if pre is None:
        rank = (1, ())
    else:
        rank = (0, tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")))
    return int(major), int(minor), int(patch), rank


def has_packages(release):
    names = {a.get("name", "") for a in release.get("assets") or []}
    return ("SHA256SUMS" in names
            and any(n.startswith(NAME + "_") and n.endswith(".deb") for n in names)
            and any(n.startswith(NAME + "-") and n.endswith(".rpm") for n in names))


def base(version, releases):
    """The tag to upgrade from, or None."""
    now = key(version)
    best = None
    for r in releases:
        k = key(r.get("tag_name"))
        if r.get("draft") or r.get("prerelease") or k is None or k >= now or not has_packages(r):
            continue
        if best is None or k > best[0]:
            best = (k, r["tag_name"])
    return best and best[1]


def main(argv):
    if len(argv) != 1 or key(argv[0]) is None:
        print("usage: upgrade-base.py VERSION < releases.json (VERSION like 0.19.0)", file=sys.stderr)
        return 2
    tag = base(argv[0], json.load(sys.stdin))
    if not tag:
        print(f"No release older than {argv[0]} with the packages of {NAME}.", file=sys.stderr)
        return 3
    print(tag)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

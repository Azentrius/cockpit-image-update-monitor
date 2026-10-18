#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Offline tests for tools/upgrade-base.py, the choice of the release the CI
upgrade test starts from: a build newer than the newest release, a build of the
version released already, no older release with packages, versions compared by
Semantic Versioning and not as text, drafts and pre-releases left out."""
import importlib.util
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "tools", "upgrade-base.py")
spec = importlib.util.spec_from_file_location("upgrade_base", TOOL)
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)

fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(("OK   " if ok else "FAIL ") + label + ("" if ok else f"\n     got  {got!r}\n     want {want!r}"))


def rel(tag, packages=True, draft=False, prerelease=False, name=B.NAME):
    v = tag.lstrip("v").replace("-", "~")
    assets = [{"name": f"{name}_{v}-1_all.deb"}, {"name": f"{name}-{v}-1.noarch.rpm"},
              {"name": "SHA256SUMS"}] if packages else [{"name": "SHA256SUMS"}]
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease, "assets": assets}


def run(version, releases):
    p = subprocess.run([sys.executable, TOOL, version], input=json.dumps(releases), capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


RELEASES = [rel("v0.18.1"), rel("v0.18.0"), rel("v0.13.0"), rel("v0.12.2", name="cockpit-image-updates")]

# 1. This build is newer than the newest release: upgrade from that one.
check("build newer than the newest release -> the newest release", run("0.19.0", RELEASES), (0, "v0.18.1", ""))

# 2. This build has the version of the newest release (a pull request after the
#    release, before the next version): never from itself, from the one before.
check("build of the released version -> the release before it",
      run("0.19.0", [rel("v0.19.0")] + RELEASES), (0, "v0.18.1", ""))

# 3. No older release with this package's files: exit 3, said on stderr.
code, out, err = run("0.13.0", RELEASES)
check("no older release with the packages -> exit 3, nothing printed, the reason on stderr",
      (code, out, "No release older than 0.13.0" in err), (3, "", True))
check("no releases at all -> exit 3", run("0.19.0", [])[0], 3)

# Versions as versions, not as text.
check("0.10.0 is newer than 0.9.0", run("0.11.0", [rel("v0.9.0"), rel("v0.10.0")])[1], "v0.10.0")
check("1.0.0 is newer than 0.99.99", run("1.0.1", [rel("v0.99.99"), rel("v1.0.0")])[1], "v1.0.0")
check("a newer release than the build is never the base",
      run("0.18.5", [rel("v0.19.0"), rel("v0.18.10"), rel("v0.18.4")])[1], "v0.18.4")
order = ["0.9.0", "0.10.0-alpha", "0.10.0-alpha.1", "0.10.0-alpha.beta", "0.10.0-beta", "0.10.0-beta.2",
         "0.10.0-beta.11", "0.10.0-rc.1", "0.10.0", "0.10.1", "1.0.0"]
check("SemVer precedence (pre-releases, numeric fields, a release after its pre-releases)",
      sorted(reversed(order), key=B.key), order)
check("build metadata does not count", B.key("1.2.3+build.5") == B.key("1.2.3"), True)
check("not versions", [B.key(x) for x in ("", "latest", "1.2", "01.2.3", "v1.2.3.4")], [None] * 5)

# Drafts, pre-releases and releases without packages are left out.
check("a draft, a pre-release and one without packages are skipped",
      run("0.19.0", [rel("v0.18.3", draft=True), rel("v0.18.2", prerelease=True), rel("v0.18.1", packages=False),
                     rel("v0.18.0")])[1], "v0.18.0")
check("a pre-release build upgrades from the release before it",
      run("0.19.0-rc.2", [rel("v0.19.0-rc.1", prerelease=True), rel("v0.18.1")])[1], "v0.18.1")
check("a VERSION that is not one -> exit 2", run("v-next", RELEASES)[0], 2)
check("base() in-process", B.base("0.19.0", RELEASES), "v0.18.1")

print("\nALL CHECKS PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

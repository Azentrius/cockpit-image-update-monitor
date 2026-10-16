#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Offline test for lib/diun-inventory.py: Docker is replaced by a fake that
returns two containers - a postgres whose tag already points to a newer local
image ("pulled, not rolled out") and an ollama on `latest` whose image only
carries an inherited Ubuntu label (must NOT be taken as the version)."""
import contextlib
import importlib.util
import io
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
spec = importlib.util.spec_from_file_location("inv", os.path.join(ROOT, "diun-inventory.py"))
inv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inv)
inv.LOCK = "/tmp/diun-inventory-test.lock"

COMPOSE = {"com.docker.compose.project": "glitchtip", "com.docker.compose.service": "postgres",
           "com.docker.compose.project.working_dir": "/opt/g",
           "com.docker.compose.project.config_files": "/opt/g/compose.yml"}
C = [{"Name": "/glitchtip-postgres", "Image": "sha256:old", "Config": {"Image": "postgres:16-alpine", "Labels": COMPOSE}},
     {"Name": "/ollama", "Image": "sha256:oll", "Config": {"Image": "ollama/ollama:latest", "Labels": {}}}]
IMAGES = {
    "sha256:old": {"Id": "sha256:old", "RepoDigests": ["postgres@sha256:16bc"], "Config": {"Env": ["PG_VERSION=16.14"]}},
    "sha256:new": {"Id": "sha256:new", "RepoDigests": ["postgres@sha256:7218"], "Config": {"Env": ["PG_VERSION=16.15"]}},
    "sha256:oll": {"Id": "sha256:oll", "RepoDigests": ["ollama/ollama@sha256:c484"],
                   "Config": {"Env": [], "Labels": {"org.opencontainers.image.version": "24.04"}}},
}


def docker(*a):
    if a[0] == "ps":
        return "a b\n"
    if a[0] == "inspect":
        return json.dumps(C)
    if a[:2] == ("image", "ls"):
        return "postgres:16-alpine sha256:new\nollama/ollama:latest sha256:oll\n"
    if a[:2] == ("image", "inspect"):
        assert sorted(a[2:]) == ["sha256:new", "sha256:old", "sha256:oll"], a   # one call for all
        return json.dumps([IMAGES[x] for x in a[2:]])
    raise AssertionError(a)


inv.docker = docker
out = io.StringIO()
with contextlib.redirect_stdout(out):
    rc = inv.main()
rows = {r["name"]: r for r in map(json.loads, out.getvalue().splitlines())}
fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(("OK   " if ok else "FAIL ") + label + ("" if ok else f"  got {got!r}, want {want!r}"))


pg, ol = rows["glitchtip-postgres"], rows["ollama"]
check("exit code", rc, 0)
check("postgres version from PG_VERSION", (pg["version"], pg["version_src"]), ("16.14", "env:PG_VERSION"))
check("postgres pulled + newer version", (pg["pulled"], pg["pulled_version"], pg["tagid"]), (True, "16.15", "sha256:new"))
check("postgres managed by compose", (pg["managed"], pg["project"], pg["service"]), (True, "glitchtip", "postgres"))
check("postgres digest", pg["digest"], "sha256:16bc")
check("ollama: inherited label ignored", (ol["version"], ol["version_src"]), (None, None))
check("ollama not pulled, not managed", (ol["pulled"], ol["managed"]), (False, False))
check("compose files: the label as a list, none without compose", (pg["files"], ol["files"]),
      (["/opt/g/compose.yml"], []))
print("\nALL CHECKS PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

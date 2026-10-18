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
     {"Name": "/ollama", "Image": "sha256:oll", "Config": {"Image": "ollama/ollama:latest", "Labels": {}}},
     # a CI job of the Forgejo runner (a CI host, 2026-10-17): no labels, a volume at /var/run/act
     {"Name": "/FORGEJO-ACTIONS-TASK-2415_WORKFLOW-a545a355_JOB-unit", "Image": "sha256:ci",
      "Config": {"Image": "example-ci:1", "Labels": {}},
      "Mounts": [{"Type": "volume", "Destination": "/workspace/example/app"},
                 {"Type": "volume", "Destination": "/var/run/act"}]}]
IMAGES = {
    "sha256:old": {"Id": "sha256:old", "RepoDigests": ["postgres@sha256:16bc"],
                   "Config": {"Env": ["PG_VERSION=16.14"],
                              "Labels": {"org.opencontainers.image.revision": "35b2a975d74519de0678486bdbd46f644533b710"}},
                   "Created": "2026-08-14T02:11:09.512433187Z"},
    "sha256:new": {"Id": "sha256:new", "RepoDigests": ["postgres@sha256:7218"], "Config": {"Env": ["PG_VERSION=16.15"]}},
    "sha256:oll": {"Id": "sha256:oll", "RepoDigests": ["ollama/ollama@sha256:c484"],
                   "Config": {"Env": [], "Labels": {"org.opencontainers.image.version": "24.04"}}},
    "sha256:ci": {"Id": "sha256:ci", "RepoDigests": [], "Config": {"Env": []}},
}


def docker(*a):
    if a[0] == "ps":
        return "a b\n"
    if a[0] == "inspect":
        return json.dumps(C)
    if a[:2] == ("image", "ls"):
        return "postgres:16-alpine sha256:new\nollama/ollama:latest sha256:oll\n"
    if a[:2] == ("image", "inspect"):
        assert sorted(a[2:]) == ["sha256:ci", "sha256:new", "sha256:old", "sha256:oll"], a   # one call for all
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
check("build time of the running image, empty when Docker names none", (pg["created"], ol["created"]),
      ("2026-08-14T02:11:09.512433187Z", ""))
check("source revision from the image label, empty without one", (pg["revision"], ol["revision"]),
      ("35b2a975d74519de0678486bdbd46f644533b710", ""))
check("CI job container marked, the others not",
      (rows["FORGEJO-ACTIONS-TASK-2415_WORKFLOW-a545a355_JOB-unit"]["ci_job"], pg["ci_job"], ol["ci_job"]),
      (True, False, False))
ci = inv.is_ci_job
check("CI jobs: by name (Gitea too), by the act volume, never a compose service",
      [ci({"Name": "/GITEA-ACTIONS-TASK-7_WORKFLOW-x_JOB-build", "Config": {"Labels": {}}}),
       ci({"Name": "/job-svc", "Config": {"Labels": None}, "Mounts": [{"Destination": "/var/run/act"}]}),
       ci({"Name": "/FORGEJO-ACTIONS-TASK-1_x", "Config": {"Labels": COMPOSE}}),
       ci({"Name": "/forgejo-runner", "Config": {"Labels": {}}, "Mounts": [{"Destination": "/data"}]}),
       ci({"Name": "/FORGEJO-ACTIONS-TASK-x", "Config": {}})],
      [True, True, False, False, False])
print("\nALL CHECKS PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

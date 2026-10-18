#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Offline tests for lib/diun-update.py: Docker, docker compose and systemctl
are replaced by fakes, /var/backups/image-updates and the other paths by a
temporary directory. Covers database detection, plan reasons and the unknown jump
size, argument checks and exit codes, the update flow (dump, rollback tag,
versions in the history, cleanup, radar start), the most likely cause of a
failure, the kept output and the digests of the new build, what an update
touches besides its image (jobs, depends_on, dependents, shared networks) and
the watch of dependent services, rollback, the cleanup rules and the radar
verb.
No Docker, no root, no network."""
import contextlib
import fcntl
import gzip
import hashlib
import importlib.util
import inspect as pyinspect
import io
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
spec = importlib.util.spec_from_file_location("upd", os.path.join(ROOT, "diun-update.py"))
U = importlib.util.module_from_spec(spec)
spec.loader.exec_module(U)

TMP = tempfile.mkdtemp(prefix="diun-update-test-")
U.BASE = os.path.join(TMP, "backups")
U.DUMPS = os.path.join(U.BASE, "dumps")
U.OPS = os.path.join(U.BASE, "operations.jsonl")
U.LOGS = os.path.join(U.BASE, "logs")
U.LOCK = os.path.join(TMP, "update.lock")
U.CONF = os.path.join(TMP, "image-updates.conf")
U.VERSIONS = os.path.join(TMP, "versions.json")
U.show_logs = lambda names: None
REAL_WAIT = U.wait_healthy
REAL_STREAM = U.stream
LOGS = {}                            # container -> log lines for the failure reason
U.container_logs = lambda name, n=80: LOGS.get(name, [])

# One operation per second at most in real life; here every update gets its
# own stamp, one second apart, starting an hour ago.
_t0 = datetime.now().replace(microsecond=0) - timedelta(hours=1)
_n = iter(range(1, 100000))
U.stamp = lambda: (_t0 + timedelta(seconds=next(_n))).strftime("%Y%m%d-%H%M%S")


class _Time:
    """time for the module under test, without waiting."""
    def __getattr__(self, name):
        return getattr(time, name)

    @staticmethod
    def sleep(seconds):
        pass


U.time = _Time()

fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(("OK   " if ok else "FAIL ") + label + ("" if ok else f"\n     got  {got!r}\n     want {want!r}"))


def quiet(fn, *args, **kw):
    """Run fn, return (exit code or Abort code, captured output)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            rc = fn(*args, **kw)
        except U.Abort as e:
            print(str(e))
            rc = e.code
    return rc, out.getvalue()


def release_lock():
    if U._lock_fh:
        U._lock_fh.close()
        U._lock_fh = None


@contextlib.contextmanager
def as_uid(uid):
    real = os.geteuid
    os.geteuid = lambda: uid
    try:
        yield
    finally:
        os.geteuid = real


def reset_fs():
    shutil.rmtree(U.BASE, ignore_errors=True)
    for p in (U.CONF, U.VERSIONS):
        if os.path.exists(p):
            os.unlink(p)
    U.ensure_base()


def touch_dump(rel, data=b"-- dump\n"):
    p = os.path.join(U.DUMPS, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with gzip.open(p, "wb") as f:
        f.write(data)
    return rel


def dumps_of(project):
    d = os.path.join(U.DUMPS, project)
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def ops():
    return U.load_ops()


def last_result(out):
    return json.loads([ln for ln in out.splitlines() if ln.startswith("@@RESULT ")][-1][9:])


def history(op_id):
    """The operation as the backups tab gets it."""
    return [o for o in json.loads(quiet(U.cmd_backups)[1])["ops"] if o["id"] == op_id][0]


def interrupted(argv):
    """main(argv) ended by KeyboardInterrupt, as a kill ends a run (without
    its last lines): the output up to there."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), as_uid(0):
        try:
            U.main(argv)
        except KeyboardInterrupt:
            pass
    release_lock()
    return out.getvalue()


# ------------------------------------------------------------- fake Docker
L = {"project": U.L_PROJECT, "service": U.L_SERVICE, "workdir": U.L_WORKDIR,
     "files": U.L_FILES, "hash": U.L_HASH}
COMPOSE_DIR = os.path.join(TMP, "glitchtip")
os.makedirs(COMPOSE_DIR)
open(os.path.join(COMPOSE_DIR, "compose.yml"), "w").close()


def ctn(name, ref, img, project=None, service=None, cmd=None, entrypoint=None, env=(),
        running=True, files=None, networks=(), env_files=None):
    labels = {}
    if project:
        labels = {L["project"]: project, L["service"]: service or name, L["workdir"]: COMPOSE_DIR,
                  L["files"]: files or os.path.join(COMPOSE_DIR, "compose.yml"),
                  L["hash"]: "h-" + (service or name)}
        if env_files:
            labels[U.L_ENVFILES] = env_files
    return {"Name": "/" + name, "Image": img,
            "Config": {"Image": ref, "Labels": labels, "Env": list(env), "Cmd": cmd, "Entrypoint": entrypoint},
            "State": {"Running": running, "Status": "running" if running else "exited"},
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
            "NetworkSettings": {"Networks": {n: {} for n in networks}}}


class Docker:
    def __init__(self):
        self.containers, self.images, self.tags = {}, {}, {}
        self.cfg = {"services": {}}
        self.compose_err = None
        self.hashes = {}
        self.calls, self.streamed = [], []
        self.rmi_refuse = set()
        self.pull_to = None          # (ref, image id) the next pull moves the tag to
        self.up_rc = 0
        self.up_out = []             # what a failing `compose up` prints
        self.images_from = None      # () -> {service: image} read from the files, as compose does
        self.pull_rc = 0             # exit code of `compose pull` on a service the files do not define
        self.registry_down = False   # the registry fails: `compose pull` exits 1, or 0 with
                                     # --ignore-pull-failures (docker/compose pkg/compose/pull.go)
        self.pull_raises = None      # exception the pull raises (the helper itself stops there)
        self.up_runs = None          # image ID recreated containers run, whatever the tag names
        self.up_raises = None        # exception `compose up` raises (diunproc.Timeout: it did not end)
        self.undefined = set()       # services the compose files no longer define

    def add(self, c):
        self.containers[c["Name"].lstrip("/")] = c

    def handle(self, cmd):
        if cmd[:2] == ["docker", "compose"]:
            if "config" not in cmd:
                return "", 0, ""
            if self.compose_err:
                return "", 1, self.compose_err
            if "--hash" in cmd:
                svcs = cmd[cmd.index("--hash") + 1].split(",")
                return "".join(f"{s} {self.hashes.get(s, 'h-' + s)}\n" for s in svcs), 0, ""
            if self.images_from:
                return json.dumps({"services": {s: {"image": i} for s, i in self.images_from().items()}}), 0, ""
            return json.dumps(self.config_of(cmd)), 0, ""
        if cmd[:2] == ["docker", "inspect"]:
            names = cmd[4:] if cmd[2:4] == ["--type", "container"] else cmd[2:]
            found = [self.containers[n] for n in names if n in self.containers]
            if len(found) != len(names):
                return "", 1, "Error: No such object"
            return json.dumps(found), 0, ""
        if cmd[:3] == ["docker", "ps", "-q"]:
            return "".join(n + "\n" for n, c in self.containers.items() if c["State"]["Running"]), 0, ""
        if cmd[:3] == ["docker", "ps", "-a"]:
            filters = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--filter"]
            want = [f[len("label="):].split("=", 1) for f in filters if f.startswith("label=")]
            names = [n for n, c in self.containers.items()
                     if all((c["Config"]["Labels"] or {}).get(k) == v for k, v in want)]
            net = [f[len("network="):] for f in filters if f.startswith("network=")]
            if net:
                names = [n for n in names if net[0] in self.containers[n]["NetworkSettings"]["Networks"]]
                return "".join(f"{n}\t{(self.containers[n]['Config']['Labels'] or {}).get(U.L_PROJECT, '')}\n"
                               for n in names), 0, ""
            return "".join(n + "\n" for n in names), 0, ""
        if cmd[:3] == ["docker", "image", "inspect"]:
            if cmd[3:5] == ["--format", "{{.Id}}"]:
                iid = self.tags.get(U.norm_ref(cmd[5]))
                return (iid + "\n", 0, "") if iid else ("", 1, "No such image")
            imgs = [self.images[i] for i in cmd[3:] if i in self.images]
            return (json.dumps(imgs), 0, "") if len(imgs) == len(cmd[3:]) else ("", 1, "No such image")
        if cmd[:3] == ["docker", "image", "ls"]:
            lines = []
            for ref, iid in sorted(self.tags.items()):
                repo, tag = ref.rsplit(":", 1)
                lines.append(json.dumps({"Repository": repo, "Tag": tag, "ID": iid, "Size": "100MB",
                                         "CreatedAt": "2026-01-01 00:00:00 +0000 UTC"}))
            return "".join(x + "\n" for x in lines), 0, ""
        if cmd[:2] == ["docker", "tag"]:
            self.tags[U.norm_ref(cmd[3])] = self.tags.get(U.norm_ref(cmd[2]), cmd[2])
            return "", 0, ""
        if cmd[:2] == ["docker", "rmi"]:
            ref = U.norm_ref(cmd[2])
            if ref in self.rmi_refuse or ref not in self.tags:
                return "", 1, "Error response from daemon: conflict: image is being used"
            del self.tags[ref]
            return f"Untagged: {ref}\n", 0, ""
        if cmd[:2] == ["docker", "exec"]:
            return "52428800\n", 0, ""
        raise AssertionError(cmd)

    def config_of(self, cmd):
        """As compose: the services of the files given with -f (those of the
        containers started from them) besides what the test set in cfg."""
        files = ",".join(cmd[i + 1] for i, a in enumerate(cmd) if a == "-f")
        svcs = dict(self.cfg.get("services") or {})
        for c in self.containers.values():
            lbl = c["Config"]["Labels"] or {}
            svc = lbl.get(U.L_SERVICE)
            if svc and lbl.get(U.L_FILES) == files and svc not in svcs:
                svcs[svc] = {"image": c["Config"]["Image"]}
        for svc in self.undefined:
            svcs.pop(svc, None)
        return {**self.cfg, "services": svcs}

    def run(self, cmd, check=True, capture=True, timeout=600):
        self.calls.append(list(cmd))
        out, rc, err = self.handle(list(cmd))
        if check and rc != 0:
            raise subprocess.CalledProcessError(rc, cmd, out, err)
        return out

    def stream(self, cmd):
        self.streamed.append(list(cmd))
        if "pull" in cmd and self.pull_raises:
            raise self.pull_raises
        if "pull" in cmd and self.pull_rc:
            U.STREAM_TAIL[:] = ["no such service: " + cmd[-1]]
            return self.pull_rc
        if "pull" in cmd and self.registry_down:
            U.STREAM_TAIL[:] = [" postgres Pulling ",
                                ' postgres Error Get "https://registry-1.docker.io/v2/": dial tcp: lookup '
                                "registry-1.docker.io: no such host"]
            return 0 if "--ignore-pull-failures" in cmd else 1
        if "pull" in cmd and self.pull_to:
            ref, iid = self.pull_to
            self.tags[U.norm_ref(ref)] = iid
        if "up" in cmd and self.up_raises:
            raise self.up_raises
        if "up" in cmd:
            if self.up_rc:
                U.STREAM_TAIL[:] = self.up_out
                return self.up_rc
            refs = self.images_from() if self.images_from else {}
            for c in self.containers.values():
                svc = (c["Config"]["Labels"] or {}).get(U.L_SERVICE)
                if svc in cmd and svc in refs:
                    c["Config"]["Image"] = refs[svc]
                iid = self.tags.get(U.norm_ref(c["Config"]["Image"]))
                if iid and svc in cmd:
                    c["Image"] = self.up_runs or iid
        return 0


D = None


def fresh_docker():
    global D
    D = Docker()
    U.run = D.run
    U.stream = D.stream
    return D


class Systemctl:
    def __init__(self, installed=True, start_rc=0):
        self.installed, self.start_rc, self.calls = installed, start_rc, []

    def __call__(self, *args, timeout=60):
        self.calls.append(list(args))
        rc = 0
        if args[0] == "cat":
            rc = 0 if self.installed else 1
        elif args[0] == "start":
            rc = self.start_rc
        return subprocess.CompletedProcess(["systemctl", *args], rc, "", "Job failed" if rc else "")


class FakePopen:
    """docker exec <db> sh -c <dump script>, as diunproc.copy runs it: the
    data to stdout, the exit code (script: {container: (data, exit code)})."""
    script = {}

    @staticmethod
    def copy(cmd, sink, limit, errfile, grace=10, keep_fds=()):
        data, rc = FakePopen.script[cmd[2]]
        if data:
            sink(data)
        if rc:
            errfile.write(b"mariadb-dump: Got error: 1045: Access denied for user 'root'@'localhost'")
        return rc


U.copy_out = FakePopen.copy

# ------------------------------------------------------- database detection
k = U.kind_of
check("kind: postgres server", k(ctn("a", "postgres:16-alpine", "x", cmd=["postgres"])), "postgres")
check("kind: postgres with options",
      k(ctn("a", "postgres:16", "x", entrypoint=["docker-entrypoint.sh"], cmd=["postgres", "-c", "max_connections=200"])),
      "postgres")
check("kind: helper with the postgres image",
      k(ctn("a", "postgres:16", "x", entrypoint=["sh", "-c"], cmd=["pg_dump -h db app > /b/x.sql"])), None)
check("kind: renamed image with PG_MAJOR", k(ctn("a", "acme/db:3", "x", env=["PG_MAJOR=16"], cmd=["postgres"])), "postgres")
check("kind: mariadb", k(ctn("a", "mariadb:11", "x", cmd=["mariadbd"])), "mysql")
# command: with options only, as Nextcloud's compose example has it (2026-10-16):
# the image's docker-entrypoint.sh puts mariadbd / postgres in front of them
check("kind: mariadb with options only",
      k(ctn("a", "mariadb:11", "x", entrypoint=["docker-entrypoint.sh"],
            cmd=["--transaction-isolation=READ-COMMITTED", "--log-bin=binlog", "--binlog-format=ROW"])), "mysql")
check("kind: postgres with options only",
      k(ctn("a", "postgres:16", "x", entrypoint=["/usr/local/bin/docker-entrypoint.sh"], cmd=["-c", "max_connections=200"])),
      "postgres")
check("kind: options for another entrypoint are no server",
      k(ctn("a", "postgres:16", "x", entrypoint=["pg_basebackup"], cmd=["-h", "db", "-D", "/backup"])), None)
check("kind: mysql 8", k(ctn("a", "mysql:8", "x", env=["MYSQL_MAJOR=8.4"], cmd=["mysqld"])), "mysql")
check("kind: redis has no dump", k(ctn("a", "redis:7-alpine", "x", cmd=["redis-server"])), "unsupported")
check("kind: nginx is no database", k(ctn("a", "nginx:1.27", "x", cmd=["nginx", "-g", "daemon off;"])), None)

# ------------------------------------------------------------- plan reasons
reset_fs()
fresh_docker()
D.add(ctn("solo", "nginx:1.27", "sha256:n"))
D.add(ctn("gone", "nginx:1.27", "sha256:n", project="gone", files="/nonexistent/compose.yml"))
D.add(ctn("pinned", "nginx@sha256:abc", "sha256:n", project="glitchtip", service="pinned"))
D.add(ctn("built", "glitchtip-custom:dev", "sha256:b", project="glitchtip", service="built"))
D.cfg = {"services": {"built": {"build": {"context": "."}}}}


def plan(name):
    with contextlib.redirect_stdout(io.StringIO()):
        return U.make_plan(name)[0]


p = plan("solo")
check("plan: not-compose", (p["updatable"], p["reason"]), (False, "not-compose"))
p = plan("gone")
check("plan: compose-file-missing", (p["reason"], p["missing"]), ("compose-file-missing", ["/nonexistent/compose.yml"]))
check("plan: digest-pinned", plan("pinned")["reason"], "digest-pinned")
check("plan: built-locally", plan("built")["reason"], "built-locally")
D.compose_err = "services.web.image: invalid reference format"
p = plan("built")
check("plan: compose-config-error", (p["reason"], p["detail"]), ("compose-config-error", D.compose_err))

# One project, two compose files started one by one, one image (a CI host,
# 2026-10-16: docker-compose.yml with runner, runner-2, runner-3 and
# docker-compose.emu.yml with runner-emu). Compose knows only the services of
# the files it is given: an update takes only what shares them.
D.compose_err = None
D.cfg = {"services": {}}
MAIN, EMU = os.path.join(COMPOSE_DIR, "compose.yml"), os.path.join(COMPOSE_DIR, "compose.emu.yml")
open(EMU, "w").close()
for name, svc, files in (("forgejo-runner", "runner", MAIN), ("forgejo-runner-2", "runner-2", MAIN),
                         ("forgejo-runner-emu", "runner-emu", EMU)):
    D.add(ctn(name, "data.forgejo.org/forgejo/runner:12", "sha256:run", project="forgejo-runner", service=svc,
              files=files))
p = plan("forgejo-runner")
check("plan: only the services of the same compose files", (p["updatable"], p["services"], p["containers"]),
      (True, ["runner", "runner-2"], ["forgejo-runner", "forgejo-runner-2"]))
p = plan("forgejo-runner-emu")
check("plan: the other file, its own service", (p["updatable"], p["services"], p["containers"]),
      (True, ["runner-emu"], ["forgejo-runner-emu"]))
D.undefined = {"runner-2"}
p = plan("forgejo-runner")
check("plan: a service the compose files no longer define", (p["updatable"], p["reason"], p["missing"]),
      (False, "service-missing", ["runner-2"]))
D.undefined = set()
# The plan of a runner names the CI jobs that run now: they break off when it
# is recreated (2026-10-17). On the shared network they are no "project".
for n in ("forgejo-runner", "forgejo-runner-2"):
    D.containers[n]["NetworkSettings"]["Networks"] = {"edge": {}}
D.add(ctn("caddy", "caddy:2", "sha256:cd", project="caddy", networks=("edge",)))
JOB = "FORGEJO-ACTIONS-TASK-2415_WORKFLOW-a545a355_JOB-unit"
D.add(ctn(JOB, "sanctum-ci:1", "sha256:ci", networks=("edge",)))
D.containers[JOB]["State"]["StartedAt"] = "2026-10-17T10:35:00Z"
D.add(ctn("FORGEJO-ACTIONS-TASK-2399_WORKFLOW-b1_JOB-done", "sanctum-ci:1", "sha256:ci", running=False))
p = plan("forgejo-runner")
check("plan: a runner names the CI jobs that run, not the finished ones", p["ci_jobs"],
      [{"name": JOB, "image": "sanctum-ci:1", "started": "2026-10-17T10:35:00Z"}])
check("plan: CI jobs are left out of the shared network", p["shared_networks"], [{"network": "edge", "projects": ["caddy"]}])

fresh_docker()
PG_ENV = ["PG_MAJOR=16", "PG_VERSION=16.14"]
D.images = {"sha256:old": {"Id": "sha256:old", "Config": {"Env": PG_ENV}},
            "sha256:new": {"Id": "sha256:new", "Config": {"Env": ["PG_MAJOR=16", "PG_VERSION=16.15"]}},
            "sha256:web": {"Id": "sha256:web", "Config": {"Env": ["GLITCHTIP_VERSION=6.1.8"]}}}
D.tags = {"postgres:16-alpine": "sha256:new", "glitchtip/glitchtip:latest": "sha256:web"}
D.add(ctn("glitchtip-postgres", "postgres:16-alpine", "sha256:old", project="glitchtip", service="postgres",
          cmd=["postgres"], env=PG_ENV))
D.add(ctn("glitchtip-migrate", "postgres:16-alpine", "sha256:old", project="glitchtip", service="migrate",
          entrypoint=["sh", "-c"], cmd=["psql -f /m.sql"], env=PG_ENV, running=False))
D.add(ctn("glitchtip-redis", "redis:7-alpine", "sha256:r", project="glitchtip", service="redis", cmd=["redis-server"]))
D.add(ctn("glitchtip-web", "glitchtip/glitchtip:latest", "sha256:web", project="glitchtip", service="web"))
D.hashes = {"migrate": "h-changed"}
p = plan("glitchtip-postgres")
check("plan: updatable", (p["updatable"], p["reason"]), (True, None))
check("plan: services with the same image", (p["services"], p["containers"]),
      (["migrate", "postgres"], ["glitchtip-migrate", "glitchtip-postgres"]))
check("plan: databases of the project", sorted((d["name"], d["kind"], d["size"]) for d in p["dbs"]),
      [("glitchtip-postgres", "postgres", 52428800), ("glitchtip-redis", "unsupported", None)])
check("plan: compose changed + pulled", (p["config_changed"], p["pulled"]), (True, True))
check("plan: no runner, no look for CI jobs", (p["ci_jobs"], ["docker", "ps", "-q"] in D.calls), ([], False))
check("plan: version from the variable, the tag fixes one block -> size of the jump known",
      (p["installed_version"], p["tag_digits"], p["jump_unknown"]), ("16.14", 1, False))
check("plan: pulled already -> the target is the pulled image (16.14 -> 16.15)",
      (p["target_version"], p["jump"]), ("16.15", "minor"))
check("jump sizes", [U.jump_of(a, b) for a, b in (("5.0.2", "6.0.2"), ("5.0.2", "5.4.5"), ("5.0.2", "5.0.3"),
                                                   ("16.14", "16.15"), ("6.1.8", "6.1.8"), (None, "6.0.2"), ("x", "1"))],
      ["major", "minor", "patch", "minor", "same", None, None])
D.images["sha256:fb"] = {"Id": "sha256:fb", "Config": {"Env": ["NODE_VERSION=24.21.0"]}}
D.add(ctn("formbricks-app", "ghcr.io/formbricks/formbricks:latest", "sha256:fb", project="glitchtip",
          service="formbricks"))
p = plan("formbricks-app")
check("plan: latest + unknown version -> size of the jump unknown",
      (p["installed_version"], p["tag_digits"], p["jump_unknown"]), (None, 0, True))
with open(U.VERSIONS, "w") as f:
    json.dump({"images": [{"image": "ghcr.io/formbricks/formbricks", "tag": "latest",
                           "installed": {"sha256:fb": {"version": "5.0.2", "src": "digest"}}}]}, f)
p = plan("formbricks-app")
check("plan: latest, version known to the radar -> no warning", (p["installed_version"], p["jump_unknown"]),
      ("5.0.2", False))
check("plan: no tag_now in the radar -> target unknown", (p["target_version"], p["jump"]), (None, None))
with open(U.VERSIONS, "w") as f:
    json.dump({"images": [{"image": "ghcr.io/formbricks/formbricks", "tag": "latest",
                           "installed": {"sha256:fb": {"version": "5.0.2", "src": "digest"}},
                           "versions": {"5.0.2": {"major": "6.0.2", "status": "major"}},
                           "tag_now": {"digest": "sha256:t602", "version": "6.0.2", "tag": "6.0.2"}}]}, f)
p = plan("formbricks-app")
check("plan: latest points to 6.0.2 now -> a major jump", (p["target_version"], p["jump"]), ("6.0.2", "major"))
D.images["sha256:fb"]["RepoDigests"] = ["ghcr.io/formbricks/formbricks@sha256:t602"]
p = plan("formbricks-app")
check("plan: latest still points to the running build -> same", (p["target_version"], p["jump"]), ("5.0.2", "same"))
del D.containers["formbricks-app"]
# pgvector:pg17 runs PostgreSQL 17.10 (PG_VERSION) while its tags count
# pgvector releases: pg17 = 0.8.7-pg17-bookworm is no jump from 17.10.
PGV_ENV = ["PG_MAJOR=17", "PG_VERSION=17.10"]
D.images["sha256:pgv"] = {"Id": "sha256:pgv", "Config": {"Env": PGV_ENV}}
D.add(ctn("formbricks-db", "pgvector/pgvector:pg17", "sha256:pgv", project="glitchtip", service="db",
          cmd=["postgres"], env=PGV_ENV))
with open(U.VERSIONS, "w") as f:
    json.dump({"images": [{"image": "pgvector/pgvector", "tag": "pg17",
                           "installed": {"sha256:pgv": {"version": "17.10", "src": "env:PG_VERSION"}},
                           "versions": {"17.10": {"status": "unknown"}},
                           "tag_now": {"digest": "sha256:t087", "version": "0.8.7", "tag": "0.8.7-pg17-bookworm"}}]}, f)
p = plan("formbricks-db")
check("plan: the tag points to a version in another numbering -> no target, no jump",
      (p["installed_version"], p["target_version"], p["jump"], p["jump_unknown"]), ("17.10", None, None, False))
check("same series: only a version the radar found among the tags",
      [U.same_series(r, "5.0.2") for r in ({"versions": {"5.0.2": {"status": "minor"}}},
                                            {"versions": {"5.0.2": {"status": "current"}}},
                                            {"versions": {"5.0.2": {"status": "unknown"}}},
                                            {"versions": {}}, {}, None)],
      [True, True, False, False, False, False])
os.unlink(U.VERSIONS)
del D.containers["formbricks-db"]

# ------------------------------------------- what else an update touches
fresh_docker()
D.add(ctn("gt-web", "app:1", "sha256:a1", project="gt", service="web", networks=("gt_default", "mailrelay")))
D.add(ctn("gt-worker", "app:1", "sha256:a1", project="gt", service="worker", networks=("gt_default",)))
D.add(ctn("gt-migrate", "app:1", "sha256:a1", project="gt", service="migrate", running=False))
D.containers["gt-migrate"]["HostConfig"]["RestartPolicy"]["Name"] = "on-failure"
D.add(ctn("gt-db", "postgres:16", "sha256:p", project="gt", service="db", cmd=["postgres"]))
D.add(ctn("gt-side", "side:2", "sha256:s", project="gt", service="side"))
D.add(ctn("smtp", "boky/postfix:latest", "sha256:m", project="smtp-relay", service="smtp", networks=("mailrelay",)))
D.add(ctn("lonely", "x:1", "sha256:x", networks=("mailrelay",)))
D.cfg = {"services": {
    "web": {"depends_on": {"db": {"condition": "service_healthy"},
                           "migrate": {"condition": "service_completed_successfully"}}},
    "worker": {"depends_on": {"db": {"condition": "service_started"}}},
    "migrate": {"depends_on": {"db": {"condition": "service_healthy"}}},
    "db": {}, "side": {"depends_on": {"web": {"condition": "service_started"}}}}}
p = plan("gt-web")
check("relations: one-off job among the same image", (p["services"], p["jobs"]),
      (["migrate", "web", "worker"], ["gt-migrate"]))
check("relations: what the services wait for", p["depends_on"], [{"service": "db", "condition": "service_healthy"}])
check("relations: who waits for them (watched)", p["dependents"], [{"service": "side", "containers": ["gt-side"]}])
check("relations: a service that does not wait for the job", p["job_order"], [{"job": "migrate", "services": ["worker"]}])
check("relations: other projects on a shared network",
      p["shared_networks"], [{"network": "mailrelay", "projects": ["lonely", "smtp-relay"]}])
D.cfg = {"services": {}}
check("relations: without depends_on nothing waits, so both services get the warning",
      [plan("gt-web")[k] for k in ("depends_on", "dependents", "job_order")],
      [[], [], [{"job": "migrate", "services": ["web", "worker"]}]])

# watching the dependent services after the update
D.containers["gt-side"]["State"] = {"Running": True, "StartedAt": "2026-10-13T10:00:00Z", "RestartCount": 0}
before = U.running_state(["gt-side", "gt-migrate", "nope"])
check("watch: only containers that run now", before, {"gt-side": ("2026-10-13T10:00:00Z", 0)})
check("watch: keeps running -> nothing failed", quiet(U.watch_dependents, before)[0], [])
D.containers["gt-side"]["State"]["RestartCount"] = 1
check("watch: a new restart counts as failed", quiet(U.watch_dependents, before)[0], ["gt-side"])
D.containers["gt-side"]["State"].update(RestartCount=0, StartedAt="2026-10-13T10:05:00Z")
check("watch: a restart without the counter (new start time) too", quiet(U.watch_dependents, before)[0], ["gt-side"])
D.containers["gt-side"]["State"].update(StartedAt="2026-10-13T10:00:00Z", Health={"Status": "unhealthy"})
check("watch: unhealthy", quiet(U.watch_dependents, before)[0], ["gt-side"])
check("watch: nothing to watch", quiet(U.watch_dependents, {})[0], [])
del D.containers["gt-side"]["State"]["Health"]

# an update whose dependent service fails afterwards fails as a whole
D.cfg = {"services": {"side": {"depends_on": {"web": {"condition": "service_started"}}}}}
D.tags = {"app:1": "sha256:a2"}
D.images = {"sha256:a2": {"Id": "sha256:a2", "Config": {"Env": []}, "RepoDigests": ["app@sha256:abc"]}}
U.wait_healthy = lambda project, services: (True, [], {})
real_watch, seen = U.watch_dependents, []
U.watch_dependents = lambda before: seen.append(before) or ["gt-side"]
LOGS["gt-side"] = ["Error: lost the connection to web"]
rc, out = quiet(U.cmd_update, "gt-web", do_dump=False)
release_lock()
res = json.loads(out.strip().splitlines()[-1][9:])
check("dependent fails after the update -> exit 4, its cause",
      (rc, res["status"], res["reason"]["container"], res["reason"]["line"]),
      (4, "failed", "gt-side", "Error: lost the connection to web"))
check("the dependent was watched with its state from before the update", list(seen[0]), ["gt-side"])
U.watch_dependents = real_watch
LOGS.clear()
reset_fs()

# -------------------------------------------------- most likely cause
NODE_LOG = [
    "    {",
    "      path: 'AUTHZED_CONSISTENCY',",
    "      message: 'Formbricks v6 requires AUTHZED_CONSISTENCY=fully_consistent'",
    "    }",
    "  ]",
    "}",
    "file:///home/nextjs/validate-env.mjs:4307",
    "  throw new Error(message);",
    "        ^",
    "",
    "Error: Invalid environment variables:",
    "  - AUTHZED_ENABLED: Formbricks v6 requires AUTHZED_ENABLED=true; configure SpiceDB first.",
    "  - AUTHZED_TOKEN: AUTHZED_TOKEN is required when AuthZed is enabled",
    "    at throwEnvValidationError (file:///home/nextjs/validate-env.mjs:4307:9)",
    "    at file:///home/nextjs/validate-env.mjs:4878:3",
    "",
    "Node.js v24.21.0",
]
PY_LOG = [
    "Traceback (most recent call last):",
    '  File "/code/apps/issue_events/tasks.py", line 12, in cleanup_old_issues',
    "    Issue.objects.filter(index__isnull=False).delete()",
    'RuntimeError: [42P01] relation "issue_events_issueindex" does not exist',
    "",
    "The above exception was the direct cause of the following exception:",
    "",
    "Traceback (most recent call last):",
    '  File "/code/manage.py", line 3, in <module>',
    "    raise dj_exc_value.with_traceback(traceback) from exc_value",
    'django.db.utils.ProgrammingError: [42P01] relation "issue_events_issueindex" does not exist',
    'SQL: SELECT "issue_events_issue"."id" FROM "issue_events_issue"',
]
PG_LOG = ["2026-10-12 23:13:56.303 UTC [1] LOG:  starting PostgreSQL 17.2",
          "2026-10-12 23:13:56.310 UTC [1] FATAL:  database files are incompatible with server",
          "2026-10-12 23:13:56.310 UTC [1] DETAIL:  The data directory was initialized by PostgreSQL version 16."]
GO_LOG = ["panic: runtime error: invalid memory address or nil pointer dereference",
          "[signal SIGSEGV: segmentation violation code=0x1 addr=0x0 pc=0x4a3b2c]", "",
          "goroutine 1 [running]:", "main.main()", "\t/src/main.go:12 +0x1d"]
r = U.reason_line
check("cause: Node error with its first detail",
      r(NODE_LOG), "Error: Invalid environment variables: - AUTHZED_ENABLED: Formbricks v6 requires "
                   "AUTHZED_ENABLED=true; configure SpiceDB first.")
check("cause: last Python exception", r(PY_LOG),
      'django.db.utils.ProgrammingError: [42P01] relation "issue_events_issueindex" does not exist')
check("cause: Postgres FATAL", r(PG_LOG),
      "2026-10-12 23:13:56.310 UTC [1] FATAL:  database files are incompatible with server")
check("cause: Go panic, not the goroutine dump", r(GO_LOG),
      "panic: runtime error: invalid memory address or nil pointer dereference")
check("cause: a log line that only mentions a problem",
      r(["starting", 'level=error msg="dial tcp 10.0.0.3:6379: connect: connection refused"', "exiting"]),
      'level=error msg="dial tcp 10.0.0.3:6379: connect: connection refused"')
check("cause: colour codes removed", r(["\x1b[31mERROR\x1b[0m: boom"]), "ERROR: boom")
check("cause: stack frames alone are no cause", r(["    at main (app.js:1:1)", "", "  ^"]), None)
check("cause: nothing telling", r(["listening on :3000", "ready"]), None)
check("cause: long lines are cut", len(r(["Error: " + "x" * 1000])), U.REASON_MAX)

fresh_docker()


def failing(name, state, health=None):
    c = ctn(name, "app:1", "sha256:a", project="p", service=name)
    c["State"] = dict(state)
    if health:
        c["State"]["Health"] = health
    D.add(c)


failing("fb", {"Status": "restarting", "ExitCode": 1, "RestartCount": 3})
failing("web", {"Status": "running"}, {"Status": "unhealthy", "Log": [
    {"ExitCode": 7, "Output": "curl: (7) Failed to connect to localhost port 8000\n"}]})
failing("oom", {"Status": "exited", "ExitCode": 137, "OOMKilled": True})
failing("slow", {"Status": "running"}, {"Status": "starting", "Log": []})
LOGS.update(fb=NODE_LOG, oom=["Killed"], slow=["migrating ..."])
fr = U.failure_reason
check("cause of a restart loop, from the log", fr(["fb"]),
      {"container": "fb", "state": "restarting", "exit_code": 1, "oom": False, "source": "log",
       "line": r(NODE_LOG)})
check("cause of an unhealthy container, from the health check",
      (fr(["web"])["source"], fr(["web"])["state"], fr(["web"])["line"]),
      ("health", "unhealthy", "curl: (7) Failed to connect to localhost port 8000"))
check("out of memory without a log line", (fr(["oom"])["oom"], fr(["oom"])["line"], fr(["oom"])["exit_code"]),
      (True, None, 137))
check("still starting at the timeout", (fr(["slow"])["state"], fr(["slow"])["line"]), ("starting", None))
check("the first container with a telling line wins", fr(["oom", "fb"])["container"], "fb")
check("unknown containers are skipped", fr(["nope"]), None)
U.STREAM_TAIL[:] = ["Container x Recreate",
                    "Error response from daemon: Bind for 0.0.0.0:8080 failed: port is already allocated"]
check("cause of a failed compose up", fr(["fb"], compose_rc=1),
      {"container": None, "state": None, "exit_code": 1, "oom": False, "source": "compose",
       "line": "Error response from daemon: Bind for 0.0.0.0:8080 failed: port is already allocated"})
LOGS.clear()
fresh_docker()
D.images = {"sha256:old": {"Id": "sha256:old", "Config": {"Env": PG_ENV}},
            "sha256:new": {"Id": "sha256:new", "Config": {"Env": ["PG_MAJOR=16", "PG_VERSION=16.15"]}},
            "sha256:web": {"Id": "sha256:web", "Config": {"Env": ["GLITCHTIP_VERSION=6.1.8"]}}}
D.tags = {"postgres:16-alpine": "sha256:new", "glitchtip/glitchtip:latest": "sha256:web"}
D.add(ctn("glitchtip-postgres", "postgres:16-alpine", "sha256:old", project="glitchtip", service="postgres",
          cmd=["postgres"], env=PG_ENV))
D.add(ctn("glitchtip-migrate", "postgres:16-alpine", "sha256:old", project="glitchtip", service="migrate",
          entrypoint=["sh", "-c"], cmd=["psql -f /m.sql"], env=PG_ENV, running=False))
D.add(ctn("glitchtip-redis", "redis:7-alpine", "sha256:r", project="glitchtip", service="redis", cmd=["redis-server"]))
D.add(ctn("glitchtip-web", "glitchtip/glitchtip:latest", "sha256:web", project="glitchtip", service="web"))
D.hashes = {"migrate": "h-changed"}

# ----------------------------------------------------- arguments, exit codes


def main_rc(*argv, uid=0):
    with as_uid(uid):
        return quiet(U.main, list(argv))[0]


check("not root -> 77", main_rc("backups", uid=1000), 77)
check("unknown verb -> 64", main_rc("bogus"), 64)
check("update with a wrong flag -> 64", main_rc("update", "glitchtip-postgres", "--force"), 64)
check("cleanup with a wrong flag -> 64", main_rc("cleanup", "--all"), 64)
check("radar with an argument -> 64", main_rc("radar", "now"), 64)
check("plan: invalid name -> 64", main_rc("plan", "../etc"), 64)
check("plan: unknown container -> 2", main_rc("plan", "nope"), 2)
check("rollback: invalid id -> 64", main_rc("rollback", "20261012-0000-x;rm"), 64)
check("rollback: unknown operation -> 2", main_rc("rollback", "20261012-000000-nope"), 2)
check("delete-dump: path trick -> 64", main_rc("delete-dump", "glitchtip/../../../etc/20261012-000000_passwd.sql.gz"), 64)
check("delete-dump: missing -> 2", main_rc("delete-dump", "glitchtip/20261012-000000_glitchtip-postgres.sql.gz"), 2)
check("delete-image: normal tag -> 64", main_rc("delete-image", "postgres:16-alpine"), 64)
check("delete-image: missing -> 2", main_rc("delete-image", "postgres:rollback-20261012-000000"), 2)
release_lock()
D.cfg = {"services": {"web": {"build": "."}}}
check("update: not updatable -> 2", quiet(U.cmd_update, "glitchtip-web")[0], 2)
D.cfg = {"services": {}}
release_lock()
held = open(U.LOCK, "w")
U.fcntl.flock(held, U.fcntl.LOCK_EX | U.fcntl.LOCK_NB)
check("lock taken -> 75", quiet(U.cmd_update, "glitchtip-postgres")[0], 75)
held.close()
release_lock()

# ------------------------------------------------------------ update flow
U.systemctl = radar = Systemctl()
U.wait_healthy = lambda project, services: (True, [], {})
DUMP_OK = b"--\n-- PostgreSQL database cluster dump\n--\nhello\n-- PostgreSQL database cluster dump complete\n"
FakePopen.script = {"glitchtip-postgres": (DUMP_OK, 0)}

# Dump fails -> exit 5 before anything changes
FakePopen.script = {"glitchtip-postgres": (b"partial", 1)}
tags_before = dict(D.tags)
rc, out = quiet(U.cmd_update, "glitchtip-postgres")
release_lock()
check("dump fails -> exit 5", rc, 5)
check("dump fails -> no tag, no pull, no record",
      (D.tags == tags_before, D.streamed, os.path.exists(U.OPS)), (True, [], False))
check("dump fails -> no file left", dumps_of("glitchtip"), [])
FakePopen.script = {"glitchtip-postgres": (b"-- no end marker\n", 0)}
check("dump without end marker -> exit 5", quiet(U.cmd_update, "glitchtip-postgres")[0], 5)
release_lock()

# Two databases, the second dump fails (wrong password) -> no dump left at all
D.add(ctn("glitchtip-mariadb", "mariadb:11", "sha256:m", project="glitchtip", service="mariadb",
          cmd=["mariadbd"], env=["MARIADB_VERSION=1:11.4.5+maria~ubu2404"]))
FakePopen.script = {"glitchtip-postgres": (DUMP_OK, 0), "glitchtip-mariadb": (b"", 2)}
rc, out = quiet(U.cmd_update, "glitchtip-postgres")
release_lock()
check("second dump fails -> exit 5, the first dump removed again", (rc, dumps_of("glitchtip")), (5, []))
check("second dump fails -> message names the database",
      "Dump of glitchtip-mariadb failed (mariadb-dump: Got error: 1045" in out, True)
del D.containers["glitchtip-mariadb"]

# Too little space -> exit 5
FakePopen.script = {"glitchtip-postgres": (DUMP_OK, 0)}
U.FREE_MARGIN = 1 << 62
check("too little space -> exit 5", quiet(U.cmd_update, "glitchtip-postgres")[0], 5)
U.FREE_MARGIN = 1 << 30
release_lock()

# Successful update: 4 old dumps of this container, 5 of another project,
# old rollback images of postgres (unused) and redis (other image).
for day in range(1, 5):
    touch_dump(f"glitchtip/2026010{day}-030000_glitchtip-postgres.sql.gz")
for day in range(1, 6):
    touch_dump(f"other/2026010{day}-030000_other-db.sql.gz")
D.tags["postgres:rollback-20250101-000000"] = "sha256:ancient"
D.tags["redis:rollback-20250101-000000"] = "sha256:ancient-redis"
D.pull_to = None                    # the new image was pulled already (tag -> sha256:new)
rc, out = quiet(U.cmd_update, "glitchtip-postgres")
release_lock()
res = json.loads([ln for ln in out.splitlines() if ln.startswith("@@RESULT ")][-1][9:])
op = ops()[res["op"]]
check("update ok -> exit 0", (rc, res["status"]), (0, "ok"))
check("update: containers on the new image",
      [D.containers[n]["Image"] for n in ("glitchtip-postgres", "glitchtip-migrate")], ["sha256:new", "sha256:new"])
check("update: rollback tag on the old image", D.tags.get(U.norm_ref(res["rollback"])), "sha256:old")
check("update: up -d --no-deps --pull never for both services",
      [c[c.index("up"):] for c in D.streamed if "up" in c], [["up", "-d", "--no-deps", "--pull", "never", "migrate", "postgres"]])
with gzip.open(os.path.join(U.DUMPS, res["dumps"][0])) as f:
    check("update: dump written completely", f.read(), DUMP_OK)
check("update: dump readable by root only", oct(os.stat(os.path.join(U.DUMPS, res["dumps"][0])).st_mode & 0o777), "0o600")
check("history: versions 16.14 -> 16.15", (op["from_version"], op["to_version"]), ("16.14", "16.15"))
check("result: versions", (res["from_version"], res["to_version"]), ("16.14", "16.15"))
check("cleanup after update: 3 newest dumps of the container kept",
      dumps_of("glitchtip"), sorted([os.path.basename(res["dumps"][0]),
                                    "20260103-030000_glitchtip-postgres.sql.gz",
                                    "20260104-030000_glitchtip-postgres.sql.gz"]))
check("cleanup after update: other projects untouched", len(dumps_of("other")), 5)
check("cleanup after update: old postgres rollback image removed, redis kept",
      ("postgres:rollback-20250101-000000" in D.tags, "redis:rollback-20250101-000000" in D.tags), (False, True))
check("radar started without waiting", ["start", "--no-block", U.RADAR_UNIT] in radar.calls, True)
check("output names the radar", "Version radar started" in out, True)

# Nothing newer -> nochange, rollback tag removed again, no radar run
radar.calls.clear()
n_tags = len(D.tags)
rc, out = quiet(U.cmd_update, "glitchtip-postgres", do_dump=False)
release_lock()
res = json.loads(out.strip().splitlines()[-1][9:])
check("nochange -> exit 0", (rc, res["status"]), (0, "nochange"))
check("nochange: no rollback tag left", len(D.tags), n_tags)
check("nochange: versions equal", (ops()[res["op"]]["from_version"], ops()[res["op"]]["to_version"]), ("16.15", "16.15"))
check("nochange: radar not started", radar.calls, [])

# A pull that fails and leaves the old image is no "nothing newer": compose
# stops at once on a service its files do not define and pulls nothing
# (2026-10-16) -> exit 5, nothing left behind, no history entry
D.pull_rc = 1
D.streamed.clear()
tags_before, dumps_before, ops_before = dict(D.tags), dumps_of("glitchtip"), ops()
rc, out = quiet(U.cmd_update, "glitchtip-postgres")
release_lock()
check("failed pull, old image -> exit 5", rc, 5)
check("failed pull: no rollback tag, no dump, no history entry, nothing recreated",
      (D.tags == tags_before, dumps_of("glitchtip"), ops() == ops_before, [c for c in D.streamed if "up" in c]),
      (True, dumps_before, True, []))
check("failed pull: says so, not 'no newer image'",
      ("The pull failed and there is no newer image on the server – nothing changed." in out,
       "No newer image available" in out, "is rolled out" in out), (True, False, False))
D.pull_rc = 0

# The registry fails (no network, rate limit). Up to 0.18.0 the pull ran with
# --ignore-pull-failures: compose exited 0, and with the old image still there
# the run ended as "no newer image" (exit 0). Now: exit 5, nothing left.
D.registry_down = True
D.streamed.clear()
tags_before, dumps_before, ops_before = dict(D.tags), dumps_of("glitchtip"), ops()
rc, out = quiet(U.cmd_update, "glitchtip-postgres")
release_lock()
check("registry down, old image -> exit 5, not 'nochange'",
      (rc, "No newer image available" in out, '"status": "nochange"' in out), (5, False, False))
check("registry down: pulled without --ignore-pull-failures, the error in the message",
      (["--ignore-pull-failures" in c for c in D.streamed if "pull" in c],
       'nothing changed. Pull: postgres Error Get "https://registry-1.docker.io/v2/"' in out), ([False], True))
check("registry down: no rollback tag, no dump, no history entry, nothing recreated",
      (D.tags == tags_before, dumps_of("glitchtip"), ops() == ops_before, [c for c in D.streamed if "up" in c]),
      (True, dumps_before, True, []))
# A newer image on the server already (pulled by hand, or before the registry
# went down) is still rolled out, with a warning that names it.
D.images["sha256:prepulled"] = {"Id": "sha256:prepulled", "Config": {"Env": ["PG_VERSION=16.16"]}}
D.tags["postgres:16-alpine"] = "sha256:prepulled"
rc, out = quiet(U.cmd_update, "glitchtip-postgres", do_dump=False)
release_lock()
res = last_result(out)
check("registry down, newer image on the server -> rolled out, exit 0",
      (rc, res["status"], res["to_version"], [D.containers[n]["Image"] for n in ("glitchtip-postgres", "glitchtip-migrate")]),
      (0, "ok", "16.16", ["sha256:prepulled"] * 2))
check("registry down, newer image on the server: the warning says so",
      ("Warning: the pull failed (postgres Error Get" in out
       and "postgres:16-alpine is on the server already (prepulled) and is rolled out." in out), True)
D.registry_down = False
rc, _ = quiet(U.cmd_rollback, res["op"])
release_lock()
check("registry down: its update rolled back", (rc, D.tags["postgres:16-alpine"], D.containers["glitchtip-postgres"]["Image"]),
      (0, "sha256:new", "sha256:new"))

# Time limits (DOCKER_TIMEOUT, DUMP_TIMEOUT). A pull that does not end is stopped
# before anything changed. A compose up that does not end fails the update with
# its history, output and rollback, and never claims the containers are as they
# were: the Docker daemon may still be changing them.
D.pull_raises = U.Timeout(["docker", "compose", "pull", "migrate", "postgres"], 1800, ["postgres Pulling"])
tags_before, dumps_before, ops_before = dict(D.tags), dumps_of("glitchtip"), ops()
rc, out = quiet(U.cmd_update, "glitchtip-postgres")
release_lock()
D.pull_raises = None
check("pull timeout -> exit 5, nothing left, the daemon may still finish the download",
      (rc, D.tags == tags_before, dumps_of("glitchtip"), ops() == ops_before,
       "The pull did not end within 1800 s and was stopped (DOCKER_TIMEOUT" in out, "Docker may still finish" in out),
      (5, True, dumps_before, True, True, True))
D.pull_to = ("postgres:16-alpine", "sha256:v30")
D.images["sha256:v30"] = {"Id": "sha256:v30", "Config": {"Env": ["PG_VERSION=16.30"]}}
D.up_raises = U.Timeout(["docker", "compose", "up", "-d", "--no-deps", "--pull", "never", "migrate", "postgres"], 1800)
with as_uid(0):
    rc, out = quiet(U.main, ["update", "glitchtip-postgres", "--no-dump"])
release_lock()
D.up_raises = None
res = last_result(out)
op = ops()[res["op"]]
check("up timeout -> exit 4, failed, a timeout cause that says the containers may still change",
      (rc, res["status"], op["status"], op["reason"]["source"], "may still be changing" in op["reason"]["line"]),
      (4, "failed", "failed", "timeout", True))
check("up timeout: output kept, rollback offered",
      (os.path.exists(os.path.join(U.LOGS, res["op"] + ".log")), history(res["op"])["can_rollback"]), (True, True))
rc, _ = quiet(U.cmd_rollback, res["op"])
release_lock()
check("up timeout: the lock is free again, the rollback runs", (rc, D.tags["postgres:16-alpine"]), (0, "sha256:new"))
real_copy = U.copy_out


def dump_hangs(cmd, sink, limit, errfile, grace=10, keep_fds=()):
    sink(b"--\n-- PostgreSQL database cluster dump\n")
    raise U.Timeout(cmd, limit)


U.copy_out, U.LIMITS["dump"] = dump_hangs, 7
rc, out = quiet(U.cmd_update, "glitchtip-postgres")
release_lock()
U.copy_out, U.LIMITS["dump"] = real_copy, 14400
check("dump timeout -> exit 5, the limit named, no partial file left",
      (rc, "Dump of glitchtip-postgres stopped after 7 s (DUMP_TIMEOUT" in out,
       [f for f in dumps_of("glitchtip") if f.endswith(".part")], dumps_of("glitchtip") == dumps_before),
      (5, True, [], True))
D.pull_to = None
check("after the timeouts: the next update runs (nothing newer now)",
      quiet(U.cmd_update, "glitchtip-postgres", do_dump=False)[0], 0)
release_lock()

# Signals to the helper (SIGTERM, SIGHUP: diunproc.Interrupted, raised where the
# command ran). Before any container changed, the run takes back what it did and
# stops with exit 5. During the restart it fails with its history, output and
# rollback (exit 4), and its cause names the signal without claiming that the
# containers are as before. Real processes and signals: further below.


def dump_signalled(cmd, sink, limit, errfile, grace=10, keep_fds=()):
    sink(b"--\n-- PostgreSQL database cluster dump\n")
    raise U.Interrupted(signal.SIGTERM)


U.copy_out = dump_signalled
tags_before, dumps_before, ops_before = dict(D.tags), dumps_of("glitchtip"), ops()
rc, out = quiet(U.cmd_update, "glitchtip-postgres")
release_lock()
U.copy_out = real_copy
check("SIGTERM during the dump -> exit 5, the partial dump gone, nothing else left",
      (rc, "Stopped by SIGTERM before any container changed – the dumps begun are deleted, nothing changed." in out,
       [f for f in dumps_of("glitchtip") if f.endswith(".part")], dumps_of("glitchtip") == dumps_before,
       D.tags == tags_before, ops() == ops_before), (5, True, [], True, True, True))
D.pull_raises = U.Interrupted(signal.SIGHUP)
rc, out = quiet(U.cmd_update, "glitchtip-postgres")
release_lock()
D.pull_raises = None
check("SIGHUP during the pull -> exit 5, dumps and rollback tag taken back, no history entry",
      (rc, "Stopped by SIGHUP before any container changed – dumps, rollback copy and changed files taken back, "
           "nothing changed. A pull that had begun may still finish in the Docker daemon." in out,
       D.tags == tags_before, dumps_of("glitchtip") == dumps_before, ops() == ops_before), (5, True, True, True, True))
D.pull_to = ("postgres:16-alpine", "sha256:v31")
D.images["sha256:v31"] = {"Id": "sha256:v31", "Config": {"Env": ["PG_VERSION=16.31"]}}
D.up_raises = U.Interrupted(signal.SIGTERM)
with as_uid(0):
    rc, out = quiet(U.main, ["update", "glitchtip-postgres", "--no-dump"])
release_lock()
D.up_raises = None
res = last_result(out)
op = ops()[res["op"]]
check("SIGTERM during the restart -> exit 4, failed, the cause names the signal and says to check",
      (rc, res["status"], op["status"], op["reason"]["source"], "got SIGTERM and stopped the restart" in
       op["reason"]["line"], "check with docker ps" in op["reason"]["line"]), (4, "failed", "failed", "signal", True, True))
check("SIGTERM during the restart: output kept, rollback offered, no success claimed",
      (os.path.exists(os.path.join(U.LOGS, res["op"] + ".log")), history(res["op"])["can_rollback"],
       "Update successful" in out), (True, True, False))
D.up_raises = U.Interrupted(signal.SIGHUP)
rc, out = quiet(U.cmd_rollback, res["op"])
release_lock()
D.up_raises = None
check("SIGHUP during the rollback's restart -> exit 4, failed with the signal as cause, offered again",
      (rc, ops()[res["op"]].get("rollback_status"), ops()[res["op"]]["rollback_reason"]["source"],
       ops()[res["op"]]["rolled_back"], history(res["op"])["can_rollback"]), (4, "failed", "signal", False, True))
D.images_from = lambda: (_ for _ in ()).throw(U.Interrupted(signal.SIGTERM))
tags_before, calls_before = dict(D.tags), len(D.calls)
rc, out = quiet(U.cmd_rollback, res["op"])
release_lock()
D.images_from = None
check("SIGTERM before the rollback changed a container -> exit 5, nothing changed",
      (rc, "Rollback stopped by SIGTERM before any container changed – nothing changed." in out,
       D.tags == tags_before, [c[:2] for c in D.calls[calls_before:] if c[:2] == ["docker", "tag"]]), (5, True, True, []))
rc, _ = quiet(U.cmd_rollback, res["op"])
release_lock()
check("after the signals: the rollback runs", (rc, D.tags["postgres:16-alpine"], ops()[res["op"]]["rolled_back"]),
      (0, "sha256:new", True))
D.pull_to = None
# The limits come from /etc/image-updates.conf, checked as strictly as the cleanup rules.
with open(U.CONF, "w") as f:
    f.write("KEEP_DUMPS=2\nDOCKER_TIMEOUT=900\nDUMP_TIMEOUT='7200'\n")
os.chmod(U.CONF, 0o600)
U.load_limits()
check("limits from the file, the cleanup rules beside them",
      (U.LIMITS, U.load_rules()["keep_dumps"]), ({"docker": 900, "dump": 7200}, 2))
bad = []
for line in ("DOCKER_TIMEOUT=30", "DUMP_TIMEOUT=999999", "DOCKER_TIMEOUT=ten", "DOCKER_TIMEOUT=-60"):
    with open(U.CONF, "w") as f:
        f.write(line + "\n")
    bad.append(quiet(U.load_limits))
check("limits out of range or no number -> exit 2 with the allowed range",
      [(rc, "(60–86400 s) – nothing changed" in out or "(60–172800 s) – nothing changed" in out) for rc, out in bad],
      [(2, True)] * 4)
# Through main, as the page calls it (the file belongs to the test's user, so main,
# as root, refuses it already for that; either way before anything changes).
with as_uid(0):
    rc, out = quiet(U.main, ["update", "glitchtip-postgres"])
release_lock()
check("a settings file not understood stops an update before anything changes",
      (rc, out.strip().endswith("– nothing changed.")), (2, True))
os.unlink(U.CONF)
U.LIMITS.update(docker=1800, dump=14400)

# Version only known to the radar (latest without a version variable)
D.images["sha256:oll"] = {"Id": "sha256:oll", "Config": {"Env": []}}
with open(U.VERSIONS, "w") as f:
    json.dump({"images": [{"image": "ollama/ollama", "tag": "latest",
                           "installed": {"sha256:oll": {"version": "0.30.11", "src": "digest"}}}]}, f)
check("version from the radar cache", U.version_of("ollama/ollama:latest", "sha256:oll"), "0.30.11")
check("version unknown", U.version_of("ollama/ollama:latest", "sha256:unknown"), None)
with open(U.VERSIONS, "w") as f:
    f.write("{")
check("version: broken radar file is no error", U.version_of("ollama/ollama:latest", "sha256:oll"), None)
# A line tag (cubejs/cube:v1.6) names only its line: the radar's release
# refines the tag's number (plan and history), a version that does not fit
# leaves it; a version variable is not overridden.
D.images["sha256:cube"] = {"Id": "sha256:cube", "Config": {"Env": []}}
D.images["sha256:rds"] = {"Id": "sha256:rds", "Config": {"Env": ["REDIS_VERSION=7.4.11"]}}
with open(U.VERSIONS, "w") as f:
    json.dump({"images": [{"image": "cubejs/cube", "tag": "v1.6", "installed": {"sha256:cube": {"version": "1.6.74"}}},
                          {"image": "cubejs/cube", "tag": "v1.7", "installed": {"sha256:cube": {"version": "2.0.1"}}},
                          {"image": "redis", "tag": "7.4-alpine", "installed": {"sha256:rds": {"version": "7.4.12"}}}]}, f)
check("line tag: the radar's release refines the tag's number, nothing else does",
      (U.version_of("cubejs/cube:v1.6", "sha256:cube"), U.version_of("cubejs/cube:v1.7", "sha256:cube"),
       U.version_of("redis:7.4-alpine", "sha256:rds")), ("1.6.74", "1.7", "7.4.11"))
with open(U.VERSIONS, "w") as f:
    f.write("{")

# Failed health check -> exit 4, no cleanup, no radar; then rollback
D.pull_to = ("postgres:16-alpine", "sha256:newer")
D.images["sha256:newer"] = {"Id": "sha256:newer", "Config": {"Env": ["PG_VERSION=16.16"]}}
U.wait_healthy = lambda project, services: (False, ["glitchtip-migrate"], {})
LOGS["glitchtip-migrate"] = ["running migrations", 'FATAL:  role "glitchtip" does not exist']
touch_dump("glitchtip/20251231-030000_glitchtip-postgres.sql.gz")
radar.calls.clear()
rc, out = quiet(U.cmd_update, "glitchtip-postgres", do_dump=False)
release_lock()
res = json.loads(out.strip().splitlines()[-1][9:])
check("failed update -> exit 4", (rc, res["status"]), (4, "failed"))
check("failed update: cause in the result and the history",
      (res["reason"]["container"], res["reason"]["line"], ops()[res["op"]]["reason"]["line"]),
      ("glitchtip-migrate", 'FATAL:  role "glitchtip" does not exist', 'FATAL:  role "glitchtip" does not exist'))
check("failed update: cause in the output",
      'Most likely cause (glitchtip-migrate): FATAL:  role "glitchtip" does not exist' in out, True)
LOGS.clear()
check("failed update: no cleanup", "20251231-030000_glitchtip-postgres.sql.gz" in dumps_of("glitchtip"), True)
check("failed update: radar not started", radar.calls, [])
U.wait_healthy = lambda project, services: (True, [], {})
rc, out = quiet(U.cmd_rollback, res["op"])
release_lock()
check("rollback -> exit 0", rc, 0)
check("rollback: tag back on the previous image", D.tags["postgres:16-alpine"], "sha256:new")
check("rollback: marked in the history", (ops()[res["op"]]["rolled_back"], ops()[res["op"]]["rollback_status"]), (True, "ok"))
check("rollback: radar started", ["start", "--no-block", U.RADAR_UNIT] in radar.calls, True)
check("second rollback -> 3", quiet(U.cmd_rollback, res["op"])[0], 3)
release_lock()

# compose up fails -> cause from its output; the rollback fails too -> its cause
D.pull_to = ("postgres:16-alpine", "sha256:newest")
D.images["sha256:newest"] = {"Id": "sha256:newest", "Config": {"Env": ["PG_VERSION=16.17"]}}
D.up_rc, D.up_out = 1, ["Container glitchtip-postgres Recreate",
                        "Error response from daemon: Bind for 0.0.0.0:5432 failed: port is already allocated"]
rc, out = quiet(U.cmd_update, "glitchtip-postgres", do_dump=False)
release_lock()
res = json.loads(out.strip().splitlines()[-1][9:])
check("compose up fails -> exit 4, cause from compose",
      (rc, res["reason"]["source"], res["reason"]["line"]),
      (4, "compose", "Error response from daemon: Bind for 0.0.0.0:5432 failed: port is already allocated"))
D.up_rc, D.up_out = 0, []
U.wait_healthy = lambda project, services: (False, ["glitchtip-postgres"], {})
LOGS["glitchtip-postgres"] = ["FATAL:  database files are incompatible with server"]
rc, out = quiet(U.cmd_rollback, res["op"])
release_lock()
rres = json.loads(out.strip().splitlines()[-1][9:])
check("failed rollback -> exit 4 with its cause",
      (rc, rres["status"], rres["reason"]["line"]), (4, "failed", "FATAL:  database files are incompatible with server"))
check("failed rollback: cause in the history",
      (ops()[res["op"]]["rollback_status"], ops()[res["op"]]["rollback_reason"]["container"],
       ops()[res["op"]]["reason"]["source"]),
      ("failed", "glitchtip-postgres", "compose"))
LOGS.clear()
U.wait_healthy = lambda project, services: (True, [], {})

# A failed rollback may be tried again once the cause is fixed. Up to 0.18.0
# it was marked rolled_back as well, and a second try ended with exit 3.
failed_op = res["op"]
check("failed rollback: not marked rolled back, offered again",
      (ops()[failed_op]["rolled_back"], history(failed_op)["can_rollback"]), (False, True))
rc, out = quiet(U.cmd_rollback, failed_op)
release_lock()
check("failed rollback tried again -> exit 0, rolled back, its old cause cleared",
      (rc, ops()[failed_op]["rolled_back"], ops()[failed_op]["rollback_status"], ops()[failed_op]["rollback_reason"]),
      (0, True, "ok", None))
check("a rollback that went through is not repeated -> 3", quiet(U.cmd_rollback, failed_op)[0], 3)
release_lock()
U.record({"id": failed_op, "rolled_back": True, "rollback_status": "failed"})
check("a history line up to 0.18.0 (rolled_back and failed) -> offered again", history(failed_op)["can_rollback"], True)
U.record({"id": failed_op, "rolled_back": True, "rollback_status": "ok"})

# The helper itself stops after the containers were recreated (docker inspect
# fails while it waits for them). Up to 0.18.0 the operation was recorded only
# afterwards: no history, no output, no rollback from the page.
D.pull_to = ("postgres:16-alpine", "sha256:v20")
D.images["sha256:v20"] = {"Id": "sha256:v20", "Config": {"Env": ["PG_VERSION=16.20"]}}


def docker_gone(project, services):
    raise subprocess.CalledProcessError(1, ["docker", "inspect"], "", "Cannot connect to the Docker daemon")


U.wait_healthy = docker_gone
with as_uid(0):
    rc, out = quiet(U.main, ["update", "glitchtip-postgres", "--no-dump"])
release_lock()
res = last_result(out)
op = ops()[res["op"]]
check("exception after the restart -> exit 4, failed, its cause in the result and the history",
      (rc, res["status"], op["status"], op["reason"]["source"], op["reason"]["line"]),
      (4, "failed", "failed", "error", "Check after the restart stopped: Cannot connect to the Docker daemon"))
check("exception after the restart: output kept, rollback offered",
      (os.path.exists(os.path.join(U.LOGS, res["op"] + ".log")), history(res["op"])["can_rollback"]), (True, True))
U.wait_healthy = lambda project, services: (True, [], {})
rc, _ = quiet(U.cmd_rollback, res["op"])
release_lock()
check("exception after the restart: rollback -> exit 0, the image from before again",
      (rc, [D.containers[n]["Image"] for n in ("glitchtip-postgres", "glitchtip-migrate")]), (0, ["sha256:new"] * 2))

# A run that ends for good after the restart (killed, power cut) keeps its
# entry ("running", the page shows "aborted") with the rollback copy.
D.pull_to = ("postgres:16-alpine", "sha256:v21")
D.images["sha256:v21"] = {"Id": "sha256:v21", "Config": {"Env": ["PG_VERSION=16.21"]}}


def killed(project, services):
    raise KeyboardInterrupt


U.wait_healthy = killed
out = interrupted(["update", "glitchtip-postgres", "--no-dump"])
op = [o for o in ops().values() if o.get("new_id") == "sha256:v21"][0]
h = history(op["id"])
check("run ended after the restart: in the history with its rollback copy and its output",
      (op["status"], D.tags.get(op["rollback"]), D.containers["glitchtip-postgres"]["Image"], h["active"],
       h["can_rollback"], os.path.exists(os.path.join(U.LOGS, op["id"] + ".log"))),
      ("running", "sha256:new", "sha256:v21", False, True, True))
real_alive, U.helper_alive = U.helper_alive, lambda pid: pid == os.getpid()
h = history(op["id"])
check("an update whose process still runs is not offered for rollback", (h["active"], h["can_rollback"]), (True, False))
U.helper_alive = real_alive
U.wait_healthy = lambda project, services: (True, [], {})
rc, _ = quiet(U.cmd_rollback, op["id"])
release_lock()
check("run ended after the restart: rollback -> exit 0, the image from before again",
      (rc, D.containers["glitchtip-postgres"]["Image"], ops()[op["id"]]["rolled_back"]), (0, "sha256:new", True))

# The compose file names another image since the update (latest -> 2, here
# 16 -> 17): the rollback put only the old tag back, compose started the other
# image, and the rollback said "ok". Now refused before anything changes.
D.pull_to = ("postgres:16-alpine", "sha256:v22")
D.images["sha256:v22"] = {"Id": "sha256:v22", "Config": {"Env": ["PG_VERSION=16.22"]}}
rc, out = quiet(U.cmd_update, "glitchtip-postgres", do_dump=False)
release_lock()
res = last_result(out)
D.images_from = lambda: {"postgres": "postgres:17-alpine", "migrate": "postgres:16-alpine"}
D.streamed.clear()
tags_before = dict(D.tags)
rc, out = quiet(U.cmd_rollback, res["op"])
release_lock()
check("rollback, the compose file names another image now -> exit 5, nothing changed",
      (rc, D.tags == tags_before, [c for c in D.streamed if "up" in c], ops()[res["op"]].get("rolled_back"),
       D.containers["glitchtip-postgres"]["Image"]), (5, True, [], None, "sha256:v22"))
check("rollback, another image: the message names it",
      "The compose files now give postgres: postgres:17-alpine instead of postgres:16-alpine – nothing changed" in out,
      True)
D.images_from = None
# Compose starts something else than the rollback copy all the same: the image
# IDs that run are checked; the rollback fails and can be tried again.
D.up_runs = "sha256:elsewhere"
rc, out = quiet(U.cmd_rollback, res["op"])
release_lock()
rres = last_result(out)
check("rollback, the containers run another image -> exit 4, failed, not 'ok'",
      (rc, rres["status"], rres["reason"]["source"], rres["reason"]["container"], ops()[res["op"]]["rolled_back"]),
      (4, "failed", "image", "glitchtip-migrate", False))
check("rollback, another image: the cause names both",
      rres["reason"]["line"].startswith("runs elsewhere instead of the rollback copy postgres:rollback-"), True)
D.up_runs = None
rc, _ = quiet(U.cmd_rollback, res["op"])
release_lock()
check("rollback tried again -> exit 0, on the rollback copy",
      (rc, [D.containers[n]["Image"] for n in ("glitchtip-postgres", "glitchtip-migrate")]), (0, ["sha256:new"] * 2))

# kept output (through main, as the plugin calls it) and the new build's digests
D.pull_to = ("postgres:16-alpine", "sha256:v18")
D.images["sha256:v18"] = {"Id": "sha256:v18", "Config": {"Env": ["PG_VERSION=16.18"]},
                          "RepoDigests": ["postgres@sha256:dddd", "docker.io/library/postgres@sha256:dddd"]}
U.wait_healthy = lambda project, services: (False, ["glitchtip-postgres"], {})
LOGS["glitchtip-postgres"] = ["Error: kept for the log test"]
with as_uid(0):
    rc, out = quiet(U.main, ["update", "glitchtip-postgres", "--no-dump"])
release_lock()
res = json.loads(out.strip().splitlines()[-1][9:])
logf = os.path.join(U.LOGS, res["op"] + ".log")
with open(logf) as f:
    kept = f.read()
check("kept output: the whole run, without @@RESULT, root only",
      (rc, "Updating postgres:16-alpine" in kept, "Most likely cause" in kept, "@@RESULT" in kept,
       oct(os.stat(logf).st_mode & 0o777)), (4, True, True, False, "0o600"))
check("kept output: the caller still sees everything", "@@RESULT" in out and "Updating postgres:16-alpine" in out, True)
check("new build: its registry digests in the history", ops()[res["op"]]["new_digests"], ["sha256:dddd"])
with as_uid(0):
    rc1, _ = quiet(U.main, ["rollback", res["op"]])
release_lock()
U.wait_healthy = lambda project, services: (True, [], {})
with as_uid(0):
    rc, _ = quiet(U.main, ["rollback", res["op"]])
release_lock()
with as_uid(0):
    rc, out = quiet(U.main, ["log", res["op"]])
check("log: the update, then its rollback",
      (rc, out.index("Updating") < out.index("--- Rollback ---") < out.index("Rolling")), (0, True))
check("log: a rollback tried again after a failure keeps both outputs",
      (rc1, out.count("Rolling postgres:16-alpine back"), out.index("Rollback failed") < out.index("--- Tried again ---")
       < out.index("Rollback successful")), (4, 2, True))
bk = json.loads(quiet(U.cmd_backups)[1])
o = [x for x in bk["ops"] if x["id"] == res["op"]][0]
check("backups: which outputs are kept, digests of the new build",
      (o["log"], o["rollback_log"], o["new_digests"]), (True, True, ["sha256:dddd"]))
check("log: unknown operation -> 2", main_rc("log", "20200101-000000-nope"), 2)
check("log: invalid id -> 64", main_rc("log", "../../etc/passwd"), 64)
FakePopen.script = {"glitchtip-postgres": (b"partial", 1)}
n_logs = len(os.listdir(U.LOGS))
with as_uid(0):
    rc, _ = quiet(U.main, ["update", "glitchtip-postgres"])
release_lock()
check("an update that stops before any change keeps no output", (rc, len(os.listdir(U.LOGS))), (5, n_logs))
FakePopen.script = {"glitchtip-postgres": (DUMP_OK, 0)}
U.LOG_MAX, real_max = 40, U.LOG_MAX
D.pull_to = ("postgres:16-alpine", "sha256:v19")
D.images["sha256:v19"] = {"Id": "sha256:v19", "Config": {"Env": ["PG_VERSION=16.19"]}}
with as_uid(0):
    rc, out = quiet(U.main, ["update", "glitchtip-postgres", "--no-dump"])
release_lock()
res = json.loads(out.strip().splitlines()[-1][9:])
with open(os.path.join(U.LOGS, res["op"] + ".log")) as f:
    kept = f.read()
check("kept output is cut at LOG_MAX and says so", (rc, kept.rstrip().endswith("KiB]"), len(kept) < 200), (0, True, True))
check("a locally tagged build has no registry digests", ops()[res["op"]]["new_digests"], [])
U.LOG_MAX = real_max
LOGS.clear()

# ------------------------------------------------------------ cleanup rules
reset_fs()
check("rules: defaults", U.load_rules(), {"keep_dumps": 3, "image_days": 14, "file": None})
with open(U.CONF, "w") as f:
    f.write("# comment\n\nKEEP_DUMPS = 5\nROLLBACK_IMAGE_DAYS=\"0\"\n")
os.chmod(U.CONF, 0o600)
check("rules: from the file", U.load_rules(), {"keep_dumps": 5, "image_days": 0, "file": U.CONF})
for bad in ("KEEP_DUMP=0\n", "KEEP_DUMPS=-1\n", "KEEP_DUMPS=three\n", "KEEP_DUMPS\n"):
    with open(U.CONF, "w") as f:
        f.write(bad)
    check(f"rules: {bad.strip()!r} stops the cleanup", quiet(U.load_rules)[0], 2)
with open(U.CONF, "w") as f:
    f.write("KEEP_DUMPS=1\n")
os.chmod(U.CONF, 0o664)
check("rules: group-writable file stops the cleanup", quiet(U.load_rules)[0], 2)
os.unlink(U.CONF)

NOW = datetime(2026, 10, 12, 12, 0, 0)
DUMPS = [{"path": p, "project": p.split("/")[0], "container": p.partition("_")[2][:-len(".sql.gz")], "size": 100}
         for p in ("a/20261005-030000_a-db.sql.gz", "a/20261004-030000_a-db.sql.gz", "a/20261003-030000_a-db.sql.gz",
                   "a/20261002-030000_a-db.sql.gz", "a/20261001-030000_a-db.sql.gz",
                   "a/20261001-030000_a-cache.sql.gz",
                   "b/20261004-030000_b-db.sql.gz", "b/20261003-030000_b-db.sql.gz", "b/20261002-030000_b-db.sql.gz",
                   "b/20261001-030000_b-db.sql.gz", "b/manual-copy.sql.gz")]
OPS = {"20261001-030000-b-db": {"id": "20261001-030000-b-db", "kind": "update", "project": "b",
                                "dumps": ["b/20261001-030000_b-db.sql.gz"]},
       "20260901-030000-a-db": {"id": "20260901-030000-a-db", "kind": "update", "project": "a",
                                "dumps": ["a/20261001-030000_a-db.sql.gz"]},
       "20261005-030000-a-db": {"id": "20261005-030000-a-db", "kind": "update", "project": "a",
                                "dumps": ["a/20261005-030000_a-db.sql.gz"]}}
IMAGES = [{"ref": "postgres:rollback-20260920-030000", "in_use": False},     # 22 days, unused
          {"ref": "postgres:rollback-20260915-030000", "in_use": True},      # old, but in use
          {"ref": "postgres:rollback-20261005-030000", "in_use": False},     # 7 days
          {"ref": "ghcr.io/x/app:rollback-20260801-000000", "in_use": False}]


def sel(rules, **kw):
    d, i = U.cleanup_plan(dict({"file": None}, **rules), DUMPS, IMAGES, OPS, now=NOW, **kw)
    return [x["path"] for x in d], [x["ref"] for x in i]


d, i = sel({"keep_dumps": 3, "image_days": 14})
check("cleanup: 2 oldest dumps of a-db", sorted(x for x in d if x.startswith("a/")),
      ["a/20261001-030000_a-db.sql.gz", "a/20261002-030000_a-db.sql.gz"])
check("cleanup: dump of the last update of b is kept although oldest", [x for x in d if x.startswith("b/")], [])
check("cleanup: files not named like dumps are never touched", "b/manual-copy.sql.gz" in d, False)
check("cleanup: unused images older than 14 days", sorted(i),
      ["ghcr.io/x/app:rollback-20260801-000000", "postgres:rollback-20260920-030000"])
check("cleanup: keep 1", len(sel({"keep_dumps": 1, "image_days": 14})[0]), 6)
check("cleanup: 0 switches rules off", sel({"keep_dumps": 0, "image_days": 0}), ([], []))
check("cleanup: only project b, only postgres (docker.io name)",
      sel({"keep_dumps": 1, "image_days": 14}, project="b", repo="docker.io/library/postgres"),
      (["b/20261003-030000_b-db.sql.gz", "b/20261002-030000_b-db.sql.gz"],
       ["postgres:rollback-20260920-030000"]))

# cleanup verb on files + fake Docker
fresh_docker()
for p in ("a/20261005-030000_a-db.sql.gz", "a/20261004-030000_a-db.sql.gz", "a/20261003-030000_a-db.sql.gz",
          "a/20261002-030000_a-db.sql.gz", "c/20250101-000000_c-db.sql.gz", "c/20240101-000000_c-db.sql.gz",
          "c/20230101-000000_c-db.sql.gz", "c/20220101-000000_c-db.sql.gz"):
    touch_dump(p)
D.tags = {"postgres:rollback-20200101-000000": "sha256:p1", "nginx:rollback-20200101-000000": "sha256:n1",
          "postgres:16": "sha256:p2"}
D.rmi_refuse = {"nginx:rollback-20200101-000000"}
rc, out = quiet(U.cmd_cleanup, dry=True)
check("cleanup --dry-run deletes nothing", (rc, len(dumps_of("a")), len(D.tags)), (0, 4, 3))
check("cleanup --dry-run lists what it would do", out.count("Would "), 4)
rc, out = quiet(U.cmd_cleanup)
release_lock()
res = json.loads(out.strip().splitlines()[-1][9:])
check("cleanup -> exit 0, deleted + failed", (rc, res["status"], sorted(res["dumps"]), res["images"], res["failed"]),
      (0, "ok", ["a/20261002-030000_a-db.sql.gz", "c/20220101-000000_c-db.sql.gz"],
       ["postgres:rollback-20200101-000000"], ["nginx:rollback-20200101-000000"]))
check("cleanup: files gone", (len(dumps_of("a")), len(dumps_of("c"))), (3, 3))
check("cleanup: freed bytes", res["bytes"] > 0, True)
D.rmi_refuse = set()
D.tags.pop("nginx:rollback-20200101-000000")
rc, out = quiet(U.cmd_cleanup)
release_lock()
check("cleanup: nothing left -> nochange", json.loads(out.strip().splitlines()[-1][9:])["status"], "nochange")
with open(U.CONF, "w") as f:
    f.write("KEEP_DUMPS=1\nROLLBACK_IMAGE_DAYS=0\n")
os.chmod(U.CONF, 0o600)
rc, out = quiet(U.cmd_cleanup)
release_lock()
check("cleanup: rules from the file", (rc, len(dumps_of("a")), len(dumps_of("c"))), (0, 1, 1))
check("cleanup: rules named in the output", "Rules (" + U.CONF + "): keep the last 1 dumps" in out, True)
with open(U.CONF, "w") as f:
    f.write("KEEP=1\n")
rc, out = quiet(U.cmd_cleanup)
release_lock()
check("cleanup: broken rules -> exit 2, nothing deleted", (rc, len(dumps_of("a"))), (2, 1))

# backups: preview of the cleanup, history fields
os.unlink(U.CONF)
for p in ("a/20261004-030000_a-db.sql.gz", "a/20261003-030000_a-db.sql.gz", "a/20261002-030000_a-db.sql.gz"):
    touch_dump(p)
D.tags["postgres:rollback-20200101-000000"] = "sha256:p1"
D.add(ctn("a-db", "postgres:16", "sha256:p1", project="a"))
U.record({"id": "20261005-030000-a-db", "ts": "x", "kind": "update", "container": "a-db", "project": "a",
          "workdir": "/opt/a", "files": [], "image": "postgres:16", "old_id": "sha256:p0", "new_id": "sha256:p1",
          "from_version": "16.3", "to_version": "16.4", "rollback": "postgres:rollback-20200101-000000",
          "dumps": ["a/20261005-030000_a-db.sql.gz"], "status": "ok"})
rc, out = quiet(U.cmd_backups)
bk = json.loads(out)
check("backups: cleanup preview", (bk["cleanup"]["keep_dumps"], bk["cleanup"]["image_days"], bk["cleanup"]["dumps"],
                                   bk["cleanup"]["images"], bk["cleanup"]["error"]),
      (3, 14, ["a/20261002-030000_a-db.sql.gz"], [], None))
check("backups: rollback image in use is not offered", [x["in_use"] for x in bk["images"]], [True])
check("backups: rollback image without a size Docker names -> None, created as ISO 8601",
      [(x["size"], x["created"]) for x in bk["images"]], [(None, "2026-01-01T00:00:00+00:00")])
D.images["sha256:p1"] = {"Id": "sha256:p1", "Config": {"Env": []}, "Size": 288358400}
bk = json.loads(quiet(U.cmd_backups)[1])
check("backups: rollback image size in bytes (docker image inspect)", [x["size"] for x in bk["images"]], [288358400])
del D.images["sha256:p1"]
check("iso_time: docker image ls -> ISO 8601",
      (U.iso_time("2026-10-12 03:15:00 +0200 CEST"), U.iso_time("2026-10-12 03:15:00 -0430 X"), U.iso_time("")),
      ("2026-10-12T03:15:00+02:00", "2026-10-12T03:15:00-04:30", ""))
check("backups: history with versions, without paths",
      [(o["from_version"], o["to_version"], "workdir" in o, "new_id" in o) for o in bk["ops"]], [("16.3", "16.4", False, False)])
# Versions the update could not tell (latest without a version variable): the radar's since
U.record({"id": "20261004-030000-ollama", "ts": "x", "kind": "update", "container": "ollama", "project": "o",
          "image": "ollama/ollama:latest", "old_id": "sha256:oll", "new_id": "sha256:oll2",
          "from_version": "0.30.11", "to_version": None, "rollback": None, "dumps": [], "status": "ok"})
U.record({"id": "20261004-040000-mcp", "ts": "x", "kind": "update", "container": "mcp", "project": "o",
          "image": "x/mcp:latest", "old_id": "sha256:mcp1", "new_id": "sha256:mcp2",
          "from_version": None, "to_version": None, "rollback": None, "dumps": [], "status": "ok"})
# A line tag (cubejs/cube:v1.6): the history recorded the tag's number, the radar names the release.
U.record({"id": "20261004-050000-cube", "ts": "x", "kind": "retag", "container": "cube", "project": "o",
          "image": "cubejs/cube:v1.6.6", "old_id": "sha256:cube1", "new_id": "sha256:cube2",
          "from_version": "1.6.6", "to_version": "1.6", "rollback": None, "dumps": [], "status": "ok"})
vers = lambda: {o["id"]: (o["from_version"], o["to_version"])
                for o in json.loads(quiet(U.cmd_backups)[1])["ops"] if o["project"] == "o"}
check("backups: broken radar file -> versions stay unknown", vers(),
      {"20261004-030000-ollama": ("0.30.11", None), "20261004-040000-mcp": (None, None),
       "20261004-050000-cube": ("1.6.6", "1.6")})
with open(U.VERSIONS, "w") as f:
    json.dump({"images": [{"image": "ollama/ollama", "tag": "latest",
                           "installed": {"sha256:oll2": {"version": "0.40.6", "src": "digest"}}},
                          {"image": "x/mcp", "tag": "latest",
                           "installed": {"sha256:mcp2": {"version": "0.199.4", "src": "label"},
                                         "sha256:mcp1": {"version": None, "src": None}}},
                          {"image": "cubejs/cube", "tag": "v1.6",
                           "installed": {"sha256:cube2": {"version": "1.6.74", "src": "digest"},
                                         "sha256:cube1": {"version": "2.0.0", "src": "digest"}}}]}, f)
check("backups: missing versions from the radar, a line tag's number refined, others kept", vers(),
      {"20261004-030000-ollama": ("0.30.11", "0.40.6"), "20261004-040000-mcp": (None, "0.199.4"),
       "20261004-050000-cube": ("1.6.6", "1.6.74")})
check("backups: the history file itself is not changed",
      (ops()["20261004-030000-ollama"]["to_version"], ops()["20261004-040000-mcp"]["from_version"]), (None, None))
U.record({"id": "20261006-030000-a-db", "ts": "x", "kind": "update", "container": "a-db", "project": "a",
          "image": "postgres:16", "rollback": None, "dumps": [], "status": "failed",
          "reason": {"container": "a-db", "state": "exited", "exit_code": 1, "oom": False, "source": "log",
                     "line": "FATAL: x"}})
bk = json.loads(quiet(U.cmd_backups)[1])
check("backups: the cause of a failed update is in the history", bk["ops"][0]["reason"]["line"], "FATAL: x")
with open(U.CONF, "w") as f:
    f.write("NONSENSE\n")
os.chmod(U.CONF, 0o600)
bk = json.loads(quiet(U.cmd_backups)[1])
check("backups: broken rules -> error, nothing offered",
      (bool(bk["cleanup"]["error"]), bk["cleanup"]["dumps"], bk["cleanup"]["images"]), (True, [], []))
os.unlink(U.CONF)

# ------------------------------------------------------------------ radar
U.systemctl = Systemctl(installed=False)
check("radar: not set up -> 2", quiet(U.cmd_radar)[0], 2)
U.systemctl = sc = Systemctl()
check("radar: runs and waits -> 0", (quiet(U.cmd_radar)[0], sc.calls[-1]), (0, ["start", U.RADAR_UNIT]))
U.systemctl = Systemctl(start_rc=1)
check("radar: failed run -> 1", quiet(U.cmd_radar)[0], 1)
U.systemctl = sc = Systemctl(installed=False)
quiet(U.start_radar)
check("no radar unit -> nothing started after an update", [c for c in sc.calls if c[0] == "start"], [])

# ---------------------------------------------------------- waiting for health
fresh_docker()
U.HEALTH_TIMEOUT = 0
old = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")


def state(status, health=None, exit_code=0, restarts=0, policy="unless-stopped"):
    c = ctn("x", "app:1", "sha256:a", project="p", service="s")
    c["State"] = {"Status": status, "Running": status == "running", "ExitCode": exit_code,
                  "RestartCount": restarts, "StartedAt": old + ".123Z"}
    if health:
        c["State"]["Health"] = {"Status": health}
    c["HostConfig"]["RestartPolicy"]["Name"] = policy
    D.containers = {"x": c}
    with contextlib.redirect_stdout(io.StringIO()):
        return REAL_WAIT("p", ["s"])[:2]


check("health: running, no healthcheck, stable", state("running"), (True, []))
check("health: healthy", state("running", "healthy"), (True, []))
check("health: unhealthy", state("running", "unhealthy"), (False, ["x"]))
check("health: one-shot job finished with 0", state("exited", policy="no"), (True, []))
check("health: exited with an error", state("exited", exit_code=1, policy="no"), (False, ["x"]))
check("health: restart loop", state("restarting", restarts=3), (False, ["x"]))

# Docker counts a restart only when it happens: in the restart loop the
# counter can stay 0. Seen restarting twice, the container counts as failed
# at once (two inspects), not after HEALTH_TIMEOUT. One crash that recovers
# is no failure.
U.HEALTH_TIMEOUT = 30
seq, real_handle = [], D.handle


def handle(cmd):
    if cmd[:2] == ["docker", "inspect"] and seq:
        D.containers["x"]["State"] = seq.pop(0)
    return real_handle(cmd)


D.handle = handle
state("running")
seq[:] = [{"Status": "restarting", "Running": True, "ExitCode": 4, "RestartCount": 0, "StartedAt": old + ".1Z"}] * 9
n0 = len(D.calls)
with contextlib.redirect_stdout(io.StringIO()):
    got = REAL_WAIT("p", ["s"])[:2]
check("health: restart loop with RestartCount 0 fails at the second look",
      (got, len([c for c in D.calls[n0:] if c[:2] == ["docker", "inspect"]])), ((False, ["x"]), 2))
seq[:] = [{"Status": "restarting", "Running": True, "ExitCode": 1, "RestartCount": 0, "StartedAt": old + ".1Z"},
          {"Status": "running", "Running": True, "ExitCode": 0, "RestartCount": 1, "StartedAt": old + ".2Z"}]
with contextlib.redirect_stdout(io.StringIO()):
    got = REAL_WAIT("p", ["s"])[:2]
check("health: one crash that recovers is no failure", got, (True, []))
D.handle = real_handle
U.HEALTH_TIMEOUT = 0
check("health: still starting -> timeout", state("running", "starting"), (False, ["x"]))

# ------------------------------------------------ retag --dry-run (stage 2)
reset_fs()
fresh_docker()
for i in ("fb", "app", "pl"):                       # versions from the tag itself
    D.images["sha256:" + i] = {"Id": "sha256:" + i, "Config": {"Env": []}}
RT = os.path.join(TMP, "retag")
os.makedirs(RT)


def write(name, text, newline="\n"):
    path = os.path.join(RT, name)
    with open(path, "w", encoding="utf-8", newline=newline) as f:
        f.write(text)
    return path


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def radar(*recs):
    with open(U.VERSIONS, "w") as f:
        json.dump({"images": list(recs)}, f)


def retag(name, tag):
    with contextlib.redirect_stdout(io.StringIO()):
        return U.make_retag(name, tag)[0]


def why(name, *tags):
    return [retag(name, t)["retag"]["reason"] for t in tags]


def edits(r):
    return [(os.path.basename(c["file"]), c["line"], c["old"], c["new"], c["service"]) for c in r["changes"]]


FB = "ghcr.io/formbricks/formbricks"
GT = "glitchtip/glitchtip"
PL = "ghcr.io/plausible/community-edition"
REC_FB = {"image": FB, "tag": "5.0.2", "db": False, "versions": {"5.0.2": {"status": "major", "choices": {
    "pin": "5.0.2", "lines": [{"tag": "6", "newest": "6.0.2"}, {"tag": "5", "newest": "5.4.5"}],
    "newer": [{"tag": "5.0.3", "level": "patch"}, {"tag": "5.4.5", "level": "minor"}, {"tag": "6.0.2", "level": "major"}],
    "floating": ["latest"]}}}}
REC_GT = {"image": GT, "tag": "latest", "db": False, "versions": {"6.1.8": {"status": "minor", "choices": {
    "pin": "6.1.8", "lines": [{"tag": "6", "newest": "6.2.6"}], "newer": [{"tag": "6.2.6", "level": "minor"}],
    "floating": ["latest"]}}}}
REC_PL = {"image": PL, "tag": "v2.1.4", "db": False, "versions": {"2.1.4": {"status": "major", "choices": {
    "pin": "v2.1.4", "lines": [], "newer": [{"tag": "v2.1.5", "level": "patch"}, {"tag": "v3.2.1", "level": "major"}],
    "floating": []}}}}
REC_PG = {"image": "postgres", "tag": "16-alpine", "db": True, "versions": {"16.14": {"status": "major", "choices": {
    "pin": "16.14-alpine", "lines": [{"tag": "17-alpine", "newest": "17.11-alpine"}, {"tag": "16-alpine", "newest": "16.15-alpine"}],
    "newer": [{"tag": "16.15-alpine", "level": "minor"}, {"tag": "17.11-alpine", "level": "major"}], "floating": ["alpine"]}}}}
REC_NC = {"image": "nextcloud", "tag": "apache", "db": False, "versions": {"34.0.3": {"status": "major", "choices": {
    "pin": "34.0.3-apache", "lines": [{"tag": "36-apache", "newest": "36.0.0-apache"}],
    "newer": [{"tag": "35.0.1-apache", "level": "major"}, {"tag": "36.0.0-apache", "level": "major"}],
    "floating": ["stable-apache"]}}}}
REC_APP = {"image": "acme/app", "tag": "1.0.0", "db": False, "versions": {"1.0.0": {"status": "patch", "choices": {
    "pin": "1.0.0", "lines": [], "newer": [{"tag": "1.0.1", "level": "patch"}], "floating": []}}}}
radar(REC_FB, REC_GT, REC_PL, REC_PG, REC_NC, REC_APP)

# A direct tag in a CRLF file, with quotes and a comment that must stay.
fb_file = write("fb.yml", 'services:\n  formbricks:\n    image: "ghcr.io/formbricks/formbricks:5.0.2"   # 6 needs AuthZed\n'
                          "    restart: always\n  db:\n    image: pgvector/pgvector:pg17\n", newline="\r\n")
D.add(ctn("formbricks-app", FB + ":5.0.2", "sha256:fb", project="formbricks", service="formbricks", files=fb_file))
D.cfg = {"services": {"formbricks": {"image": FB + ":5.0.2"}, "db": {"image": "pgvector/pgvector:pg17"}}}
before = sha(fb_file)
p = retag("formbricks-app", "5")
r = p["retag"]
check("retag: a version line, the compose line before and after (quotes, comment, CRLF file)",
      (r["possible"], r["choice"], r["newest"], r["image"], edits(r)),
      (True, "line", "5.4.5", FB + ":5",
       [("fb.yml", 3, '    image: "ghcr.io/formbricks/formbricks:5.0.2"   # 6 needs AuthZed',
         '    image: "ghcr.io/formbricks/formbricks:5"   # 6 needs AuthZed', "formbricks")]))
check("retag: the line brings its newest version", (p["target_version"], p["jump"], p["jump_unknown"]), ("5.4.5", "minor", False))
check("retag --dry-run writes nothing; the plan names the file's checksum", (sha(fb_file), r["changes"][0]["sha256"]),
      (before, before))
p = retag("formbricks-app", "6.0.2")
check("retag: a new major is planned (the page asks for a tick box)", (p["retag"]["possible"], p["retag"]["level"], p["jump"]),
      (True, "major", "major"))
p = retag("formbricks-app", "latest")
check("retag: a floating tag -> what it brings is unknown", (p["retag"]["choice"], p["target_version"], p["jump_unknown"]),
      ("floating", None, True))
check("retag: the configured tag, or one the radar does not offer (older, rc, other word) -> refused",
      why("formbricks-app", "5.0.2", "4.9.3", "5.4.5-rc.1", "edge"), ["same-tag"] + ["not-offered"] * 3)
radar(REC_GT)
check("retag: no choices in the radar data -> refused", why("formbricks-app", "5"), ["no-choices"])
radar(REC_FB, REC_GT, REC_PL, REC_PG, REC_NC, REC_APP)
check("retag: invalid tag -> 64, without --dry-run -> 64",
      (main_rc("retag", "formbricks-app", "5;rm", "--dry-run"), main_rc("retag", "formbricks-app", "5")), (64, 64))
with as_uid(0):
    rc, out = quiet(U.main, ["retag", "formbricks-app", "5", "--dry-run"])
check("retag --dry-run: JSON on stdout, exit 0", (rc, json.loads(out)["retag"]["changes"][0]["line"]), (0, 3))
del D.containers["formbricks-app"]

# Two services of one image; anchors elsewhere in the file do not matter.
D.images["sha256:gt"] = {"Id": "sha256:gt", "Config": {"Env": ["GLITCHTIP_VERSION=6.1.8"]}}
gt_file = write("gt.yml", "x-defaults: &defaults\n  restart: unless-stopped\n\nservices:\n  web:\n    <<: *defaults\n"
                          "    image: glitchtip/glitchtip:latest\n  worker:\n    <<: *defaults\n"
                          "    image: glitchtip/glitchtip:latest  # same build\n    command: ./bin/run-celery.sh\n")
D.add(ctn("glitchtip-web", GT + ":latest", "sha256:gt", project="gt", service="web", files=gt_file))
D.add(ctn("glitchtip-worker", GT + ":latest", "sha256:gt", project="gt", service="worker", files=gt_file))
D.cfg = {"services": {"web": {"image": GT + ":latest"}, "worker": {"image": GT + ":latest"}}}
p = retag("glitchtip-web", "6")
check("retag: every service of the image changes, anchors elsewhere are fine",
      [(c[1], c[3], c[4]) for c in edits(p["retag"])],
      [(7, "    image: glitchtip/glitchtip:6", "web"), (10, "    image: glitchtip/glitchtip:6  # same build", "worker")])
p = retag("glitchtip-web", "6.1.8")
check("retag: pin the running version of latest", (p["retag"]["choice"], p["target_version"], p["jump"]), ("pin", "6.1.8", "same"))
del D.containers["glitchtip-web"], D.containers["glitchtip-worker"]

# The image inside an anchor: the line belongs to no service.
an_file = write("anchor.yml", "x-app: &app\n  image: acme/app:1.0.0\n  restart: always\n\nservices:\n  app:\n    <<: *app\n")
D.add(ctn("anchor-app", "acme/app:1.0.0", "sha256:app", project="an", service="app", files=an_file))
D.cfg = {"services": {"app": {"image": "acme/app:1.0.0"}}}
check("retag: an image under an anchor -> refused", why("anchor-app", "1.0.1"), ["anchors"])
write("anchor.yml", "services:\n  app:\n    build: .\n    image: acme/other:2\n")
check("retag: no line with this image (include:, generated files) -> refused", why("anchor-app", "1.0.1"), ["not-found"])
del D.containers["anchor-app"]

# An override file: the line that gives the service its running image changes,
# in whichever file it is; an older line it overrides stays.
ov_base = write("ov.yml", "services:\n  app:\n    image: acme/app:0.9.0\n    restart: always\n")
ov_over = write("ov.override.yml", "services:\n  app:\n    image: acme/app:1.0.0\n    ports:\n      - 8080:80\n")
D.add(ctn("ov-app", "acme/app:1.0.0", "sha256:app", project="ov", service="app", files=ov_base + "," + ov_over))
D.cfg = {"services": {"app": {"image": "acme/app:1.0.0"}}}
check("retag: the image set in an override file -> its line, the base file's older line stays",
      edits(retag("ov-app", "1.0.1")["retag"]),
      [("ov.override.yml", 3, "    image: acme/app:1.0.0", "    image: acme/app:1.0.1", "app")])
write("ov.yml", "services:\n  app:\n    image: acme/app:1.0.0\n    restart: always\n")
write("ov.override.yml", "services:\n  app:\n    ports:\n      - 8080:80\n")
check("retag: an override file without an image -> the base file's line",
      edits(retag("ov-app", "1.0.1")["retag"]),
      [("ov.yml", 3, "    image: acme/app:1.0.0", "    image: acme/app:1.0.1", "app")])
write("ov.override.yml", "services:\n  app:\n    image: acme/app:1.0.0\n")
check("retag: the same image in both files -> both lines",
      [(c[0], c[1]) for c in edits(retag("ov-app", "1.0.1")["retag"])], [("ov.yml", 3), ("ov.override.yml", 3)])
del D.containers["ov-app"]

# The tag in a variable of the .env file.
pl_env = write("pl.env", "# Plausible\nBASE_URL=https://stats.example.org\nPLAUSIBLE_VERSION=v2.1.4  # keep with the db\n")
pl_file = write("pl.yml", "services:\n  plausible:\n    image: ghcr.io/plausible/community-edition:${PLAUSIBLE_VERSION}\n")
D.add(ctn("plausible", PL + ":v2.1.4", "sha256:pl", project="pl", service="plausible", files=pl_file, env_files=pl_env))
D.cfg = {"services": {"plausible": {"image": PL + ":v2.1.4"}}}
p = retag("plausible", "v2.1.5")
check("retag: a variable of the .env file -> that line changes, the compose file stays",
      (p["retag"]["possible"], p["retag"]["variable"], edits(p["retag"]), p["target_version"], p["jump"]),
      (True, "PLAUSIBLE_VERSION", [("pl.env", 3, "PLAUSIBLE_VERSION=v2.1.4  # keep with the db",
                                    "PLAUSIBLE_VERSION=v2.1.5  # keep with the db", None)], "2.1.5", "patch"))
write("pl.env", 'PLAUSIBLE_IMAGE="ghcr.io/plausible/community-edition:v2.1.4"\n')
write("pl.yml", "services:\n  plausible:\n    image: ${PLAUSIBLE_IMAGE}\n")
check("retag: a variable holding the whole reference",
      [c[3] for c in edits(retag("plausible", "v2.1.5")["retag"])], ['PLAUSIBLE_IMAGE="ghcr.io/plausible/community-edition:v2.1.5"'])
write("pl.env", "PLAUSIBLE_VERSION=v2.1.4\n")
write("pl.yml", "services:\n  plausible:\n    image: ghcr.io/plausible/community-edition:${PLAUSIBLE_VERSION}\n"
                "  cron:\n    image: acme/cron:${PLAUSIBLE_VERSION}\n")
check("retag: a variable used elsewhere too -> refused", why("plausible", "v2.1.5"), ["variable-shared"])
write("pl.env", "PLAUSIBLE_VERSION=v2.1.3\nPLAUSIBLE_VERSION=v2.1.4\n")
write("pl.yml", "services:\n  plausible:\n    image: ghcr.io/plausible/community-edition:${PLAUSIBLE_VERSION}\n")
check("retag: a variable set twice -> refused", why("plausible", "v2.1.5"), ["variable-complex"])
write("pl.env", "BASE_URL=https://stats.example.org\n")
write("pl.yml", "services:\n  plausible:\n    image: ghcr.io/plausible/community-edition:${PLAUSIBLE_VERSION:-v2.1.4}\n")
check("retag: only a default in the compose file -> refused", why("plausible", "v2.1.5"), ["variable-complex"])
write("pl.yml", "services:\n  plausible:\n    image: ghcr.io/plausible/community-edition:${PV}\n")
check("retag: a variable that is not in the .env file -> refused", why("plausible", "v2.1.5"), ["variable-complex"])
del D.containers["plausible"]
write("pl.yml", "services:\n  plausible:\n    image: ghcr.io/plausible/community-edition:${PLAUSIBLE_VERSION}\n")
default_env = os.path.join(COMPOSE_DIR, ".env")
with open(default_env, "w") as f:
    f.write("PLAUSIBLE_VERSION=v2.1.4\n")
D.add(ctn("plausible", PL + ":v2.1.4", "sha256:pl", project="pl", service="plausible", files=pl_file))
check("retag: no env file label -> the .env of the project directory, as compose reads it",
      [(c["file"], c["new"]) for c in retag("plausible", "v2.1.5")["retag"]["changes"]],
      [(default_env, "PLAUSIBLE_VERSION=v2.1.5")])
os.unlink(default_env)
del D.containers["plausible"]

# Databases keep their major; Nextcloud goes one major at a time.
pg_file = write("pg.yml", "services:\n  db:\n    image: postgres:16-alpine\n")
D.add(ctn("pg", "postgres:16-alpine", "sha256:pg", project="pg", service="db", files=pg_file, cmd=["postgres"],
          env=["PG_VERSION=16.14"]))
D.images["sha256:pg"] = {"Id": "sha256:pg", "Config": {"Env": ["PG_VERSION=16.14"]}}
D.cfg = {"services": {"db": {"image": "postgres:16-alpine"}}}
check("retag: a database to another major, also through a line or a floating tag -> refused",
      why("pg", "17.11-alpine", "17-alpine", "alpine"), ["db-major"] * 3)
check("retag: a database within its major", [c[3] for c in edits(retag("pg", "16.15-alpine")["retag"])],
      ["    image: postgres:16.15-alpine"])
del D.containers["pg"]
nc_file = write("nc.yml", "services:\n  app:\n    image: nextcloud:apache\n")
D.add(ctn("nc", "nextcloud:apache", "sha256:nc", project="nc", service="app", files=nc_file))
D.images["sha256:nc"] = {"Id": "sha256:nc", "Config": {"Env": ["NEXTCLOUD_VERSION=34.0.3"]}}
D.cfg = {"services": {"app": {"image": "nextcloud:apache"}}}
check("retag: Nextcloud two majors ahead or to an unknown version -> refused",
      why("nc", "36.0.0-apache", "36-apache", "stable-apache"), ["major-skip"] * 3)
check("retag: Nextcloud one major ahead", (retag("nc", "35.0.1-apache")["retag"]["possible"]), True)
del D.containers["nc"]
D.add(ctn("pinned2", "nginx@sha256:abc", "sha256:n", project="glitchtip", service="pinned2"))
check("retag: what cannot be updated cannot be retagged either", why("pinned2", "1.27"), ["digest-pinned"])
del D.containers["pinned2"]
os.unlink(U.VERSIONS)

# ------------------------------------------------------- retag (the change)
reset_fs()
fresh_docker()
release_lock()
U.systemctl = Systemctl()
U.wait_healthy = lambda project, services: (True, [], {})
radar(REC_FB, REC_PL)


def compose_images(files, env_file=None):
    """What compose would read from the files: {service: image}."""
    def get():
        env = {}
        if env_file:
            for line in open(env_file, encoding="utf-8").read().splitlines():
                m = U.ENV_LINE_RE.match(line)
                if m:
                    env[m["key"]] = m["val"]
        out = {}
        for f in files:
            lines = open(f, encoding="utf-8", newline="").read().splitlines(True)
            for i, line in enumerate(lines):
                m = U.IMAGE_LINE_RE.match(U.bare(line))
                svc = U.service_of_line(lines, i) if m else None
                if svc:
                    out[svc] = U.interpolate(m["val"], env)
        return out
    return get


def raw(path):
    with open(path, "rb") as f:
        return f.read()


def apply(name, tag, fp, *extra):
    with as_uid(0):
        rc, out = quiet(U.main, ["retag", name, tag, "--expect", fp, "--no-dump", *extra])
    release_lock()
    res = [ln for ln in out.splitlines() if ln.startswith("@@RESULT ")]
    return rc, (json.loads(res[-1][9:]) if res else None), out


def leftovers():
    """Rollback tags, backup folders and history entries: all must stay empty
    when a tag change stops before any container changed."""
    bk = os.path.join(U.BASE, "compose")
    return ([t for t in D.tags if ":rollback-" in t], sorted(os.listdir(bk)) if os.path.isdir(bk) else [],
            len(ops()))


for i in ("fb", "fb503", "fb545", "pl", "pl215"):
    D.images["sha256:" + i] = {"Id": "sha256:" + i, "Config": {"Env": []}}
D.tags.update({FB + ":5.0.2": "sha256:fb", FB + ":5.0.3": "sha256:fb503", FB + ":5.4.5": "sha256:fb545",
               PL + ":v2.1.4": "sha256:pl", PL + ":v2.1.5": "sha256:pl215"})
FB_TEXT = ('services:\n  formbricks:\n    image: "ghcr.io/formbricks/formbricks:5.0.2"   # 6 needs AuthZed\n'
           "    restart: always\n  migrate:\n    image: ghcr.io/formbricks/formbricks:5.0.2\n"
           "    command: migrate\n")
fb_file = write("fbx.yml", FB_TEXT, newline="\r\n")
os.chmod(fb_file, 0o640)
FB_BEFORE = raw(fb_file)
D.add(ctn("formbricks-app", FB + ":5.0.2", "sha256:fb", project="fbx", service="formbricks", files=fb_file))
D.add(ctn("formbricks-migrate", FB + ":5.0.2", "sha256:fb", project="fbx", service="migrate", files=fb_file,
          running=False))
D.images_from = compose_images([fb_file])
fp = retag("formbricks-app", "5.4.5")["retag"]["fingerprint"]
check("retag: the dry run gives a fingerprint", bool(U.FP_RE.match(fp or "")), True)
check("retag: invalid fingerprint -> 64, unknown flag -> 64",
      (main_rc("retag", "formbricks-app", "5.4.5", "--expect", "abc"),
       main_rc("retag", "formbricks-app", "5.4.5", "--expect", fp, "--force")), (64, 64))
rc, res, out = apply("formbricks-app", "5.4.5", "0" * 64)
check("retag: another fingerprint than the preview -> exit 5, nothing written or left",
      (rc, raw(fb_file) == FB_BEFORE, leftovers()), (5, True, ([], [], 0)))
with open(fb_file, "ab") as f:
    f.write(b"# edited after the preview\r\n")
rc, res, out = apply("formbricks-app", "5.4.5", fp)
check("retag: a file edited after the preview -> exit 5, nothing changed", (rc, leftovers()), (5, ([], [], 0)))
with open(fb_file, "wb") as f:
    f.write(FB_BEFORE)

rc, res, out = apply("formbricks-app", "5.4.5", fp)
op = ops()[res["op"]]
check("retag: exit 0, status ok, the new tag in the result and the history",
      (rc, res["status"], res["image"], op["kind"], op["from_tag"], op["to_tag"], op["from_version"], op["to_version"]),
      (0, "ok", FB + ":5.4.5", "retag", "5.0.2", "5.4.5", "5.0.2", "5.4.5"))
check("retag: exactly the planned lines changed (quotes, comment, CRLF kept), owner and mode too",
      (raw(fb_file) == FB_BEFORE.replace(b"formbricks:5.0.2", b"formbricks:5.4.5"), oct(os.stat(fb_file).st_mode & 0o777)),
      (True, "0o640"))
check("retag: both services recreated on the new tag",
      sorted((c["Config"]["Image"], c["Image"]) for c in D.containers.values()),
      [(FB + ":5.4.5", "sha256:fb545")] * 2)
bk = op["backup_files"][0]
check("retag: byte-exact backup, root only, in compose/<project>/<stamp>/",
      (raw(bk["backup"]) == FB_BEFORE, oct(os.stat(bk["backup"]).st_mode & 0o777),
       oct(os.stat(op["backup"]).st_mode & 0o777), os.path.dirname(op["backup"]) ==
       os.path.join(U.BASE, "compose", "fbx")),
      (True, "0o600", "0o700", True))
check("retag: the changes and the rollback tag in the history",
      ([c["line"] for c in op["changes"]], op["rollback"] in D.tags and D.tags[op["rollback"]] == "sha256:fb"),
      ([3, 6], True))
with contextlib.redirect_stdout(io.StringIO()) as buf:
    U.cmd_backups()
hist = [o for o in json.loads(buf.getvalue())["ops"] if o["id"] == res["op"]][0]
check("retag: backups lists it as rollable, without the backup file paths",
      (hist["can_rollback"], "backup_files" in hist, hist["to_tag"]), (True, False, "5.4.5"))
check("retag: the same tag again -> refused (exit 5)", apply("formbricks-app", "5.4.5", fp)[0], 5)

with as_uid(0):
    rc, out = quiet(U.main, ["rollback", res["op"]])
release_lock()
check("retag rollback: exit 0, the file byte-identical again, the containers on the old tag and image",
      (rc, raw(fb_file) == FB_BEFORE, sorted((c["Config"]["Image"], c["Image"]) for c in D.containers.values())),
      (0, True, [(FB + ":5.0.2", "sha256:fb")] * 2))

fp = retag("formbricks-app", "5.0.3")["retag"]["fingerprint"]
rc, res, out = apply("formbricks-app", "5.0.3", fp)
with open(fb_file, "ab") as f:
    f.write(b"# edited after the change\r\n")
edited = raw(fb_file)
with as_uid(0):
    rc2, out2 = quiet(U.main, ["rollback", res["op"]])
release_lock()
check("retag rollback: a file edited after the change -> exit 5, nothing touched",
      (rc, rc2, raw(fb_file) == edited, ops()[res["op"]].get("rolled_back")), (0, 5, True, None))
with open(fb_file, "wb") as f:
    f.write(FB_BEFORE)
for c in D.containers.values():
    c["Config"]["Image"], c["Image"] = FB + ":5.0.2", "sha256:fb"
shutil.rmtree(os.path.join(U.BASE, "compose"))
os.unlink(U.OPS)
for t in [t for t in D.tags if ":rollback-" in t]:
    del D.tags[t]

D.images_from = lambda: {"formbricks": FB + ":5.0.2", "migrate": FB + ":5.4.5"}
fp = retag("formbricks-app", "5.4.5")["retag"]["fingerprint"]
rc, res, out = apply("formbricks-app", "5.4.5", fp)
check("retag: compose config does not give the new image -> exit 5, file restored, nothing left",
      (rc, raw(fb_file) == FB_BEFORE, leftovers(), "files restored" in out), (5, True, ([], [], 0), True))
D.images_from = compose_images([fb_file])
del D.tags[FB + ":5.0.3"]
fp = retag("formbricks-app", "5.0.3")["retag"]["fingerprint"]
rc, res, out = apply("formbricks-app", "5.0.3", fp)
check("retag: the new image cannot be pulled -> exit 5, file restored, nothing left",
      (rc, raw(fb_file) == FB_BEFORE, leftovers()), (5, True, ([], [], 0)))

# The helper itself stops after the file was written, before a container
# changed (here in the pull): the file comes back, nothing is left, and the
# entry it had in the history is marked discarded (hidden, no output kept).
fp = retag("formbricks-app", "5.4.5")["retag"]["fingerprint"]
logs_before = sorted(os.listdir(U.LOGS))
D.pull_raises = OSError("docker: connection reset by peer")
with contextlib.redirect_stdout(io.StringIO()), as_uid(0):
    try:
        U.main(["retag", "formbricks-app", "5.4.5", "--expect", fp, "--no-dump"])
        raised = None
    except OSError as e:
        raised = str(e)
release_lock()
D.pull_raises = None
check("retag, the helper stops before the restart -> file restored, nothing left, no output kept",
      (raised, raw(fb_file) == FB_BEFORE, leftovers(), sorted(os.listdir(U.LOGS)) == logs_before),
      ("docker: connection reset by peer", True, ([], [], 0), True))
with open(U.OPS) as f:
    check("retag, the helper stops before the restart: the entry is marked discarded",
          [json.loads(ln).get("status") for ln in f][-2:], ["running", "discarded"])

# A tag change that ends for good after the restart keeps its entry with the
# file copies; a rollback that fails after putting the file back can be tried
# again, and then leaves the file as it is.
fp = retag("formbricks-app", "5.4.5")["retag"]["fingerprint"]
U.wait_healthy = killed
interrupted(["retag", "formbricks-app", "5.4.5", "--expect", fp, "--no-dump"])
op = [o for o in ops().values() if o["status"] == "running"][0]
check("retag ended after the restart: in the history with its file copies, the containers on the new tag",
      (op["to_tag"], len(op["backup_files"]), history(op["id"])["can_rollback"], raw(fb_file) == FB_BEFORE,
       sorted((c["Config"]["Image"], c["Image"]) for c in D.containers.values())),
      ("5.4.5", 1, True, False, [(FB + ":5.4.5", "sha256:fb545")] * 2))
U.wait_healthy = lambda project, services: (False, ["formbricks-app"], {})
with as_uid(0):
    rc1, _ = quiet(U.main, ["rollback", op["id"]])
release_lock()
check("retag rollback fails after putting the file back -> exit 4, file back, offered again",
      (rc1, raw(fb_file) == FB_BEFORE, history(op["id"])["can_rollback"]), (4, True, True))
U.wait_healthy = lambda project, services: (True, [], {})
with as_uid(0):
    rc2, out2 = quiet(U.main, ["rollback", op["id"]])
release_lock()
check("retag rollback tried again, the file back already -> exit 0, file untouched, containers on the old tag",
      (rc2, raw(fb_file) == FB_BEFORE, "Restored:" in out2,
       sorted((c["Config"]["Image"], c["Image"]) for c in D.containers.values())),
      (0, True, False, [(FB + ":5.0.2", "sha256:fb")] * 2))
del D.containers["formbricks-app"], D.containers["formbricks-migrate"]

# The tag in a variable of the .env file: that file changes and comes back.
pl_env = write("plx.env", "PLAUSIBLE_VERSION=v2.1.4  # keep with the db\n")
pl_file = write("plx.yml", "services:\n  plausible:\n    image: ghcr.io/plausible/community-edition:${PLAUSIBLE_VERSION}\n")
ENV_BEFORE, PLY_BEFORE = raw(pl_env), raw(pl_file)
D.add(ctn("plausible", PL + ":v2.1.4", "sha256:pl", project="plx", service="plausible", files=pl_file,
          env_files=pl_env))
D.images_from = compose_images([pl_file], pl_env)
fp = retag("plausible", "v2.1.5")["retag"]["fingerprint"]
rc, res, out = apply("plausible", "v2.1.5", fp)
check("retag via .env: only the variable's line changes, the container runs the new tag",
      (rc, raw(pl_env), raw(pl_file) == PLY_BEFORE, D.containers["plausible"]["Config"]["Image"]),
      (0, b"PLAUSIBLE_VERSION=v2.1.5  # keep with the db\n", True, PL + ":v2.1.5"))
# Once the .env is back, compose must give the old image. Here the compose file
# names a tag of its own since the change: the .env is written back as it was
# before this try, and nothing else changes.
with open(pl_file, "w") as f:
    f.write("services:\n  plausible:\n    image: ghcr.io/plausible/community-edition:v2.1.5\n")
with as_uid(0):
    rc2, out2 = quiet(U.main, ["rollback", res["op"]])
release_lock()
check("retag rollback: another image once the .env is back -> exit 5, the .env as before the try",
      (rc2, raw(pl_env), D.containers["plausible"]["Image"], "instead of " + PL + ":v2.1.4 – nothing changed" in out2),
      (5, b"PLAUSIBLE_VERSION=v2.1.5  # keep with the db\n", "sha256:pl215", True))
with open(pl_file, "wb") as f:
    f.write(PLY_BEFORE)
with as_uid(0):
    rc, out = quiet(U.main, ["rollback", res["op"]])
release_lock()
check("retag via .env: rollback restores the .env byte-identical",
      (rc, raw(pl_env) == ENV_BEFORE, D.containers["plausible"]["Image"]), (0, True, "sha256:pl"))
del D.containers["plausible"]

# An override file: only it changes, the base file stays; the rollback puts it back.
ov_base = write("ovx.yml", "services:\n  plausible:\n    image: ghcr.io/plausible/community-edition:v2.0.0\n"
                           "    restart: always\n")
ov_over = write("ovx.override.yml", "services:\n  plausible:\n    image: ghcr.io/plausible/community-edition:v2.1.4\n")
BASE_BEFORE, OVER_BEFORE = raw(ov_base), raw(ov_over)
D.add(ctn("plausible", PL + ":v2.1.4", "sha256:pl", project="ovx", service="plausible", files=ov_base + "," + ov_over))
D.images_from = compose_images([ov_base, ov_over])
fp = retag("plausible", "v2.1.5")["retag"]["fingerprint"]
rc, res, out = apply("plausible", "v2.1.5", fp)
check("retag with an override file: only its line changes, the container runs the new tag",
      (rc, raw(ov_base) == BASE_BEFORE, raw(ov_over), D.containers["plausible"]["Config"]["Image"],
       [os.path.basename(x["path"]) for x in ops()[res["op"]]["backup_files"]]),
      (0, True, b"services:\n  plausible:\n    image: ghcr.io/plausible/community-edition:v2.1.5\n", PL + ":v2.1.5",
       ["ovx.override.yml"]))
with as_uid(0):
    rc, out = quiet(U.main, ["rollback", res["op"]])
release_lock()
check("retag with an override file: rollback restores it byte-identical",
      (rc, raw(ov_over) == OVER_BEFORE, raw(ov_base) == BASE_BEFORE, D.containers["plausible"]["Image"]),
      (0, True, True, "sha256:pl"))
del D.containers["plausible"]
D.images_from = None
os.unlink(U.VERSIONS)

# ------------------------------------------------- stream (real subprocess)
U.subprocess = subprocess
out = io.StringIO()
with contextlib.redirect_stdout(out):
    rc = REAL_STREAM([sys.executable, "-u", "-c",
                      "import sys; print('step 1'); print('Error: boom', file=sys.stderr); sys.exit(3)"])
check("stream: exit code, output passed through, last lines kept",
      (rc, "step 1\n" in out.getvalue(), "Error: boom\n" in out.getvalue(), U.STREAM_TAIL[-2:]),
      (3, True, True, ["step 1", "Error: boom"]))

# ---------------------------------------------- a run killed after the restart
# A real process with the fake Docker above, killed with SIGKILL once the
# recreated containers are up: no finally runs there. The history entry and
# its rollback copy are written before the restart; the output must be on
# disk as well, line by line from that checkpoint on (Tee). The child says it
# reached the checkpoint through one pipe and then blocks on another that the
# test never writes to: fixed points, no sleeps. Only this child is killed.
KILL_DRIVER = '''
import importlib.util, json, os, subprocess, sys
lib, base, tmp, state, docker_src, ready, hold = sys.argv[1:8]
spec = importlib.util.spec_from_file_location("upd", os.path.join(lib, "diun-update.py"))
U = importlib.util.module_from_spec(spec)
spec.loader.exec_module(U)
U.BASE, U.LOCK = base, os.path.join(tmp, "update.lock")
U.DUMPS, U.OPS, U.LOGS = [os.path.join(base, x) for x in ("dumps", "operations.jsonl", "logs")]
U.CONF, U.VERSIONS = os.path.join(tmp, "image-updates.conf"), os.path.join(tmp, "versions.json")
with open(docker_src, encoding="utf-8") as f:
    exec(f.read())
D = Docker()
with open(state) as f:
    st = json.load(f)
D.containers, D.images, D.tags, D.pull_to = st["containers"], st["images"], st["tags"], tuple(st["pull_to"])


def saved(fn):
    """Every Docker call leaves the fake's state on disk, for the rollback after the kill."""
    def call(*a, **kw):
        try:
            return fn(*a, **kw)
        finally:
            with open(state + ".tmp", "w") as f:
                json.dump({"containers": D.containers, "images": D.images, "tags": D.tags, "pull_to": None}, f)
            os.replace(state + ".tmp", state)
    return call


def checkpoint(project, services):
    print("Waiting for the recreated containers (test checkpoint)", flush=True)
    os.write(int(ready), b"up")
    os.read(int(hold), 1)


U.run, U.stream = saved(D.run), saved(D.stream)
U.systemctl = lambda *a, timeout=60: subprocess.CompletedProcess(["systemctl", *a], 1, "", "")
U.wait_healthy = checkpoint
os.geteuid = lambda: 0
sys.exit(U.main(["update", "killed-app", "--no-dump"]))
'''
reset_fs()
KILL_DIR = os.path.join(TMP, "killed")
os.makedirs(KILL_DIR)
kill_file = os.path.join(KILL_DIR, "compose.yml")
open(kill_file, "w").close()
kill_state = os.path.join(KILL_DIR, "docker.json")
with open(kill_state, "w") as f:
    json.dump({"containers": {"killed-app": ctn("killed-app", "acme/app:1", "sha256:app1", project="killp",
                                                service="app", files=kill_file)},
               "tags": {"acme/app:1": "sha256:app1"},
               "images": {"sha256:app1": {"Id": "sha256:app1", "Config": {"Env": ["APP_VERSION=1.0.1"]}},
                          "sha256:app2": {"Id": "sha256:app2", "Config": {"Env": ["APP_VERSION=1.0.2"]}}},
               "pull_to": ["acme/app:1", "sha256:app2"]}, f)
kill_src = os.path.join(KILL_DIR, "docker_fake.py")
with open(kill_src, "w", encoding="utf-8") as f:
    f.write(pyinspect.getsource(Docker))
kill_driver = os.path.join(KILL_DIR, "driver.py")
with open(kill_driver, "w", encoding="utf-8") as f:
    f.write(KILL_DRIVER)
ready_r, ready_w = os.pipe()
hold_r, hold_w = os.pipe()
kill_out = os.path.join(KILL_DIR, "stdout.txt")
with open(kill_out, "w") as out_f:
    child = subprocess.Popen([sys.executable, kill_driver, ROOT, U.BASE, TMP, kill_state, kill_src,
                              str(ready_w), str(hold_r)], stdout=out_f, stderr=subprocess.STDOUT,
                             pass_fds=(ready_w, hold_r), env={**os.environ, "DIUN_LANG": "en"})
os.close(ready_w)
os.close(hold_r)
ready, _, _ = select.select([ready_r], [], [], 120)
signalled = os.read(ready_r, 2) if ready else b""
child.send_signal(signal.SIGKILL)
rc = child.wait(timeout=60)
os.close(ready_r)
os.close(hold_w)
with open(kill_out) as f:
    child_out = f.read()
op = ([o for o in ops().values() if o.get("container") == "killed-app"] or [{}])[0]
logf = os.path.join(U.LOGS, op.get("id", "none") + ".log")
kept = open(logf).read() if os.path.exists(logf) else ""
check("killed after the restart: the child reached the checkpoint and died of SIGKILL",
      (signalled, rc), (b"up", -signal.SIGKILL))
if signalled != b"up":
    print(child_out)
check("killed after the restart: in the history as running, with the new image and its rollback copy",
      (op.get("status"), op.get("new_id"), op.get("pid") == child.pid, bool(op.get("rollback"))),
      ("running", "sha256:app2", True, True))
check("killed after the restart: its output kept up to the checkpoint, root only, without @@RESULT",
      ("Updating acme/app:1 in project killp" in kept, "Rollback image: acme/app:rollback-" in kept,
       "Waiting for the recreated containers (test checkpoint)" in kept, "@@RESULT" in kept,
       oct(os.stat(logf).st_mode & 0o777) if kept else None), (True, True, True, False, "0o600"))
check("killed after the restart: the live output went through as before",
      ("Updating acme/app:1" in child_out, "(test checkpoint)" in child_out), (True, True))
fresh_docker()
with open(kill_state) as f:
    st = json.load(f)
D.containers, D.images, D.tags = st["containers"], st["images"], st["tags"]
U.wait_healthy = lambda project, services: (True, [], {})
U.systemctl = Systemctl()
h = history(op["id"])
check("killed after the restart: the rollback is offered (its process is gone)",
      (h["status"], h["active"], h["can_rollback"], D.containers["killed-app"]["Image"]),
      ("running", False, True, "sha256:app2"))
with as_uid(0):
    rc, _ = quiet(U.main, ["rollback", op["id"]])
release_lock()
check("killed after the restart: rollback -> exit 0, the old image again, the update's output unchanged",
      (rc, D.containers["killed-app"]["Image"], ops()[op["id"]]["rolled_back"], open(logf).read() == kept,
       os.path.exists(os.path.join(U.LOGS, op["id"] + ".rollback.log"))), (0, "sha256:app1", True, True, True))

# ------------------------------------- signals to a real run, real commands
# The helper as the command line runs it (entry), with the fake Docker above
# but a real process for the dump and for `compose up`: SIGTERM while the dump
# runs (before any change), SIGHUP while the containers are recreated (after
# the change). The commands report through a FIFO once they run; at that point
# the state before the signal is read, then the signal is sent.
SIG_DRIVER = '''
import importlib.util, json, os, subprocess, sys
lib, base, tmp, state, docker_src = sys.argv[1:6]
spec = importlib.util.spec_from_file_location("upd", os.path.join(lib, "diun-update.py"))
U = importlib.util.module_from_spec(spec)
spec.loader.exec_module(U)
U.BASE, U.LOCK = base, os.path.join(tmp, "update.lock")
U.DUMPS, U.OPS, U.LOGS = [os.path.join(base, x) for x in ("dumps", "operations.jsonl", "logs")]
U.CONF, U.VERSIONS = os.path.join(tmp, "image-updates.conf"), os.path.join(tmp, "versions.json")
with open(docker_src, encoding="utf-8") as f:
    exec(f.read())
D = Docker()
with open(state) as f:
    st = json.load(f)
D.containers, D.images, D.tags, D.pull_to = st["containers"], st["images"], st["tags"], tuple(st["pull_to"])
real_stream, real_copy = U.stream, U.copy_out


def saved(fn):
    def call(*a, **kw):
        try:
            return fn(*a, **kw)
        finally:
            with open(state + ".tmp", "w") as f:
                json.dump({"containers": D.containers, "images": D.images, "tags": D.tags, "pull_to": None}, f)
            os.replace(state + ".tmp", state)
    return call


def copy(cmd, sink, limit, errfile, grace=10, keep_fds=()):
    return real_copy(["sh", "-c", os.environ["DUMP_SCRIPT"]], sink, limit, errfile, grace, keep_fds)


def stream(cmd):
    if "up" in cmd:
        D.stream(cmd)       # the fake recreates the containers, the real command runs on
        return real_stream(["sh", "-c", os.environ["UP_SCRIPT"]])
    return D.stream(cmd)


U.copy_out = copy
U.run, U.stream = saved(D.run), saved(stream)
U.systemctl = lambda *a, timeout=60: subprocess.CompletedProcess(["systemctl", *a], 1, "", "")
os.geteuid = lambda: 0
sys.exit(U.entry(sys.argv[6:]))
'''
SIG_DIR = os.path.join(TMP, "signals")
os.makedirs(SIG_DIR)
sig_file = os.path.join(SIG_DIR, "compose.yml")
open(sig_file, "w").close()
sig_state = os.path.join(SIG_DIR, "docker.json")
sig_src = os.path.join(SIG_DIR, "docker_fake.py")
with open(sig_src, "w", encoding="utf-8") as f:
    f.write(pyinspect.getsource(Docker))
sig_driver = os.path.join(SIG_DIR, "driver.py")
with open(sig_driver, "w", encoding="utf-8") as f:
    f.write(SIG_DRIVER)
EV = os.path.join(SIG_DIR, "events")
os.mkfifo(EV)
ev_r = os.open(EV, os.O_RDONLY | os.O_NONBLOCK)
ev_keep = os.open(EV, os.O_WRONLY)
SIG_ENV = {**os.environ, "DIUN_LANG": "en", "EV": EV,
           "DUMP_SCRIPT": 'printf -- "--\\n-- PostgreSQL database cluster dump\\n"; echo "dump $$" > "$EV"; exec sleep 600',
           "UP_SCRIPT": 'echo " Container sig-app  Recreate"; echo "up $$" > "$EV"; '
                        'while :; do echo " Container sig-app  Starting"; sleep 0.05; done'}


def sig_event(within=120):
    """(word, pid) the command wrote to the FIFO once it ran."""
    buf = b""
    end = time.monotonic() + within
    while not buf.endswith(b"\n"):
        left = end - time.monotonic()
        if left <= 0 or not select.select([ev_r], [], [], left)[0]:
            return None, None
        buf += os.read(ev_r, 4096)
    word, pid = buf.decode().split()[:2]
    return word, int(pid)


def lock_free():
    with open(U.LOCK, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fh, fcntl.LOCK_UN)
        return True


def pid_gone(pid, within=10):
    end = time.monotonic() + within
    while time.monotonic() < end:
        try:
            with open(f"/proc/{pid}/stat") as f:
                if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                    return True
        except (FileNotFoundError, ProcessLookupError):
            return True
        time.sleep(0.02)
    return False


def sig_run(argv, sig):
    """Run the helper until its command reports, read the state, send sig.
    Returns (word, before, exit code, output, pid of the command)."""
    reset_fs()
    with open(sig_state, "w") as f:
        json.dump({"containers": {
            "sig-app": ctn("sig-app", "acme/sig:1", "sha256:sig1", project="sigp", service="app", files=sig_file),
            "sig-db": ctn("sig-db", "postgres:16", "sha256:pg16", project="sigp", service="db", files=sig_file,
                          cmd=["postgres"])},
            "tags": {"acme/sig:1": "sha256:sig1", "postgres:16": "sha256:pg16"},
            "images": {"sha256:sig1": {"Id": "sha256:sig1", "Config": {"Env": ["SIG_VERSION=1.0.1"]}},
                       "sha256:sig2": {"Id": "sha256:sig2", "Config": {"Env": ["SIG_VERSION=1.0.2"]}},
                       "sha256:pg16": {"Id": "sha256:pg16", "Config": {"Env": ["PG_VERSION=16.4"]}}},
            "pull_to": ["acme/sig:1", "sha256:sig2"]}, f)
    out_path = os.path.join(SIG_DIR, "stdout.txt")
    with open(out_path, "w") as out_f:
        child = subprocess.Popen([sys.executable, sig_driver, ROOT, U.BASE, TMP, sig_state, sig_src, *argv],
                                 stdout=out_f, stderr=subprocess.STDOUT, env=SIG_ENV)
    word, pid = sig_event()
    pdir = os.path.join(U.DUMPS, "sigp")
    before = {"part": [f for f in (os.listdir(pdir) if os.path.isdir(pdir) else []) if f.endswith(".part")],
              "ops": {o["id"]: o["status"] for o in ops().values() if o.get("container") == "sig-app"},
              "logs": sorted(os.listdir(U.LOGS)) if os.path.isdir(U.LOGS) else [],
              "lock_free": lock_free(), "running": child.poll() is None}
    child.send_signal(sig)
    rc = child.wait(timeout=120)
    with open(out_path) as f:
        return word, before, rc, f.read(), pid


word, before, rc, out, pid = sig_run(["update", "sig-app"], signal.SIGTERM)
check("SIGTERM while the dump runs: before, a partial dump, no history entry, the lock held",
      (word, len(before["part"]), before["ops"], before["lock_free"], before["running"]), ("dump", 1, {}, False, True))
with open(sig_state) as f:
    st = json.load(f)
check("SIGTERM while the dump runs: exit 5, 'nothing changed', the dump process gone, no partial dump, no "
      "history entry, no rollback tag, the lock free",
      (rc, "Stopped by SIGTERM before any container changed – the dumps begun are deleted, nothing changed." in out,
       pid_gone(pid), dumps_of("sigp"), [o for o in ops().values() if o.get("container") == "sig-app"],
       [t for t in st["tags"] if ":rollback-" in t], lock_free()),
      (5, True, True, [], [], [], True))
if rc != 5:
    print(out)

word, before, rc, out, pid = sig_run(["update", "sig-app", "--no-dump"], signal.SIGHUP)
op_id = next(iter(before["ops"]), None)
check("SIGHUP while the containers are recreated: before, in the history as running, its log, the lock held",
      (word, list(before["ops"].values()), before["logs"], before["lock_free"], before["running"]),
      ("up", ["running"], [f"{op_id}.log"], False, True))
res = last_result(out) if "@@RESULT" in out else {}
op = ops().get(op_id, {})
logf = os.path.join(U.LOGS, f"{op_id}.log")
kept = open(logf).read() if os.path.exists(logf) else ""
check("SIGHUP while the containers are recreated: exit 4, failed with the signal as cause, the command gone, "
      "the lock free",
      (rc, res.get("status"), op.get("status"), (op.get("reason") or {}).get("source"), pid_gone(pid), lock_free()),
      (4, "failed", "failed", "signal", True, True))
if rc != 4:
    print(out)
check("SIGHUP while the containers are recreated: the log has the command's output, the cause, no @@RESULT",
      ("Container sig-app  Recreate" in kept, "Container sig-app  Starting" in kept,
       "diun-update.py got SIGHUP and stopped the restart" in kept, "Update failed. A rollback is possible." in kept,
       "@@RESULT" in kept), (True, True, True, True, False))
fresh_docker()
with open(sig_state) as f:
    st = json.load(f)
D.containers, D.images, D.tags = st["containers"], st["images"], st["tags"]
U.wait_healthy = lambda project, services: (True, [], {})
U.systemctl = Systemctl()
check("SIGHUP while the containers are recreated: the rollback is offered",
      (history(op_id)["can_rollback"], D.containers["sig-app"]["Image"]), (True, "sha256:sig2"))
with as_uid(0):
    rc, _ = quiet(U.main, ["rollback", op_id])
release_lock()
check("after the signal: the rollback runs, the old image again", (rc, D.containers["sig-app"]["Image"]),
      (0, "sha256:sig1"))
os.close(ev_r)
os.close(ev_keep)

shutil.rmtree(TMP, ignore_errors=True)
print("\nALL CHECKS PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

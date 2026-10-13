#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Offline tests for lib/diun-update.py: Docker, docker compose and systemctl
are replaced by fakes, /var/backups/image-updates and the other paths by a
temporary directory. Covers database detection, plan reasons and the unknown jump
size, argument checks and exit codes, the update flow (dump, rollback tag,
versions in the history, cleanup, radar start), the most likely cause of a
failure, the kept output and the digests of the new build, rollback, the
cleanup rules and the radar verb.
No Docker, no root, no network."""
import contextlib
import gzip
import importlib.util
import io
import json
import os
import shutil
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


# ------------------------------------------------------------- fake Docker
L = {"project": U.L_PROJECT, "service": U.L_SERVICE, "workdir": U.L_WORKDIR,
     "files": U.L_FILES, "hash": U.L_HASH}
COMPOSE_DIR = os.path.join(TMP, "glitchtip")
os.makedirs(COMPOSE_DIR)
open(os.path.join(COMPOSE_DIR, "compose.yml"), "w").close()


def ctn(name, ref, img, project=None, service=None, cmd=None, entrypoint=None, env=(),
        running=True, files=None):
    labels = {}
    if project:
        labels = {L["project"]: project, L["service"]: service or name, L["workdir"]: COMPOSE_DIR,
                  L["files"]: files or os.path.join(COMPOSE_DIR, "compose.yml"),
                  L["hash"]: "h-" + (service or name)}
    return {"Name": "/" + name, "Image": img,
            "Config": {"Image": ref, "Labels": labels, "Env": list(env), "Cmd": cmd, "Entrypoint": entrypoint},
            "State": {"Running": running, "Status": "running" if running else "exited"},
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}}}


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
            return json.dumps(self.cfg), 0, ""
        if cmd[:2] == ["docker", "inspect"]:
            names = cmd[4:] if cmd[2:4] == ["--type", "container"] else cmd[2:]
            found = [self.containers[n] for n in names if n in self.containers]
            if len(found) != len(names):
                return "", 1, "Error: No such object"
            return json.dumps(found), 0, ""
        if cmd[:3] == ["docker", "ps", "-a"]:
            want = [cmd[i + 1][len("label="):].split("=", 1) for i, a in enumerate(cmd) if a == "--filter"]
            names = [n for n, c in self.containers.items()
                     if all((c["Config"]["Labels"] or {}).get(k) == v for k, v in want)]
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

    def run(self, cmd, check=True, capture=True, timeout=600):
        self.calls.append(list(cmd))
        out, rc, err = self.handle(list(cmd))
        if check and rc != 0:
            raise subprocess.CalledProcessError(rc, cmd, out, err)
        return out

    def stream(self, cmd):
        self.streamed.append(list(cmd))
        if "pull" in cmd and self.pull_to:
            ref, iid = self.pull_to
            self.tags[U.norm_ref(ref)] = iid
        if "up" in cmd:
            if self.up_rc:
                U.STREAM_TAIL[:] = self.up_out
                return self.up_rc
            for c in self.containers.values():
                iid = self.tags.get(U.norm_ref(c["Config"]["Image"]))
                svc = (c["Config"]["Labels"] or {}).get(U.L_SERVICE)
                if iid and svc in cmd:
                    c["Image"] = iid
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
    """docker exec <db> sh -c <dump script>: data on stdout, exit code."""
    script = {}

    def __init__(self, cmd, stdout=None, stderr=None):
        data, self.rc = FakePopen.script[cmd[2]]
        self.stdout = io.BytesIO(data)
        if self.rc and stderr is not None:
            stderr.write(b"mariadb-dump: Got error: 1045: Access denied for user 'root'@'localhost'")

    def wait(self):
        return self.rc


class _Subprocess:
    """subprocess for the module under test, with the fake Popen."""
    Popen = FakePopen

    def __getattr__(self, name):
        return getattr(subprocess, name)


U.subprocess = _Subprocess()

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
check("plan: version from the variable, the tag fixes one block -> size of the jump known",
      (p["installed_version"], p["tag_digits"], p["jump_unknown"]), ("16.14", 1, False))
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
os.unlink(U.VERSIONS)
del D.containers["formbricks-app"]

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
U.wait_healthy = lambda project, services: (True, [], {})
with as_uid(0):
    rc, _ = quiet(U.main, ["rollback", res["op"]])
release_lock()
with as_uid(0):
    rc, out = quiet(U.main, ["log", res["op"]])
check("log: the update, then its rollback",
      (rc, out.index("Updating") < out.index("--- Rollback ---") < out.index("Rolling")), (0, True))
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
check("backups: history with versions, without paths",
      [(o["from_version"], o["to_version"], "workdir" in o, "new_id" in o) for o in bk["ops"]], [("16.3", "16.4", False, False)])
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
check("health: still starting -> timeout", state("running", "starting"), (False, ["x"]))

# ------------------------------------------------- stream (real subprocess)
U.subprocess = subprocess
out = io.StringIO()
with contextlib.redirect_stdout(out):
    rc = REAL_STREAM([sys.executable, "-u", "-c",
                      "import sys; print('step 1'); print('Error: boom', file=sys.stderr); sys.exit(3)"])
check("stream: exit code, output passed through, last lines kept",
      (rc, "step 1\n" in out.getvalue(), "Error: boom\n" in out.getvalue(), U.STREAM_TAIL[-2:]),
      (3, True, True, ["step 1", "Error: boom"]))

shutil.rmtree(TMP, ignore_errors=True)
print("\nALL CHECKS PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

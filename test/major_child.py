#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The child process of test/major_recovery_test.py: runs the database major
(migrate) or its rollback (cmd_rollback) of diun-update.py in a process of its
own, as the helper does (on_signals), with Docker and compose stood in for
and the data of the database in a temporary directory. The test stops it with
a real signal at a chosen rename: after rename n the child says so on a pipe
and waits until the test has sent the signal and answers (a synchronisation
point, no timing).

    major_child.py <dir>        settings in <dir>/child.json:
      action       "migrate" or "rollback" (with "op": the operation's id)
      type         "bind" or "volume": where the data lies
      stop_at      the rename after which the child waits (with ready_fd, go_fd)
      fail         renames (by attempt) that fail with an OSError
      probe_fail   the trial run's restore gives an error
      engine       "postgres" (default) or "mariadb"

What happened goes to <dir>/calls.jsonl: every docker command, and for each
`up` of the database the files of its data directory at that moment."""
import errno
import importlib.util
import json
import os
import sys

DIR = sys.argv[1]
CFG = json.load(open(os.path.join(DIR, "child.json")))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(os.path.dirname(HERE), "lib")
sys.path.insert(0, ROOT)
spec = importlib.util.spec_from_file_location("diun_update_child", os.path.join(ROOT, "diun-update.py"))
U = importlib.util.module_from_spec(spec)
spec.loader.exec_module(U)
import diunproc  # noqa: E402  (the module of the signals, as diun-update.py imported it)

U.BASE = os.path.join(DIR, "backups")
U.DUMPS = os.path.join(U.BASE, "dumps")
U.OPS = os.path.join(U.BASE, "operations.jsonl")
U.LOGS = os.path.join(U.BASE, "logs")
U.LOCK = os.path.join(DIR, "update.lock")
U.CONF = os.path.join(DIR, "image-updates.conf")
U.VERSIONS = os.path.join(DIR, "versions.json")
U.LANG = "en"

SRC = CFG["src"]                      # the data directory of the database (a volume's _data or a bind mount)
VOLUMES = os.path.join(DIR, "volumes")
CALLS = os.path.join(DIR, "calls.jsonl")


def tree(path):
    """{relative path: content, or "/" for a directory}, every level."""
    out = {}
    for root, dirs, files in os.walk(path):
        for d in dirs:
            out[os.path.relpath(os.path.join(root, d), path)] = "/"
        for f in files:
            with open(os.path.join(root, f)) as fh:
                out[os.path.relpath(os.path.join(root, f), path)] = fh.read()
    return out


def log_call(cmd, **kw):
    with open(CALLS, "a") as f:
        f.write(json.dumps(dict(cmd=cmd, **kw)) + "\n")
        f.flush()
        os.fsync(f.fileno())


def write_tree(path, files):
    for rel, content in files.items():
        p = os.path.join(path, rel)
        if content == "/":
            os.makedirs(p, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(content)


ENGINE = CFG.get("engine") or "postgres"
# What the server writes into an empty data directory on its first start: the
# new major's cluster (migrate) or a fresh, empty one of the old major (rollback).
NEW_CLUSTER = {"17": {"PG_VERSION": "17\n", "base": "/", "base/5": "/", "base/5/16384": "rows of the new major",
                      "global": "/", "global/pg_control": "control 17", "pg_wal": "/",
                      "pg_wal/000000010000000000000001": "wal 17"},
               "16": {"PG_VERSION": "16\n", "base": "/", "global": "/", "global/pg_control": "a fresh, empty cluster"},
               "12": {"mysql_upgrade_info": "12.3.3-MariaDB", "ibdata1": "innodb 12", "mysql": "/",
                      "mysql/global_priv.MAD": "users 12", "appdb": "/", "appdb/notes.ibd": "rows of the new major"},
               "11": {"mysql_upgrade_info": "11.8.9-MariaDB", "ibdata1": "a fresh, empty server", "mysql": "/"}}
NEW_MAJOR, OLD_MAJOR = ("12", "11") if ENGINE == "mariadb" else ("17", "16")


def stream(cmd):
    if "up" in cmd and "db" in cmd:
        # The image starts on the data directory; an empty one gets a new cluster (initdb).
        before = tree(SRC)
        if not before:
            write_tree(SRC, NEW_CLUSTER[NEW_MAJOR if CFG["action"] == "migrate" else OLD_MAJOR])
        log_call(cmd, data=before)
    else:
        log_call(cmd)
    return 0


def run(cmd, check=True, capture=True, timeout=600, **kw):
    log_call(cmd)
    if cmd[:3] == ["docker", "volume", "create"]:
        os.makedirs(os.path.join(VOLUMES, cmd[-1], "_data"))
        with open(os.path.join(VOLUMES, cmd[-1], "label"), "w") as f:
            f.write(cmd[cmd.index("--label") + 1].split("=", 1)[1])
        return cmd[-1] + "\n"
    if cmd[:3] == ["docker", "volume", "inspect"]:
        name = cmd[-1]
        if not os.path.isdir(os.path.join(VOLUMES, name)):
            if check:
                raise U.subprocess.CalledProcessError(1, cmd, "", "no such volume")
            return "[]"
        if "{{.Mountpoint}}" in cmd:
            return os.path.join(VOLUMES, name, "_data") + "\n"
        if any("image-updates.op" in a for a in cmd):
            return open(os.path.join(VOLUMES, name, "label")).read() + "\n"
        return json.dumps([{"Name": name, "Mountpoint": os.path.join(VOLUMES, name, "_data")}])
    if cmd[:3] == ["docker", "volume", "rm"]:
        # As Docker does: the volume goes with its data.
        U.shutil.rmtree(os.path.join(VOLUMES, cmd[-1]))
        return cmd[-1] + "\n"
    if cmd[:2] == ["docker", "compose"] and "config" in cmd:
        return json.dumps({"name": "app", "services": {"db": {"image": OLD_IMAGE}, "web": {"image": "acme/web:1"}}})
    if cmd[:2] == ["docker", "exec"] and "select version()" in cmd[-1]:
        return "11.8.9-MariaDB-ubu2404\n"
    if cmd[:2] == ["du", "-sb"]:
        return "1\t" + cmd[2] + "\n"
    return ""


OLD_IMAGE, NEW_IMAGE = ("mariadb:11", "mariadb:12") if ENGINE == "mariadb" else ("postgres:16-alpine", "postgres:17.6-alpine")
ENV = ["MARIADB_ROOT_PASSWORD=the-root-secret"] if ENGINE == "mariadb" else []


def run_env(cmd, env, timeout=300):
    """docker run with values in its environment: the names are logged, never the values."""
    log_call(cmd, env=sorted(env), secret_in_cmd=any(v and v in a for v in env.values() for a in cmd))
    return ""


U.run = run
U.run_env = run_env
U.stream = stream
U.inspect = lambda name: {"Name": "/" + name, "Image": "sha256:old", "Config": {"Image": OLD_IMAGE, "Env": list(ENV)},
                          "State": {"Running": True, "Status": "running"}}
U.image_id = lambda ref: ("sha256:new" if ref == NEW_IMAGE else "sha256:old") if ref else ""
U.missing_extensions = lambda ref, used: []
U.dump_db = lambda project, db, st: f"{project}/{st}_{db['name']}.sql.gz"
U.table_counts = lambda name, engine="postgres": {"postgres": {"public.t": 3}}
U.change_files = lambda op, ctx, rt, services, st: U.record(op, log=op["id"] + ".log")
U.wait_ready = lambda name, engine="postgres": True
U.after_restore = lambda name, engine: log_call(["after_restore", name, engine])
U.restore_into = lambda name, rel, engine="postgres": (['psql:<stdin>:9: ERROR:  stand-in error']
                                                       if CFG.get("probe_fail") and "-probe-" in name else [])
U.version_of = lambda ref, iid: ("12.3.3" if ENGINE == "mariadb" else "17.6") if iid == "sha256:new" else \
    ("11.8.9" if ENGINE == "mariadb" else "16.14")
U.wait_healthy = lambda project, services: (True, [], {})
U.start_radar = lambda: None
U.show_logs = lambda names: None
U.not_on = lambda project, services, image: []

# The renames: count them, fail some, stop after one until the test has sent its signal.
real_rename = os.rename
attempts = [0]


def rename(a, b):
    attempts[0] += 1
    n = attempts[0]
    if n in CFG.get("fail", []):
        raise OSError(errno.EIO, "injected failure", a)
    real_rename(a, b)
    if n == CFG.get("stop_at"):
        os.write(CFG["ready_fd"], b"r")
        os.read(CFG["go_fd"], 1)          # the test answers once the signal is sent (SIGKILL: never)
        g = diunproc._guard
        print(f"@@SIGNAL-WHILE-HELD {g is not None and (g.pending is not None or g.got is not None)}", flush=True)


os.rename = rename

DEST = "/var/lib/mysql" if ENGINE == "mariadb" else "/var/lib/postgresql/data"
if CFG["type"] == "volume":
    mount = {"type": "volume", "name": "app_pgdata", "driver": "local", "source": SRC, "destination": DEST}
else:
    mount = {"type": "bind", "name": None, "driver": None, "source": SRC, "destination": DEST}
PLAN = {"container": "db", "image": OLD_IMAGE, "services": ["db"], "containers": ["db"], "project": "app",
        "installed_version": "11.8.9" if ENGINE == "mariadb" else "16.14", "free_bytes": 50 << 30,
        "dbs": [{"name": "db", "kind": "mysql" if ENGINE == "mariadb" else "postgres", "running": True, "size": 1}],
        "dependents": [{"service": "web", "containers": ["web"]}],
        "retag": {"image": NEW_IMAGE, "from_tag": OLD_IMAGE.split(":")[1], "tag": NEW_IMAGE.split(":")[1], "changes": []},
        "major": {"engine": ENGINE, "from_major": int(OLD_MAJOR), "to_major": int(NEW_MAJOR), "data": mount,
                  "pgdata": None if ENGINE == "mariadb" else DEST, "datadir": DEST if ENGINE == "mariadb" else None,
                  "stop": ["web"], "extensions": [], "data_bytes": 1}}
CTX = {"project": "app", "workdir": DIR, "files": [os.path.join(DIR, "compose.yml")], "env_files": []}

rc = None
try:
    with U.on_signals():
        if CFG["action"] == "migrate":
            rc = U.migrate("db", PLAN, CTX)
        else:
            rc = U.cmd_rollback(CFG["op"])
except U.Abort as e:
    print(str(e))
    rc = e.code
except diunproc.Interrupted as e:
    print("Interrupted", e.name)
    rc = 130
print(f"@@RC {rc}", flush=True)
sys.exit(rc)

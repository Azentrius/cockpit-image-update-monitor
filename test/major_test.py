#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Offline tests for the database major of diun-update.py (stage 5): the plan
and every reason it refuses (make_major, with Docker and compose stood in
for), where a new major keeps its data (PostgreSQL 18), the counts and the
error lines of a restore, roles with MD5 passwords, the trial run in a
throwaway container (probe_restore), the parts that move data (a place next to it,
renames that never replace a file, a place removed only while empty), which
kept places may ever be deleted, and the refusals of delete-olddata. A major
and its rollback stopped half way by real signals are in
test/major_recovery_test.py; the run itself (dump, restore, rollback, release
against real PostgreSQL 16 and 17, also stopped while it moves the data) is
section 17 of test/e2e-server.sh."""
import copy
import importlib.util
import io
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(os.path.dirname(HERE), "lib")
sys.path.insert(0, ROOT)
spec = importlib.util.spec_from_file_location("diun_update_major", os.path.join(ROOT, "diun-update.py"))
U = importlib.util.module_from_spec(spec)
spec.loader.exec_module(U)

fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(("OK   " if ok else "FAIL ") + label + ("" if ok else f"\n     got  {got!r}\n     want {want!r}"))


# ------------------------------------------------------------- helpers ----
check("major_of", [U.major_of(v) for v in ("16.14", "17", None, "x")], [16, 17, None, None])
check("inside", [U.inside("/var/lib/postgresql/data", "/var/lib/postgresql/data"),
                 U.inside("/var/lib/postgresql/data/pgdata", "/var/lib/postgresql/data"),
                 U.inside("/var/lib/postgresql/18/docker", "/var/lib/postgresql/data"),
                 U.inside("/var/lib/postgresql/18/docker", "/var/lib/postgresql"),
                 U.inside("/var/lib/postgresql/database", "/var/lib/postgresql/data")],
      [True, True, False, True, False])
MOUNTS = [{"Type": "volume", "Name": "x_conf", "Driver": "local", "Source": "/v/conf", "Destination": "/etc/x"},
          {"Type": "volume", "Name": "x_pg", "Driver": "local", "Source": "/v/pg", "Destination": "/var/lib/postgresql"},
          {"Type": "bind", "Source": "/srv/pg", "Destination": "/var/lib/postgresql/data"}]
check("data_mount: the longest destination that holds PGDATA",
      U.data_mount({"Mounts": MOUNTS}, "/var/lib/postgresql/data/pgdata"),
      {"type": "bind", "name": None, "driver": None, "source": "/srv/pg", "destination": "/var/lib/postgresql/data"})
check("data_mount: none", U.data_mount({"Mounts": MOUNTS[:1]}, "/var/lib/postgresql/data"), None)

BEFORE = {"appdb": {"public.notes": 300, "public.empty": 0}, "shop": {"public.items": 5000}}
check("compare_counts: the same", U.compare_counts(BEFORE, copy.deepcopy(BEFORE)), [])
after = copy.deepcopy(BEFORE)
after["shop"]["public.items"] = 4999
del after["appdb"]["public.empty"]
after["appdb"]["public.new"] = 1
check("compare_counts: a row less, a table gone, one more",
      U.compare_counts(BEFORE, after),
      ["appdb: public.empty 0 -> -", "appdb: public.new - -> 1", "shop: public.items 5000 -> 4999"])
check("count_summary (en)", U.count_summary(BEFORE), "2 databases, 3 tables, 5300 rows")
STDERR = """psql:<stdin>:14: ERROR:  role "owner" already exists
psql:<stdin>:40: ERROR:  extension "vector" is not available
psql:<stdin>:41: NOTICE:  something else
psql:<stdin>:90: ERROR:  relation "items" does not exist"""
check("restore_errors: the role the new cluster has already is no error",
      U.restore_errors(STDERR), ['psql:<stdin>:40: ERROR:  extension "vector" is not available',
                                 'psql:<stdin>:90: ERROR:  relation "items" does not exist'])

# ------------------------------------------------- which kept data may go ----
for kept, want in (({"type": "volume", "name": "glitchtip_pgdata-pg16-20261020-120000"}, True),
                   ({"type": "volume", "name": "glitchtip_pgdata"}, False),
                   ({"type": "volume", "name": "../x.pg16-20261020-120000"}, False),
                   ({"type": "bind", "path": "/srv/pg.pg16-20261020-120000"}, True),
                   ({"type": "bind", "path": "/srv/pg"}, False),
                   ({"type": "bind", "path": "srv/pg.pg16-20261020-120000"}, False),
                   ({"type": "bind", "path": "/srv/../etc.pg16-20261020-120000"}, False),
                   ({"type": "other", "path": "/srv/pg.pg16-20261020-120000"}, False), (None, False)):
    check(f"kept_ok {kept}", U.kept_ok(kept), want)

# ------------------------------------------------- moving data aside ----
TMP = tempfile.mkdtemp(prefix="major-test-")
src = os.path.join(TMP, "pg")
os.mkdir(src, 0o700)
for f in ("PG_VERSION", "base", "global"):
    p = os.path.join(src, f)
    os.mkdir(p) if f != "PG_VERSION" else open(p, "w").write("16\n")
open(os.path.join(src, "base", "1"), "w").write("data")
kept = U.make_place({"type": "bind", "source": src}, "pg16-20261020-120000", "20261020-120000-db")
check("make_place (bind): an empty sibling directory with the mode of the data's",
      (kept, os.listdir(kept["path"]), oct(os.stat(kept["path"]).st_mode & 0o777)),
      ({"type": "bind", "path": src + ".pg16-20261020-120000"}, [], "0o700"))
U.move_entries(src, kept["path"])
check("move_entries: the data in the place, the directory empty",
      (sorted(os.listdir(kept["path"])), os.listdir(src)), (["PG_VERSION", "base", "global"], []))
open(os.path.join(src, "PG_VERSION"), "w").write("17\n")      # the new major's data, set aside again
new17 = os.path.join(TMP, "new17")
os.mkdir(new17)
U.move_entries(src, new17)
U.move_entries(kept["path"], src)
check("move_entries back: the old data in place again",
      (open(os.path.join(src, "PG_VERSION")).read(), open(os.path.join(src, "base", "1")).read(), os.listdir(kept["path"])),
      ("16\n", "data", []))
check("drop_empty: an empty place goes", (U.drop_empty(kept), os.path.exists(kept["path"])), (True, False))
# A name in both places: a rename would replace the file in the way, so
# nothing moves at all, and nothing is deleted.
blocker = src + ".pg16-20261020-130000"
os.mkdir(blocker)
os.mkdir(os.path.join(blocker, "global"))
open(os.path.join(blocker, "global", "x"), "w").write("in the way")
try:
    U.move_entries(src, blocker)
    broke = None
except OSError as e:
    broke = type(e).__name__
check("move_entries with a name in both places: refused before the first rename, both as they were",
      (broke, sorted(os.listdir(src)), open(os.path.join(src, "base", "1")).read(), sorted(os.listdir(blocker)),
       open(os.path.join(blocker, "global", "x")).read()),
      ("FileExistsError", ["PG_VERSION", "base", "global"], "data", ["global"], "in the way"))
check("drop_empty: a place with data in it stays",
      (U.drop_empty({"type": "bind", "path": blocker}), open(os.path.join(blocker, "global", "x")).read()),
      (False, "in the way"))
shutil.rmtree(blocker)
real_mkdir = U.os.mkdir


def taken(p, *a):
    if p == blocker:
        raise FileExistsError(p)
    return real_mkdir(p, *a)


U.os.mkdir = taken
try:
    U.make_place({"type": "bind", "source": src}, "pg16-20261020-130000", "20261020-130000-db")
    broke = None
except OSError as e:
    broke = type(e).__name__
finally:
    U.os.mkdir = real_mkdir
check("make_place where the name is taken: an error, nothing moved", (broke, sorted(os.listdir(src))),
      ("FileExistsError", ["PG_VERSION", "base", "global"]))

# ------------------------------------------------------------- the plan ----
def container(running=True, mounts=None, env=("PG_VERSION=16.14",), image="postgres:16-alpine"):
    return {"Name": "/db", "Image": "sha256:old", "Config": {"Image": image, "Env": list(env), "Labels": {}},
            "State": {"Running": running, "Status": "running" if running else "exited"},
            "Mounts": [{"Type": "volume", "Name": "app_pgdata", "Driver": "local", "Source": src,
                        "Destination": "/var/lib/postgresql/data"}] if mounts is None else mounts}


def plan_of(target="17.6", services=("db",), env=None, size=10 << 20):
    plan = {"container": "db", "image": "postgres:16-alpine", "services": list(services), "containers": ["db"],
            "installed_version": "16.14", "target_version": target,
            "dependents": [{"service": "app", "containers": ["app"]}, {"service": "cron", "containers": ["cron"]}],
            "dbs": [{"name": "db", "kind": "postgres", "running": True, "size": size}],
            "retag": {"possible": True, "reason": None, "detail": None, "fingerprint": "f" * 64, "changes": []}}
    ctx = {"cfg": {"services": {"db": {"environment": env or {"POSTGRES_PASSWORD": "x"}}}}}
    return plan, ctx


STATE = {}


def stand_in(plan_ctx=None, c=None, kind="postgres", lines=("pgcrypto", "plpgsql"), free=50 << 30, md5=(),
             root=None, root_free=None):
    """make_major's "major" with Docker, compose and psql stood in for: lines
    the extensions, md5 the roles with an MD5 password, root Docker's own
    directory (on another filesystem than the data, with root_free free)."""
    plan, ctx = plan_ctx or plan_of()
    U.make_retag = lambda name, tag, db_major=False: (copy.deepcopy(plan), copy.deepcopy(ctx))
    cs = {"db": c or container(), "app": container(running=True), "cron": container(running=False)}
    U.inspect = lambda n: cs[n]
    U.kind_of = lambda x: kind
    if isinstance(lines, Exception):
        def boom(*a, **k):
            raise lines
        U.pg_lines = boom
    else:
        U.pg_lines = lambda name, script, timeout=None: list(md5 if script == U.PG_MD5_ROLES else lines)
    U.docker_root = lambda: root
    U.same_fs = lambda a, b: root is None
    U.shutil.disk_usage = lambda p: type("du", (), {"free": root_free if p == root else free})()
    return U.make_major("db", "17.6")[0]["major"]


ok = stand_in()
check("plan: possible, 16 to 17, the volume, the running waiting service stopped meanwhile, the extension",
      (ok["possible"], ok["from_major"], ok["to_major"], ok["data"]["name"], ok["pgdata"], ok["stop"], ok["extensions"],
       len(ok["fingerprint"])),
      (True, 16, 17, "app_pgdata", "/var/lib/postgresql/data", ["app"], ["pgcrypto"], 64))
nope = U.make_retag
for label, kw, reason in (
        ("the tag change refused", {"plan_ctx": (lambda p: (dict(p[0], retag=dict(p[0]["retag"], possible=False,
                                                                                  reason="anchors")), p[1]))(plan_of())},
         "anchors"),
        ("not PostgreSQL", {"kind": "mysql"}, "major-not-postgres"),
        ("two services on the image", {"plan_ctx": plan_of(services=("db", "db2"))}, "major-several"),
        ("not running", {"c": container(running=False)}, "major-stopped"),
        ("not a newer major", {"plan_ctx": plan_of(target="16.20")}, "major-not-newer"),
        ("the target unknown", {"plan_ctx": plan_of(target=None)}, "major-not-newer"),
        ("no mount for the data", {"c": container(mounts=[])}, "major-no-volume"),
        ("a volume of another driver", {"c": container(mounts=[{"Type": "volume", "Name": "n", "Driver": "nfs",
                                                                "Source": src, "Destination": "/var/lib/postgresql/data"}])},
         "major-volume-driver"),
        ("an anonymous volume", {"c": container(mounts=[{"Type": "volume", "Name": "a" * 64, "Driver": "local",
                                                         "Source": src, "Destination": "/var/lib/postgresql/data"}])},
         "major-anonymous-volume"),
        ("PostgreSQL 18 without PGDATA: its data would not be in the volume", {"plan_ctx": plan_of(target="18.1")},
         "major-pgdata"),
        ("the extensions unreadable", {"lines": U.subprocess.CalledProcessError(2, ["psql"], "", "no psql")},
         "major-unreadable"),
        ("too little space next to the data", {"free": 100 << 20}, "major-space"),
        ("roles with an MD5 password (the new image lets them in with scram-sha-256 only)",
         {"md5": ("legacy", "oldapp")}, "major-md5"),
        ("too little space for the trial run in Docker's directory on another filesystem",
         {"root": "/var/lib/docker", "root_free": 100 << 20}, "major-probe-space")):
    check(f"plan refused: {label}", stand_in(**kw)["reason"], reason)
check("plan refused: the MD5 roles named", stand_in(md5=("legacy", "oldapp"))["detail"], "legacy, oldapp")
check("plan: MD5 roles, but the compose file keeps MD5 for the network (POSTGRES_HOST_AUTH_METHOD): possible",
      stand_in(plan_of(env={"POSTGRES_PASSWORD": "x", "POSTGRES_HOST_AUTH_METHOD": "md5"}), md5=("legacy",))["possible"], True)
check("plan: Docker's directory on another filesystem with room for the trial run: possible",
      stand_in(root="/var/lib/docker", root_free=50 << 30)["possible"], True)
check("plan: PostgreSQL 18 with PGDATA in the compose file: possible",
      (lambda m: (m["possible"], m["pgdata"]))(stand_in(plan_of(target="18.1", env={"PGDATA": "/var/lib/postgresql/data"}))),
      (True, "/var/lib/postgresql/data"))
check("plan: PostgreSQL 18 with the volume at /var/lib/postgresql: possible",
      (lambda m: (m["possible"], m["pgdata"]))(stand_in(plan_of(target="18.1"), c=container(mounts=[
          {"Type": "volume", "Name": "app_pg", "Driver": "local", "Source": src, "Destination": "/var/lib/postgresql"}]))),
      (True, "/var/lib/postgresql/18/docker"))
real_ismount = U.os.path.ismount
U.os.path.ismount = lambda p: p == src
check("plan refused: a bind mount that is a mount point itself",
      stand_in(c=container(mounts=[{"Type": "bind", "Source": src, "Destination": "/var/lib/postgresql/data"}]))["reason"],
      "major-bind-mountpoint")
U.os.path.ismount = real_ismount
check("plan: the fingerprint follows the stop list and the mount",
      stand_in()["fingerprint"] != stand_in(c=container(mounts=[{"Type": "bind", "Source": src,
                                                                 "Destination": "/var/lib/postgresql/data"}]))["fingerprint"],
      True)
U.make_retag = nope


# ------------------------------------------------------- the trial run ----
class Probe:
    """docker run / rm / ps for probe_restore; ready, errors, counts: what the
    probe's PostgreSQL does."""
    def __init__(self, ready=True, errors=(), counts=None, left=()):
        self.ready, self.errors, self.counts, self.left, self.calls = ready, list(errors), counts, list(left), []

    def run(self, cmd, check=True, capture=True, timeout=600):
        self.calls.append(list(cmd))
        if cmd[:3] == ["docker", "ps", "-aq"]:
            return "".join(i + "\n" for i in self.left)
        return ""


def probe(**kw):
    p = Probe(**kw)
    U.run = p.run
    U.inspect = lambda n: container(env=("PG_VERSION=16.14", "POSTGRES_USER=owner", "POSTGRES_DB=appdb",
                                         "POSTGRES_PASSWORD=the-real-secret"))
    U.wait_postgres = lambda n: p.ready
    U.restore_into = lambda n, rel: list(p.errors)
    U.table_counts = lambda n: p.counts if p.counts is not None else copy.deepcopy(BEFORE)
    plan = {"retag": {"image": "postgres:17.6-alpine"}, "major": {"to_major": 17, "pgdata": "/var/lib/postgresql/data"}}
    op = {"id": "20261022-210000-db", "dumps": ["app/20261022-210000_db.sql.gz"]}
    try:
        with redirect_stdout(io.StringIO()) as out:
            U.probe_restore("db", plan, op, BEFORE)
        return p, None, out.getvalue()
    except U.Abort as e:
        return p, (e.code, str(e)), ""


p, err, out = probe(left=("abc123",))
run_cmd_ = next(c for c in p.calls if c[:2] == ["docker", "run"])
check("probe: the earlier probe of a killed run removed first, then the new one started",
      ([c for c in p.calls if c[:3] == ["docker", "rm", "-f"]][0], run_cmd_[:3]), (["docker", "rm", "-f", "-v", "abc123"], ["docker", "run", "-d"]))
check("probe: no network, labelled with the operation, the user, database and PGDATA of the real one, the new image",
      ("--network" in run_cmd_ and run_cmd_[run_cmd_.index("--network") + 1], "image-updates.probe=20261022-210000-db" in run_cmd_,
       "POSTGRES_USER=owner" in run_cmd_, "POSTGRES_DB=appdb" in run_cmd_, "PGDATA=/var/lib/postgresql/data" in run_cmd_,
       run_cmd_[-1]), ("none", True, True, True, True, "postgres:17.6-alpine"))
check("probe: the database's own password never on the command line (a random one for the probe)",
      (any("the-real-secret" in a for c in p.calls for a in c),
       len([a for a in run_cmd_ if a.startswith("POSTGRES_PASSWORD=")][0]) - len("POSTGRES_PASSWORD=")), (False, 32))
check("probe without a difference: no error, said so, the probe removed",
      (err, "the same" in out, p.calls[-1]), (None, True, ["docker", "rm", "-f", "-v", "db-probe-20261022-210000"]))
for label, kw, want in (("the restore fails", {"errors": ['psql:<stdin>:7: ERROR:  type "x" does not exist']},
                         'restore: psql:<stdin>:7: ERROR:  type "x" does not exist'),
                        ("the counts differ", {"counts": {"appdb": {"public.notes": 299, "public.empty": 0},
                                                          "shop": {"public.items": 5000}}},
                         "rows differ: appdb: public.notes 300 -> 299"),
                        ("PostgreSQL does not start", {"ready": False}, "PostgreSQL did not start")):
    p, err, _ = probe(**kw)
    check(f"probe: {label}: exit 5, nothing changed, the cause named, the probe removed",
          (err[0], want in err[1], "nothing changed" in err[1], p.calls[-1]),
          (5, True, True, ["docker", "rm", "-f", "-v", "db-probe-20261022-210000"]))


# --------------------------------------------------- delete-olddata ----
def run_cmd(fn, *args):
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            return fn(*args), out.getvalue()
    except U.Abort as e:
        return e.code, str(e)


U.lock = lambda: None
OPS = {"20261020-120000-db": {"id": "20261020-120000-db", "kind": "dbmajor", "status": "failed",
                              "old_data": {"type": "bind", "path": src + ".pg16-20261020-120000"}},
       "20261020-110000-web": {"id": "20261020-110000-web", "kind": "update", "status": "ok"}}
U.load_ops = lambda: OPS
check("delete-olddata: a wrong id", run_cmd(U.cmd_delete_olddata, "../x")[0], 64)
check("delete-olddata: no major", run_cmd(U.cmd_delete_olddata, "20261020-110000-web")[0], 2)
check("delete-olddata: a failed major not rolled back keeps the way back", run_cmd(U.cmd_delete_olddata, "20261020-120000-db")[0], 3)
OPS["20261020-120000-db"]["status"] = "ok"
check("delete-olddata: nothing kept there any more", run_cmd(U.cmd_delete_olddata, "20261020-120000-db")[0], 2)
os.mkdir(src + ".pg16-20261020-120000")
open(os.path.join(src + ".pg16-20261020-120000", "PG_VERSION"), "w").write("16\n")
recorded = []
U.record = lambda entry, log=None: recorded.append(entry)
rc, out = run_cmd(U.cmd_delete_olddata, "20261020-120000-db")
check("delete-olddata after a good major: the old data released, recorded",
      (rc, os.path.exists(src + ".pg16-20261020-120000"), [sorted(e) for e in recorded]),
      (0, False, [["data_released", "id"]]))
OPS["20261020-120000-db"].update(rolled_back=True, new_data={"type": "bind", "path": "/etc"})
check("delete-olddata after a rollback: only a place make_place made", run_cmd(U.cmd_delete_olddata, "20261020-120000-db")[0], 2)
os.mkdir(src + ".pg16-20261020-120000")
open(os.path.join(src + ".pg16-20261020-120000", "PG_VERSION"), "w").write("16\n")
OPS["20261020-120000-db"].update(rolled_back=False, status="ok",
                                 old_data={"type": "bind", "path": src + ".pg16-20261020-120000"},
                                 move={"step": "old-back", "from": src + ".pg16-20261020-120000", "to": src})
check("delete-olddata while a rollback's move is open: refused, the data stays",
      (run_cmd(U.cmd_delete_olddata, "20261020-120000-db")[0], os.path.exists(src + ".pg16-20261020-120000")), (3, True))

print("\nALL CHECKS PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

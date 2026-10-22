#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The data of a database major when a run is stopped half way: diun-update.py
moves the old major's data aside before the new major starts, and a rollback
moves it back. Each case runs the major or its rollback in a process of its
own (test/major_child.py: Docker and compose stood in for, the data in a
temporary directory) and stops it with a real signal at a chosen rename: the
child waits there until the signal is sent (a synchronisation point, no
timing). Afterwards every file of the data is compared with its content, and
the history with what is on disk:

  - SIGTERM after the first and after the last rename of the move aside;
  - SIGTERM while a rollback moves the new major's data aside and the old
    data back;
  - SIGKILL in the middle of either move: the next call finds the move in the
    history and finishes it;
  - a rename that fails, and its taking back failing too: no data is deleted;
  - the major and its rollback without a break (a volume and a bind mount);
  - states a rollback must not start a database on: the data of a 0.23.0 run
    in its place but not in the history, data that is gone, a new empty
    cluster next to the kept data."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CHILD = os.path.join(HERE, "major_child.py")
TMP = tempfile.mkdtemp(prefix="major-recovery-")
fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(("OK   " if ok else "FAIL ") + label + ("" if ok else f"\n     got  {got!r}\n     want {want!r}"))


# The old major's data (6 entries at the top) and what the new major writes
# into an empty directory on its first start (4 entries), as in major_child.py.
ORIGINAL = {"PG_VERSION": "16\n", "base": "/", "base/1": "/", "base/1/1259": "the old rows", "base/1/PG_VERSION": "16\n",
            "global": "/", "global/pg_control": "control 16", "pg_hba.conf": "host all all all scram-sha-256\n",
            "pg_wal": "/", "pg_wal/000000010000000000000007": "wal 16", "postgresql.conf": "max_connections = 200\n"}
NEW17 = {"PG_VERSION": "17\n", "base": "/", "base/5": "/", "base/5/16384": "rows of the new major",
         "global": "/", "global/pg_control": "control 17", "pg_wal": "/", "pg_wal/000000010000000000000001": "wal 17"}
# The same for MariaDB 11 and 12.
ORIGINAL_MY = {"mysql_upgrade_info": "11.8.9-MariaDB", "ibdata1": "innodb 11", "ib_logfile0": "redo 11",
               "mysql": "/", "mysql/global_priv.MAD": "the old users", "appdb": "/", "appdb/notes.ibd": "the old rows",
               "appdb/db.opt": "default-character-set=utf8mb4\n"}
NEW12 = {"mysql_upgrade_info": "12.3.3-MariaDB", "ibdata1": "innodb 12", "mysql": "/",
         "mysql/global_priv.MAD": "users 12", "appdb": "/", "appdb/notes.ibd": "rows of the new major"}
TOP, TOP_NEW = 6, 4


def tree(path):
    out = {}
    for root, dirs, files in os.walk(path):
        for d in dirs:
            out[os.path.relpath(os.path.join(root, d), path)] = "/"
        for f in files:
            with open(os.path.join(root, f)) as fh:
                out[os.path.relpath(os.path.join(root, f), path)] = fh.read()
    return out


def write_tree(path, files):
    os.makedirs(path, exist_ok=True)
    for rel, content in files.items():
        p = os.path.join(path, rel)
        if content == "/":
            os.makedirs(p, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(content)


class Scene:
    """One temporary host: the data of the database, the history, the calls."""

    def __init__(self, typ="bind", engine="postgres"):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.typ, self.engine = typ, engine
        self.src = os.path.join(self.dir, "volumes", "app_pgdata", "_data") if typ == "volume" \
            else os.path.join(self.dir, "srv", "pg")
        write_tree(self.src, ORIGINAL_MY if engine == "mariadb" else ORIGINAL)

    def run(self, action, op=None, stop_at=None, sig=None, fail=(), probe_fail=False):
        """The child: (exit code, output, whether it reached stop_at)."""
        cfg = {"action": action, "op": op, "type": self.typ, "src": self.src, "fail": list(fail),
               "probe_fail": probe_fail, "engine": self.engine}
        ready_r, ready_w = os.pipe()
        go_r, go_w = os.pipe()
        if stop_at:
            cfg.update(stop_at=stop_at, ready_fd=ready_w, go_fd=go_r)
        with open(os.path.join(self.dir, "child.json"), "w") as f:
            json.dump(cfg, f)
        p = subprocess.Popen([sys.executable, CHILD, self.dir], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             pass_fds=(ready_w, go_r), text=True, env=dict(os.environ, DIUN_LANG="en"))
        os.close(ready_w)
        os.close(go_r)
        reached = None
        if stop_at:
            # b"" when the child ended before that rename: no waiting on a signal that never comes.
            reached = os.read(ready_r, 1) == b"r"
            if reached:
                os.kill(p.pid, sig)
                if sig != signal.SIGKILL:
                    os.write(go_w, b"g")
        out, _ = p.communicate(timeout=300)
        os.close(ready_r)
        os.close(go_w)
        return p.returncode, out, reached

    def ops(self):
        ops = {}
        path = os.path.join(self.dir, "backups", "operations.jsonl")
        if os.path.exists(path):
            for line in open(path):
                e = json.loads(line)
                ops.setdefault(e["id"], {}).update(e)
        return ops

    def op(self):
        ops = [o for o in self.ops().values() if o.get("kind") == "dbmajor"]
        assert len(ops) == 1, ops
        return ops[0]

    def calls(self, full=False):
        path = os.path.join(self.dir, "calls.jsonl")
        calls = [json.loads(line) for line in open(path)] if os.path.exists(path) else []
        return calls if full else [c["cmd"] for c in calls]

    def ups(self):
        """The data directory as it was at each start of the database."""
        path = os.path.join(self.dir, "calls.jsonl")
        calls = [json.loads(line) for line in open(path)] if os.path.exists(path) else []
        return [c["data"] for c in calls if "data" in c]

    def places(self):
        """{path: files} of every place next to the data: what a major set aside."""
        if self.typ == "volume":
            vols = os.path.join(self.dir, "volumes")
            return {os.path.join(vols, v, "_data"): tree(os.path.join(vols, v, "_data"))
                    for v in sorted(os.listdir(vols)) if v != "app_pgdata"}
        parent, base = os.path.split(self.src)
        return {os.path.join(parent, n): tree(os.path.join(parent, n)) for n in sorted(os.listdir(parent))
                if n.startswith(base + ".")}

    def everything(self):
        """The files of the data directory and of every place together; a file
        in two of them would be counted once, so the sizes are compared too."""
        out, count = dict(tree(self.src)), len(tree(self.src))
        for files in self.places().values():
            out.update(files)
            count += len(files)
        return out, count


def path_of(kept):
    return (kept or {}).get("path")


def held_line(out):
    return "@@SIGNAL-WHILE-HELD True" in out


# ------------------------------------------------ SIGTERM: the move aside ----
for at, label in ((1, "after the first rename"), (TOP, "after the last rename")):
    sc = Scene()
    rc, out, reached = sc.run("migrate", stop_at=at, sig=signal.SIGTERM)
    op = sc.op()
    places = sc.places()
    check(f"major, SIGTERM {label} of the move aside: the signal waits until the move is done and in the "
          f"history, then the run fails (exit 4)", (reached, held_line(out), rc, op.get("status")),
          (True, True, 4, "failed"))
    check(f"major, SIGTERM {label}: the history names where the old data is, no move open",
          (path_of(op.get("old_data")) in places, op.get("move")), (True, None))
    check(f"major, SIGTERM {label}: all old data in that place, the data directory empty, nothing lost",
          (tree(sc.src), [places.get(path_of(op.get("old_data")))]), ({}, [ORIGINAL]))
    check(f"major, SIGTERM {label}: no database started after the move", sc.ups(), [])
    rc, out, _ = sc.run("rollback", op=op["id"])
    op = sc.op()
    check(f"major, SIGTERM {label}, then the rollback: exit 0, the old database starts on all of its data",
          (rc, sc.ups(), op.get("rolled_back")), (0, [ORIGINAL], True))
    check(f"major, SIGTERM {label}, then the rollback: the data as before, no place left, nothing new kept",
          (tree(sc.src), sc.places(), op.get("old_data"), op.get("new_data")), (ORIGINAL, {}, None, None))

# --------------------------------------------- SIGTERM: during the rollback ----
for at, label in ((1, "while it moves the new major's data aside"), (TOP_NEW + 1, "while it moves the old data back")):
    sc = Scene()
    rc, _, _ = sc.run("migrate")
    op = sc.op()
    check(f"rollback, SIGTERM {label}: the major before it went through",
          (rc, op.get("status"), tree(sc.src) == NEW17, sc.places().get(path_of(op.get("old_data")))), (0, "ok", True, ORIGINAL))
    rc, out, reached = sc.run("rollback", op=op["id"], stop_at=at, sig=signal.SIGTERM)
    op = sc.op()
    check(f"rollback, SIGTERM {label}: the signal waits until the data is swapped and in the history, "
          f"then the rollback fails (exit 4) without starting the database",
          (reached, held_line(out), rc, op.get("rollback_status"), op.get("rolled_back"), len(sc.ups())),
          (True, True, 4, "failed", False, 1))
    check(f"rollback, SIGTERM {label}: the old data back in place, the new major's data whole in its place, "
          f"recorded", (tree(sc.src), sc.places().get(path_of(op.get("new_data"))), op.get("old_data"), op.get("move")),
          (ORIGINAL, NEW17, None, None))
    rc, _, _ = sc.run("rollback", op=op["id"])
    op = sc.op()
    check(f"rollback, SIGTERM {label}, then again: exit 0 on all of the old data, the new major's kept",
          (rc, sc.ups()[-1], tree(sc.src), sc.places().get(path_of(op.get("new_data"))), op.get("rolled_back")),
          (0, ORIGINAL, ORIGINAL, NEW17, True))

# -------------------------------------------- SIGKILL: no cleanup at all ----
sc = Scene()
rc, out, reached = sc.run("migrate", stop_at=2, sig=signal.SIGKILL)
op = sc.op()
everything, count = sc.everything()
check("major, SIGKILL in the middle of the move aside: killed, the run still 'running' in the history, "
      "the move recorded before it began", (reached, rc, op.get("status"), (op.get("move") or {}).get("step"),
                                           path_of(op.get("old_data")) in sc.places()),
      (True, -signal.SIGKILL, "running", "aside", True))
check("major, SIGKILL in the middle of the move aside: the data split, nothing lost",
      (len(tree(sc.src)) < len(ORIGINAL), everything, count), (True, ORIGINAL, len(ORIGINAL)))
check("major, SIGKILL: no database started", sc.ups(), [])
rc, out, _ = sc.run("rollback", op=op["id"])
op = sc.op()
check("major, SIGKILL, then the rollback: it finishes the move back, exit 0, the old database on all of its data",
      (rc, sc.ups(), tree(sc.src), sc.places(), op.get("move"), op.get("rolled_back")),
      (0, [ORIGINAL], ORIGINAL, {}, None, True))

sc = Scene()
sc.run("migrate")
op = sc.op()
rc, out, reached = sc.run("rollback", op=op["id"], stop_at=TOP_NEW + 2, sig=signal.SIGKILL)
op = sc.op()
everything, count = sc.everything()
check("rollback, SIGKILL while it moves the old data back: the move recorded, the new major's data kept apart",
      (reached, rc, (op.get("move") or {}).get("step"), sc.places().get(path_of(op.get("new_data")))),
      (True, -signal.SIGKILL, "old-back", NEW17))
check("rollback, SIGKILL while it moves the old data back: nothing lost",
      (count, sorted(everything) == sorted(set(ORIGINAL) | set(NEW17))), (len(ORIGINAL) + len(NEW17), True))
rc, out, _ = sc.run("rollback", op=op["id"])
op = sc.op()
check("rollback, SIGKILL, then again: it finishes the move, exit 0 on all of the old data, the new major's kept",
      (rc, sc.ups()[-1], tree(sc.src), sc.places().get(path_of(op.get("new_data"))), op.get("move"), op.get("rolled_back")),
      (0, ORIGINAL, ORIGINAL, NEW17, None, True))

# --------------------------------------- a rename fails, its undo too ----
sc = Scene()
rc, out, _ = sc.run("migrate", fail=[3])
op = sc.op()
check("major, the third rename fails: everything goes back, the empty place goes, nothing open in the history",
      (rc, tree(sc.src), sc.places(), op.get("old_data"), op.get("move"), sc.ups()), (4, ORIGINAL, {}, None, None, []))
rc, _, _ = sc.run("rollback", op=op["id"])
check("major, the third rename failed, then the rollback: exit 0 on the data as it was",
      (rc, sc.ups(), tree(sc.src)), (0, [ORIGINAL], ORIGINAL))
for typ in ("bind", "volume"):
    sc = Scene(typ)
    rc, out, _ = sc.run("migrate", fail=[3, 4])
    op = sc.op()
    everything, count = sc.everything()
    check(f"major ({typ}), a rename fails and taking it back fails too: exit 4, no file deleted, the place kept "
          f"and in the history, no database started",
          (rc, everything, count, path_of(op.get("old_data")) in sc.places(), (op.get("move") or {}).get("step"), sc.ups()),
          (4, ORIGINAL, len(ORIGINAL), True, "aside", []))
    rc, _, _ = sc.run("rollback", op=op["id"])
    check(f"major ({typ}), after the failed undo the rollback puts the data together: exit 0",
          (rc, sc.ups(), tree(sc.src), sc.places()), (0, [ORIGINAL], ORIGINAL, {}))

# ---------------------------------------------- without a break ----
for typ in ("bind", "volume"):
    sc = Scene(typ)
    rc, out, _ = sc.run("migrate")
    op = sc.op()
    check(f"major ({typ}) without a break: exit 0, the new major on an empty directory, the old data kept whole",
          (rc, op.get("status"), sc.ups(), tree(sc.src), sc.places().get(path_of(op.get("old_data"))), op.get("move")),
          (0, "ok", [{}], NEW17, ORIGINAL, None))
    rc, out, _ = sc.run("rollback", op=op["id"])
    op = sc.op()
    check(f"rollback ({typ}) without a break: exit 0, the old database on all of its data, the new major's kept",
          (rc, sc.ups()[-1], tree(sc.src), list(sc.places().values()), op.get("old_data"), op.get("rolled_back")),
          (0, ORIGINAL, ORIGINAL, [NEW17], None, True))

# ------------------------------------------------------------- MariaDB ----
# The same moves for MariaDB 11 to 12: the place named mariadb11, the version
# in mysql_upgrade_info, the root password for the trial run through the
# environment of docker run only.
for typ in ("volume", "bind"):
    sc = Scene(typ, engine="mariadb")
    rc, out, _ = sc.run("migrate")
    op = sc.op()
    kept = path_of(op.get("old_data"))
    probe_call = [c for c in sc.calls(full=True) if c["cmd"][:2] == ["docker", "run"]]
    check(f"MariaDB major ({typ}): exit 0, MariaDB 12 on an empty directory, the old data kept whole in a place named "
          f"mariadb11, its version file recorded",
          (rc, op.get("engine"), sc.ups(), tree(sc.src), sc.places().get(kept), "mariadb11-" in (kept or "")
           or "mariadb11-" in ((op.get("old_data") or {}).get("name") or ""), op.get("version_file")),
          (0, "mariadb", [{}], NEW12, ORIGINAL_MY, True, "mysql_upgrade_info"))
    check(f"MariaDB major ({typ}): the trial run got the root password through the environment only, the privileges "
          f"and mariadb-upgrade after the restore, a restart before the counts",
          (probe_call[0]["env"], probe_call[0]["secret_in_cmd"], "MARIADB_ROOT_PASSWORD" in probe_call[0]["cmd"],
           [c for c in sc.calls() if c[0] == "after_restore"] == [["after_restore", "db-probe-" + op["id"][:15], "mariadb"],
                                                                  ["after_restore", "db", "mariadb"]],
           any(c[-2:] == ["restart", "db"] for c in sc.calls())),
          (["MARIADB_ROOT_PASSWORD"], False, True, True, True))
    check(f"MariaDB major ({typ}): the dump of the users' databases and users, not the update's dump with the system tables",
          [c for c in sc.calls() if c[0] == "dump_db"], [["dump_db", "major dump"]])
    rc, out, _ = sc.run("rollback", op=op["id"])
    op = sc.op()
    check(f"MariaDB rollback ({typ}): exit 0, MariaDB 11 on all of its data, the data of 12 kept",
          (rc, sc.ups()[-1], tree(sc.src), list(sc.places().values()), op.get("rolled_back")),
          (0, ORIGINAL_MY, ORIGINAL_MY, [NEW12], True))
# A version file that does not name the old major: no MariaDB on that data.
sc = Scene(engine="mariadb")
sc.run("migrate")
op = sc.op()
kept = path_of(op["old_data"])
with open(os.path.join(kept, "mysql_upgrade_info"), "w") as f:
    f.write("10.11.9-MariaDB")
rc, out, _ = sc.run("rollback", op=op["id"])
check("MariaDB rollback with a version file of another major in the kept data: no database started, nothing lost",
      (rc, len(sc.ups()), sc.op().get("rollback_status"), "says 10.11.9-MariaDB instead of MariaDB 11" in out),
      (4, 1, "failed", True))

# ------------------------------------------------ the trial run fails ----
# Before the database changes, the dump goes into a throwaway container of the
# new major: if that fails, the major stops there with nothing changed.
sc = Scene()
rc, out, _ = sc.run("migrate", probe_fail=True)
calls = sc.calls()
probe_run = [c for c in calls if c[:2] == ["docker", "run"]]
check("major, the trial run's restore fails: exit 5, said so, nothing in the history, the data untouched",
      (rc, "Trial run in PostgreSQL 17 failed (restore: psql:<stdin>:9: ERROR:  stand-in error)" in out,
       sc.ops(), tree(sc.src), sc.places()), (5, True, {}, ORIGINAL, {}))
check("major, the trial run fails: the database never stopped nor started, the waiting service started again, "
      "the probe removed", ([c for c in calls if c[-2:] == ["stop", "db"]], sc.ups(), calls[-1][-2:] == ["start", "web"],
                            len(probe_run), ["docker", "rm", "-f", "-v", probe_run[0][probe_run[0].index("--name") + 1]] in calls),
      ([], [], True, 1, True))

# ------------------------------------ states that start no database ----
# A 0.23.0 run stopped while it moved: its data in the place it made, the
# history without it (old_data null). The place has the name the run gave it.
sc = Scene()
rc, _, _ = sc.run("migrate", fail=[1])              # a failed major whose data did not move
op = sc.op()
legacy = sc.src + f".pg16-{op['id'][:15]}"
os.mkdir(legacy)
for e in sorted(os.listdir(sc.src))[:4]:
    os.rename(os.path.join(sc.src, e), os.path.join(legacy, e))
with open(os.path.join(sc.dir, "backups", "operations.jsonl"), "a") as f:
    f.write(json.dumps({"id": op["id"], "data_entries": None, "pg_version_file": None}) + "\n")
rc, out, _ = sc.run("rollback", op=op["id"])
check("rollback of a 0.23.0 run that stopped while it moved: the data found by the place's name and put back, "
      "exit 0 on all of it", (rc, sc.ups(), tree(sc.src), sc.places()), (0, [ORIGINAL], ORIGINAL, {}))

# The data gone (moved by hand somewhere else): no database on an empty directory.
sc = Scene()
rc, _, _ = sc.run("migrate", fail=[1])
op = sc.op()
shutil.move(sc.src, os.path.join(sc.dir, "elsewhere"))
os.mkdir(sc.src)
rc, out, _ = sc.run("rollback", op=op["id"])
op = sc.op()
check("rollback with the data directory empty: no database started, the rollback failed, nothing written",
      (rc, sc.ups(), tree(sc.src), op.get("rollback_status"), op.get("rolled_back")), (4, [], {}, "failed", False))
reason = op.get("rollback_reason") or {}
check("rollback with the data directory empty: the cause names the data and what is missing",
      (reason.get("source"), reason.get("container"), "lacks PG_VERSION, base, global" in (reason.get("line") or "")),
      ("data", "db", True))

# A new empty cluster next to the kept data (a 0.23.0 rollback started the old
# major on the empty directory): which data is right is not clear, both stay.
sc = Scene()
rc, _, _ = sc.run("migrate", fail=[1])
op = sc.op()
legacy = sc.src + f".pg16-{op['id'][:15]}"
shutil.move(sc.src, legacy)
write_tree(sc.src, {"PG_VERSION": "16\n", "base": "/", "global": "/", "global/pg_control": "a fresh, empty cluster"})
fresh = tree(sc.src)
rc, out, _ = sc.run("rollback", op=op["id"])
check("rollback with a new empty cluster where the kept data belongs: no database started, both kept as they are",
      (rc, sc.ups(), tree(sc.src) == fresh, tree(legacy)), (4, [], True, ORIGINAL))

shutil.rmtree(TMP, ignore_errors=True)
print("\nALL CHECKS PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

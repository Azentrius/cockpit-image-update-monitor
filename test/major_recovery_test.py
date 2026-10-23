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
  - SIGKILL once a move is done and in the history, before and after its
    emptied place is removed (the move back, a move aside taken back, the
    new major's move taken back): the next rollback goes on (other stopping
    points: after a place is removed, after a history line);
  - the states a rollback of 0.24.1 to 0.25.0 left when it was killed right
    after it removed an emptied place, and data that is not whole or a
    history that does not fit, which the rollback must not take for a move
    back done;
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

    def run(self, action, op=None, stop_at=None, sig=None, fail=(), probe_fail=False, stop_after_drop=None,
            stop_after_record=None, grants_lost=None):
        """The child: (exit code, output, whether it reached the point it was to stop at)."""
        cfg = {"action": action, "op": op, "type": self.typ, "src": self.src, "fail": list(fail),
               "probe_fail": probe_fail, "engine": self.engine, "grants_lost": grants_lost}
        ready_r, ready_w = os.pipe()
        go_r, go_w = os.pipe()
        stop = {k: v for k, v in (("stop_at", stop_at), ("stop_after_drop", stop_after_drop),
                                  ("stop_after_record", stop_after_record)) if v}
        if stop:
            cfg.update(stop, ready_fd=ready_w, go_fd=go_r)
        with open(os.path.join(self.dir, "child.json"), "w") as f:
            json.dump(cfg, f)
        p = subprocess.Popen([sys.executable, CHILD, self.dir], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             pass_fds=(ready_w, go_r), text=True, env=dict(os.environ, DIUN_LANG="en"))
        os.close(ready_w)
        os.close(go_r)
        reached = None
        if stop:
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

# ------------------------------------------ a kill while a place goes ----
# The end of a move back: the data whole where it belongs, then the place it
# came from removed while empty. Since 0.25.1 the history says first that the
# place is emptied (old_data or new_data null, "emptied": the place), then the
# place goes, then the history forgets it: a kill between any two of these
# leaves a state the next rollback goes on from. Up to 0.25.0 the place went
# first, and a kill right after that left the history naming a place that was
# gone: every later rollback stopped (exit 2, for the new major's place exit 5).
TOP_MY, TOP_NEW_MY = 5, 4


def remove_place(kept):
    """What a run of 0.24.1 to 0.25.0 did before it recorded the end of a
    move: the emptied place removed (a volume with its directory)."""
    assert not os.listdir(kept["path"]), kept
    shutil.rmtree(os.path.dirname(kept["path"]) if kept["type"] == "volume" else kept["path"])


for typ in ("bind", "volume"):
    sc = Scene(typ)
    sc.run("migrate")
    op = sc.op()
    old = op["old_data"]
    rc, out, reached = sc.run("rollback", op=op["id"], stop_after_drop=1, sig=signal.SIGKILL)
    op = sc.op()
    check(f"rollback ({typ}), SIGKILL right after the emptied place of the old data is removed: killed there, the "
          f"old data whole in its place, the place gone, the new major's data whole in its own, no database started",
          (reached, rc, tree(sc.src), list(sc.places().values()), os.path.exists(old["path"]), len(sc.ups())),
          (True, -signal.SIGKILL, ORIGINAL, [NEW17], False, 1))
    rc, out, _ = sc.run("rollback", op=op["id"])
    op = sc.op()
    check(f"rollback ({typ}), SIGKILL after the place went, then again: exit 0, the old database started once and on "
          f"all of its data only, nothing open in the history",
          (rc, sc.ups()[1:], tree(sc.src), op.get("rolled_back"), op.get("old_data"), op.get("move"), op.get("emptied")),
          (0, [ORIGINAL], ORIGINAL, True, None, None, None))
    check(f"rollback ({typ}), SIGKILL after the place went, then again: the new major's data kept whole, nothing else "
          f"next to the data", (list(sc.places().values()), path_of(op.get("new_data")) in sc.places()), ([NEW17], True))

    sc = Scene(typ)
    sc.run("migrate")
    op = sc.op()
    old = op["old_data"]
    rc, out, reached = sc.run("rollback", op=op["id"], stop_after_record="emptied", sig=signal.SIGKILL)
    op = sc.op()
    check(f"rollback ({typ}), SIGKILL once the history says the move back is done, before the empty place goes: "
          f"the history names the emptied place and nothing open, the data whole in its place",
          (reached, rc, path_of(op.get("emptied")), op.get("old_data"), op.get("move"), tree(sc.src),
           tree(old["path"]) if os.path.isdir(old["path"]) else None),
          (True, -signal.SIGKILL, old["path"], None, None, ORIGINAL, {}))
    rc, _, _ = sc.run("rollback", op=op["id"])
    op = sc.op()
    check(f"rollback ({typ}), SIGKILL before the empty place went, then again: exit 0, the place removed, the old "
          f"database once on all of its data, nothing open in the history",
          (rc, sc.ups()[1:], os.path.exists(old["path"]), list(sc.places().values()), op.get("emptied"), op.get("rolled_back")),
          (0, [ORIGINAL], False, [NEW17], None, True))

    # The same order where a move is taken back: the major's move aside (a rename fails) and the rollback's
    # move of the new major's data (a rename fails).
    sc = Scene(typ)
    rc, out, reached = sc.run("migrate", fail=[3], stop_after_drop=1, sig=signal.SIGKILL)
    op = sc.op()
    check(f"major ({typ}), the third rename fails, all goes back, SIGKILL right after the emptied place is removed: "
          f"the data whole in its place, nothing next to it, no database started",
          (reached, rc, tree(sc.src), sc.places(), sc.ups()), (True, -signal.SIGKILL, ORIGINAL, {}, []))
    rc, _, _ = sc.run("rollback", op=op["id"])
    op = sc.op()
    check(f"major ({typ}), the move aside taken back and the place gone at the kill, then the rollback: exit 0, the "
          f"old database on all of its data, nothing open in the history",
          (rc, sc.ups(), tree(sc.src), op.get("old_data"), op.get("move"), op.get("emptied")),
          (0, [ORIGINAL], ORIGINAL, None, None, None))

    sc = Scene(typ)
    sc.run("migrate")
    op = sc.op()
    rc, out, reached = sc.run("rollback", op=op["id"], fail=[2], stop_after_drop=1, sig=signal.SIGKILL)
    op = sc.op()
    check(f"rollback ({typ}), a rename of the new major's data fails, it goes back, SIGKILL right after its emptied "
          f"place is removed: the new major's data whole in the data directory, the old data whole in its place",
          (reached, rc, tree(sc.src), list(sc.places().values()), len(sc.ups())), (True, -signal.SIGKILL, NEW17, [ORIGINAL], 1))
    rc, _, _ = sc.run("rollback", op=op["id"])
    op = sc.op()
    check(f"rollback ({typ}), the new major's place gone at the kill, then again: exit 0, the old database on all of "
          f"its data, the new major's data kept whole",
          (rc, sc.ups()[1:], tree(sc.src), list(sc.places().values()), op.get("rolled_back")), (0, [ORIGINAL], ORIGINAL, [NEW17], True))

# A place recorded as emptied that has data in it again stays with it: nothing is ever deleted.
sc = Scene()
sc.run("migrate")
op = sc.op()
old = op["old_data"]
sc.run("rollback", op=op["id"], stop_after_record="emptied", sig=signal.SIGKILL)
if os.path.isdir(old["path"]):
    with open(os.path.join(old["path"], "put-there-later"), "w") as f:
        f.write("keep me")
rc, _, _ = sc.run("rollback", op=op["id"])
check("rollback after a kill, the emptied place has a file in it again: the place stays with the file, the rollback "
      "goes on (exit 0, the old database on all of its data)",
      (rc, tree(old["path"]) if os.path.isdir(old["path"]) else None, path_of(sc.op().get("emptied")), sc.ups()[1:]),
      (0, {"put-there-later": "keep me"}, old["path"], [ORIGINAL]))

# The MariaDB data the same way (its version file in the data).
sc = Scene(engine="mariadb")
sc.run("migrate")
op = sc.op()
rc, _, reached = sc.run("rollback", op=op["id"], stop_after_drop=1, sig=signal.SIGKILL)
rc, out, _ = sc.run("rollback", op=op["id"])
check("MariaDB rollback, SIGKILL right after the emptied place of the old data is removed, then again: exit 0, "
      "MariaDB 11 once on all of its data, the data of 12 kept",
      (reached, rc, sc.ups()[1:], tree(sc.src), list(sc.places().values())), (True, 0, [ORIGINAL_MY], ORIGINAL_MY, [NEW12]))

# ---------------------------------- what a rollback of 0.24.1 to 0.25.0 left ----
# Those versions removed an emptied place before they recorded the end of its
# move. The states a kill right after that left, made the same way here (the
# child stopped by SIGKILL after the last rename, the empty place removed as
# they did): the rollback recognises the data as whole in its place and goes
# on, and only then.
for typ in ("bind", "volume"):
    sc = Scene(typ)
    sc.run("migrate")
    op = sc.op()
    old = op["old_data"]
    _, _, reached = sc.run("rollback", op=op["id"], stop_at=TOP_NEW + TOP, sig=signal.SIGKILL)
    remove_place(old)
    op = sc.op()
    check(f"0.25.0 state ({typ}): the old data back whole, its place removed, the history still naming that place "
          f"and the move back", (reached, tree(sc.src), (op.get("move") or {}).get("step"), path_of(op.get("old_data")),
                                os.path.exists(old["path"])), (True, ORIGINAL, "old-back", old["path"], False))
    rc, out, _ = sc.run("rollback", op=op["id"])
    op = sc.op()
    check(f"0.25.0 state ({typ}), the rollback: the old data recognised as back whole (said so), exit 0, the old "
          f"database once on all of it, the new major's data kept, nothing open in the history",
          (rc, "already back in its place, whole" in out, sc.ups()[1:], tree(sc.src), list(sc.places().values()),
           op.get("old_data"), op.get("move"), op.get("rolled_back")),
          (0, True, [ORIGINAL], ORIGINAL, [NEW17], None, None, True))

    sc = Scene(typ)
    _, _, reached = sc.run("migrate", fail=[3], stop_at=5, sig=signal.SIGKILL)
    op = sc.op()
    old = op["old_data"]
    remove_place(old)
    check(f"0.25.0 state ({typ}): the move aside taken back whole, its place removed, the history still naming that "
          f"place and the move aside", (reached, tree(sc.src), (op.get("move") or {}).get("step"), os.path.exists(old["path"])),
          (True, ORIGINAL, "aside", False))
    rc, out, _ = sc.run("rollback", op=op["id"])
    op = sc.op()
    check(f"0.25.0 state ({typ}) after a move aside taken back, the rollback: recognised, exit 0, the old database "
          f"once on all of its data", (rc, "already back in its place, whole" in out, sc.ups(), tree(sc.src), op.get("move")),
          (0, True, [ORIGINAL], ORIGINAL, None))

    sc = Scene(typ)
    sc.run("migrate")
    op = sc.op()
    _, _, reached = sc.run("rollback", op=op["id"], fail=[2], stop_at=3, sig=signal.SIGKILL)
    op = sc.op()
    new = op["new_data"]
    remove_place(new)
    check(f"0.25.0 state ({typ}): the rollback's move of the new major's data taken back whole, its place removed, "
          f"the history still naming it", (reached, tree(sc.src), (op.get("move") or {}).get("step"),
                                           os.path.exists(new["path"]), list(sc.places().values())),
          (True, NEW17, "new-aside", False, [ORIGINAL]))
    rc, out, _ = sc.run("rollback", op=op["id"])
    op = sc.op()
    check(f"0.25.0 state ({typ}) after the new major's move taken back, the rollback: exit 0, the old database once "
          f"on all of its data, the new major's data kept whole",
          (rc, sc.ups()[1:], tree(sc.src), list(sc.places().values()), op.get("move"), op.get("rolled_back")),
          (0, [ORIGINAL], ORIGINAL, [NEW17], None, True))


def legacy_old_back(typ="bind", engine="postgres"):
    """The state a kill after the removed place left in 0.24.1 to 0.25.0."""
    sc = Scene(typ, engine)
    sc.run("migrate")
    op = sc.op()
    last = TOP_NEW_MY + TOP_MY if engine == "mariadb" else TOP_NEW + TOP
    sc.run("rollback", op=op["id"], stop_at=last, sig=signal.SIGKILL)
    remove_place(op["old_data"])
    return sc, sc.op()


legacy_ok = legacy_old_back("bind", "mariadb")
rc, out, _ = legacy_ok[0].run("rollback", op=legacy_ok[1]["id"])
check("0.25.0 state (MariaDB): recognised by the entries and the version file, exit 0, MariaDB 11 once on all of it",
      (rc, legacy_ok[0].ups()[1:], tree(legacy_ok[0].src)), (0, [ORIGINAL_MY], ORIGINAL_MY))

# Data that is not whole, or a history that does not say the data was going
# back, is no move back done: the rollback stops as before (exit 2), nothing
# moves, no database starts.
NEGATIVE = []
sc, op = legacy_old_back()
shutil.move(os.path.join(sc.src, "base"), os.path.join(sc.dir, "base-elsewhere"))
NEGATIVE.append(("an entry of the data moved away", sc, op))
sc, op = legacy_old_back("volume")
os.remove(os.path.join(sc.src, "postgresql.conf"))
NEGATIVE.append(("a file of the data deleted (a volume)", sc, op))
sc, op = legacy_old_back()
with open(os.path.join(sc.src, "PG_VERSION"), "w") as f:
    f.write("17\n")
NEGATIVE.append(("PG_VERSION naming the new major", sc, op))
sc, op = legacy_old_back()
with open(os.path.join(sc.src, "postmaster.pid"), "w") as f:
    f.write("1\n")
NEGATIVE.append(("an entry more than the major set aside", sc, op))
sc, op = legacy_old_back()
with open(os.path.join(sc.dir, "backups", "operations.jsonl"), "a") as f:
    f.write(json.dumps({"id": op["id"], "data_entries": None}) + "\n")
NEGATIVE.append(("the entries set aside not in the history", sc, op))
sc, op = legacy_old_back("bind", "mariadb")
with open(os.path.join(sc.src, "mysql_upgrade_info"), "w") as f:
    f.write("10.11.9-MariaDB")
NEGATIVE.append(("MariaDB: the version file naming another major", sc, op))
sc = Scene()
sc.run("migrate")
op = sc.op()
shutil.move(op["old_data"]["path"], os.path.join(sc.dir, "kept-elsewhere"))
NEGATIVE.append(("no move open: the major went through, its kept data moved away by hand", sc, op))
sc = Scene()
sc.run("migrate")
op = sc.op()
sc.run("rollback", op=op["id"], stop_at=2, sig=signal.SIGKILL)       # in the middle of the new major's move aside
op = sc.op()
shutil.move(op["old_data"]["path"], os.path.join(sc.dir, "kept-elsewhere"))
NEGATIVE.append(("a move of the new major's data open, the old data's place gone", sc, op))
for label, sc, op in NEGATIVE:
    before, ups = tree(sc.src), len(sc.ups())
    rc, out, _ = sc.run("rollback", op=op["id"])
    check(f"no move back done: {label}: exit 2 (the old data is gone), nothing moved, no database started",
          (rc, "is gone" in out, tree(sc.src) == before, len(sc.ups()), sc.op().get("rolled_back")), (2, True, True, ups, None))

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
# Since 0.25.1 the users and roles with their grants are compared as well:
# read before the dump, in the trial run and after the restore.
sc = Scene(engine="mariadb")
rc, out, _ = sc.run("migrate")
check("MariaDB major: the grants read on the old server, in the trial run and after the restore",
      [c[1:] for c in sc.calls() if c[0] == "account_grants"], [["db"], ["db-probe-" + sc.op()["id"][:15]], ["db"]])
sc = Scene(engine="mariadb")
rc, out, _ = sc.run("migrate", grants_lost=2)
check("MariaDB major, a role missing in the trial run: exit 5, said so, nothing changed (no history, the data where "
      "it was, no database started)", (rc, "privileges differ: 'editor': missing" in out, sc.ops(), tree(sc.src),
                                       sc.places(), sc.ups()), (5, True, {}, ORIGINAL_MY, {}, []))
sc = Scene(engine="mariadb")
rc, out, _ = sc.run("migrate", grants_lost=3)
op = sc.op()
reason = op.get("reason") or {}
check("MariaDB major, a role missing after the restore: exit 4, the run failed with the cause (source grants), the "
      "old data kept", (rc, op.get("status"), reason.get("source"), "privileges differ: 'editor': missing" in
                        (reason.get("line") or ""), sc.places().get(path_of(op.get("old_data")))),
      (4, "failed", "grants", True, ORIGINAL_MY))
rc, _, _ = sc.run("rollback", op=op["id"])
check("MariaDB major, a role missing after the restore, then the rollback: exit 0, MariaDB 11 on all of its data",
      (rc, sc.ups()[-1], tree(sc.src)), (0, ORIGINAL_MY, ORIGINAL_MY))

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

#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Offline tests for lib/diunstatus.py, the status for the Status Board, and
the verb `diun-update.py status`: the page's rules for the rows (Diun's event
of the tag, the same build, dismissed events, roll-out pending, new versions,
CI jobs), the failed updates from the history, the radar's age, the overall
state and the summary in English and German, the contract's shape, the file
(0644, atomic, names only), `status` without the file, from it, as root and
without root, its arguments, and the writers: the radar run and the hook
after update, rollback and cleanup. Also the manifest's menu entry.

The fixture test/fixtures/status.json is this module's output for the data
of the other fixtures; test/status-rows.js checks that the page shows the
same rows. `python3 status_test.py --write-fixture` writes it anew.
No Docker, no root, no network."""
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(os.path.dirname(HERE), "lib")
FIX = os.path.join(HERE, "fixtures")
sys.path.insert(0, ROOT)
import diunstatus as S  # noqa: E402

TMP = tempfile.mkdtemp(prefix="diun-status-test-")
S.STATUS = os.path.join(TMP, "radar", "status.json")
S.VERSIONS = os.path.join(TMP, "radar", "versions.json")
S.EVENTS = os.path.join(TMP, "diun", "events.jsonl")
S.DISMISSED = os.path.join(TMP, "diun", "dismissed.json")
S.OPS = os.path.join(TMP, "backups", "operations.jsonl")
S.PLUGIN_JS = os.path.join(os.path.dirname(ROOT), "plugin", "diun.js")
S.helper_alive = lambda pid: False
NOW = datetime(2026, 10, 12, 2, 50, tzinfo=timezone.utc)      # the time of test/harness.js
VERSION = re.search(r'const VERSION = "([^"]+)"', open(S.PLUGIN_JS, encoding="utf-8").read()).group(1)

fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(("OK   " if ok else "FAIL ") + label + ("" if ok else f"\n     got  {got!r}\n     want {want!r}"))


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, file))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def fixture_doc():
    """status.json for the fixtures (inventory3.jsonl, events.jsonl, versions2.json), no history."""
    with open(os.path.join(FIX, "inventory3.jsonl"), encoding="utf-8") as f:
        inv = [json.loads(line) for line in f if line.strip()]
    with open(os.path.join(FIX, "events.jsonl"), encoding="utf-8") as f:
        events = f.read()
    with open(os.path.join(FIX, "versions2.json"), encoding="utf-8") as f:
        versions = json.load(f)
    facts = S.facts_of(inv, events, None, versions, {}, NOW)
    doc = S.document(facts, "en", NOW, VERSION)
    doc["facts"] = facts
    return doc


if "--write-fixture" in sys.argv:
    with open(os.path.join(FIX, "status.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(fixture_doc(), f, ensure_ascii=False, indent=1)
        f.write("\n")
    print("test/fixtures/status.json written")
    sys.exit(0)


def ctn(name, image, tag, digest="sha256:run", **kw):
    c = {"name": name, "image": image, "tag": tag, "digest": digest, "imgid": "sha256:id-" + name,
         "version": None, "version_src": None, "tagid": "", "pulled": False, "project": "", "service": "",
         "managed": True, "files": [], "ci_job": False, "created": "", "revision": ""}
    c.update(kw)
    return c


def event(image, digest, ts="2026-10-11T10:00:00Z", created=""):
    return json.dumps({"ts": ts, "status": "update", "hostname": "web1", "provider": "docker",
                       "image": image, "digest": digest, "created": created, "platform": "linux/amd64"})


def rows(containers, events=(), dismissed=None, versions=None):
    return S.page_rows(containers, S.events_by_tag("\n".join(events))[0], dismissed or {}, versions)


def radar(*recs, generated="2026-10-12T02:17:34Z"):
    return {"generated": generated, "images": list(recs)}


def facts(updates=(), failed=(), generated="2026-10-12T02:17:34Z", rollout=(), news=(), events=True, found=True):
    at = S.iso(NOW)
    return {"checked_at": at,
            "updates": {"checked_at": at, "names": list(updates) if events else None},
            "failed": {"checked_at": at, "ops": [{"name": n, "how": h} for n, h in failed]},
            "radar": {"found": found, "generated": generated},
            "rollout": {"checked_at": at, "names": list(rollout)},
            "new-versions": {"checked_at": at, "names": list(news) if found else None}}


def by_id(doc):
    return {it["id"]: it for it in doc["items"]}


# ------------------------------------------------------- the page's rows
app = ctn("app", "acme/app", "latest", digest="sha256:a")
check("Diun reports another digest of the tag: update",
      rows([app], [event("docker.io/acme/app:latest", "sha256:b")])["update"], ["app"])
check("docker.io/library/ and case do not matter",
      rows([ctn("db", "Postgres", "16")], [event("docker.io/library/postgres:16", "sha256:new")])["update"], ["db"])
check("the event of another tag says nothing about this one",
      rows([ctn("db", "postgres", "16")], [event("postgres:18", "sha256:new")])["update"], [])
check("an event for a reference with a digest belongs to no row",
      rows([ctn("db", "postgres", "16")], [event("postgres:16@sha256:x", "sha256:new")])["update"], [])
check("the digest that runs: no update", rows([app], [event("acme/app:latest", "sha256:a")])["update"], [])
ev_b = json.loads(event("acme/app:latest", "sha256:b"))
check("a dismissed event: no update",
      rows([app], [json.dumps(ev_b)], {S.ev_key(ev_b): True})["update"], [])
check("the same build under a new digest (Docker's and Diun's time, another zone): no update",
      [rows([dict(app, created="2026-10-15T19:00:50.089Z")],
            [event("acme/app:latest", "sha256:b", created=c)])["update"]
       for c in ("2026-10-15 19:00:50.089 +0000 UTC", "2026-10-15 21:00:50 +0200", "2026-10-15T19:00:51Z")],
      [[], [], ["app"]])
check("a build date before 1980 proves nothing",
      rows([dict(app, created="1970-01-01T00:00:00Z")],
           [event("acme/app:latest", "sha256:b", created="1970-01-01 00:00:00 +0000 UTC")])["update"], ["app"])
check("the newest event of the tag counts",
      rows([app], [event("acme/app:latest", "sha256:a", ts="2026-10-12T00:00:00Z"),
                   event("acme/app:latest", "sha256:b", ts="2026-10-11T00:00:00Z")])["update"], [])
check("pulled, not rolled out, comes first",
      rows([dict(app, pulled=True)], [event("acme/app:latest", "sha256:b")]), {"update": [], "pulled": ["app"], "newver": []})
check("a CI job container is no row",
      rows([dict(app, ci_job=True, pulled=True)], [event("acme/app:latest", "sha256:b")]),
      {"update": [], "pulled": [], "newver": []})
check("no digest (built here): no update", rows([dict(app, digest="")], [event("acme/app:latest", "sha256:b")])["update"], [])

rec = {"image": "acme/app", "tag": "1.0.0", "db": False, "reach": [],
       "versions": {"1.0.0": {"patch": "1.0.1", "minor": None, "major": None, "status": "patch"}}}
pinned = ctn("pinned", "acme/app", "1.0.0", version="1.0.0", version_src="tag")
check("the radar knows a newer patch: new version", rows([pinned], versions=radar(rec))["newver"], ["pinned"])
check("without the radar no new version", rows([pinned])["newver"], [])
check("an update outranks a new version",
      rows([pinned], [event("acme/app:1.0.0", "sha256:b")], versions=radar(rec)),
      {"update": ["pinned"], "pulled": [], "newver": []})
pg = ctn("pg", "postgres", "16-alpine", version="16.14", version_src="env:PG_VERSION")
pg_rec = {"image": "postgres", "tag": "16-alpine", "db": True, "versions": {"16.14": {"major": "18.6-alpine", "status": "major"}}}
check("a database's major alone is no new version, another image's is",
      [rows([pg], versions=radar(pg_rec))["newver"], rows([pg], versions=radar(dict(pg_rec, db=False)))["newver"]],
      [[], ["pg"]])
unknown = ctn("pgv", "pgvector/pgvector", "pg17")
pgv_rec = {"image": "pgvector/pgvector", "tag": "pg17", "db": False,
           "versions": {"17.10": {"status": "unknown"}}, "by_tag": {"major": "pg18", "status": "major"}}
check("installed version unknown or without a scheme: the configured tag compared (by_tag)",
      [rows([unknown], versions=radar(pgv_rec))["newver"],
       rows([dict(unknown, version="17.10", version_src="env:PG_VERSION")], versions=radar(pgv_rec))["newver"]],
      [["pgv"], ["pgv"]])
check("an empty comparison is no reason to fall back to the tag",
      rows([pinned], versions=radar(dict(rec, versions={"1.0.0": {}}, by_tag={"major": "2.0.0"})))["newver"], [])
cube = ctn("cube", "cubejs/cube", "v1.6", version="1.6", version_src="tag")
cube_rec = {"image": "cubejs/cube", "tag": "v1.6", "db": False,
            "installed": {"sha256:id-cube": {"version": "1.6.74", "src": "digest"}},
            "versions": {"1.6.74": {"status": "current"}, "1.6": {"patch": "v1.6.80", "status": "patch"}}}
check("a line tag refined by the radar: its release is compared, not the tag's number",
      rows([cube], versions=radar(cube_rec))["newver"], [])
built = ctn("web", "intranet-app", "2.12.4", digest="")
base = {"image": "intranet-app", "tag": "2.12.4", "status": "unlisted", "db": False}
check("built here: a newer base is a new version, a pinned one or an error is not",
      [rows([built], versions=radar(dict(base, base=b)))["newver"]
       for b in ([{"image": "caddy", "tag": "2", "newer": True}],
                 [{"image": "caddy", "tag": "2", "newer": True, "pinned": True}],
                 [{"image": "caddy", "tag": "2", "newer": True, "error": "x"}], [])],
      [["web"], [], [], []])

# ------------------------------------------------------- failed updates
OPS = [{"id": "20261010-010000-app", "kind": "update", "container": "app", "project": "p", "image": "acme/app:1",
        "status": "failed"},
       {"id": "20261010-020000-web", "kind": "retag", "container": "web", "project": "q", "image": "acme/web:1",
        "status": "failed", "rolled_back": True, "rollback_status": "ok"},
       {"id": "20261010-030000-old", "kind": "update", "container": "old", "project": "r", "image": "acme/old:1",
        "status": "ok", "rolled_back": True, "rollback_status": "failed"},
       {"id": "20261010-040000-cut", "kind": "update", "container": "cut", "project": "s", "image": "acme/cut:1",
        "status": "running", "pid": 4242},
       {"id": "20261010-050000-gone", "kind": "update", "container": "gone", "project": "t", "image": "acme/gone:1",
        "status": "failed"},
       {"id": "20261010-050001-gone", "status": "discarded"}]
ops = {o["id"]: o for o in OPS}
check("failed and not rolled back, its rollback failed (also as written up to 0.18.0), aborted",
      S.failed_ops(ops), [{"name": "app", "how": "failed"}, {"name": "cut", "how": "aborted"},
                          {"name": "gone", "how": "failed"}, {"name": "old", "how": "rollback"}])
check("a run that still runs is no failure",
      [o["name"] for o in S.failed_ops(ops, alive=lambda pid: pid == 4242)], ["app", "gone", "old"])
later = dict(ops, **{"20261011-010000-app": {"id": "20261011-010000-app", "kind": "update", "container": "app",
                                             "containers": ["app"], "project": "p", "image": "docker.io/acme/app:1",
                                             "status": "ok"},
                     "20261011-010000-cut": {"id": "20261011-010000-cut", "kind": "update", "container": "cut",
                                             "project": "s", "image": "acme/cut:1", "status": "running", "pid": 1}})
check("a later run of the same containers decides: a success, a run in progress",
      [o["name"] for o in S.failed_ops(later, alive=lambda pid: pid == 1)], ["gone", "old"])
folded = os.path.join(TMP, "ops.jsonl")
with open(folded, "w", encoding="utf-8") as f:
    f.write(json.dumps(OPS[0]) + "\n" + "not json\n" +
            json.dumps({"id": OPS[0]["id"], "rolled_back": True, "rollback_status": "ok"}) + "\n" +
            json.dumps(OPS[4]) + "\n" + json.dumps({"id": OPS[4]["id"], "status": "discarded"}) + "\n")
check("the history folded by id, lines taken back left out", S.failed_ops(S.load_ops(folded)), [])
check("no history: nothing failed", S.failed_ops(S.load_ops(os.path.join(TMP, "none.jsonl"))), [])


# A later run settles only the services it ran for, in the same compose files
# (make_plan: the containers of the project on the same image and started from
# the same files). Before, the newest run of each repository in a project
# decided, and a success of another service hid a failure that still stood.
def run_of(id_, status="ok", kind="update", project="stack", workdir="/opt/stack", files=("/opt/stack/compose.yml",),
           services=("db16",), containers=None, image="postgres:16", **kw):
    o = {"id": id_, "kind": kind, "status": status, "project": project, "workdir": workdir, "files": list(files),
         "services": list(services), "image": image,
         "containers": list(containers) if containers is not None else [f"{project}-{s}-1" for s in services]}
    o["container"] = kw.pop("container", o["containers"][0])
    o.update(kw)
    return o


def open_failures(*runs, alive=lambda pid: False):
    return S.failed_ops({o["id"]: o for o in runs}, alive=alive)


db16_failed = run_of("20261012-010000-db16", "failed")
db17_ok = run_of("20261012-020000-db17", services=["db17"], image="postgres:17")
check("same project, another tag and service: the success of db17 leaves the failure of db16",
      open_failures(db16_failed, db17_ok), [{"name": "stack-db16-1", "how": "failed"}])
check("the order does not matter: a failure of db16 after a success of db17",
      open_failures(run_of("20261012-010000-db17", services=["db17"], image="postgres:17"),
                    run_of("20261012-020000-db16", "failed")), [{"name": "stack-db16-1", "how": "failed"}])
runner_failed = run_of("20261012-010000-runner", "failed", project="ci", workdir="/opt/ci",
                       files=["/opt/ci/docker-compose.yml"], services=["runner", "runner-2"], image="acme/runner:1")
emu_ok = run_of("20261012-020000-emu", project="ci", workdir="/opt/ci", files=["/opt/ci/docker-compose.emu.yml"],
                services=["runner-emu"], image="acme/runner:1")
check("the same image in separate compose files: the run of the other file hides nothing",
      open_failures(runner_failed, emu_ok), [{"name": "ci-runner-1", "how": "failed"}])
web_a = run_of("20261012-010000-web-a", "failed", project="app", workdir="/opt/a", files=["/opt/a/compose.yml"],
               services=["web"], containers=["web-a"], image="acme/web:1")
web_b = run_of("20261012-020000-web-b", project="app", workdir="/opt/b", files=["/opt/b/compose.yml"],
               services=["web"], containers=["web-b"], image="acme/web:1")
check("the same project name, service and image in another directory: independent",
      open_failures(web_a, web_b), [{"name": "web-a", "how": "failed"}])
check("a successful retry of the same services settles the failure",
      open_failures(db16_failed, run_of("20261012-020000-db16")), [])
check("a successful tag change of the same services settles the failure of the old tag",
      open_failures(db16_failed, run_of("20261012-020000-db16", kind="retag", to_image="postgres:17",
                                        from_tag="16", to_tag="17")), [])
check("a failed retry is the failure that counts, once",
      open_failures(db16_failed, run_of("20261012-020000-db16", "failed", container="stack-db16-1")),
      [{"name": "stack-db16-1", "how": "failed"}])
both_failed = run_of("20261012-010000-both", "failed", project="shop", workdir="/opt/shop",
                     files=["/opt/shop/compose.yml"], services=["db", "web"],
                     containers=["shop-db-1", "shop-web-1"], container="shop-web-1", image="acme/shop:1")
web_ok = run_of("20261012-020000-web", project="shop", workdir="/opt/shop", files=["/opt/shop/compose.yml"],
                services=["web"], containers=["shop-web-1"], image="acme/shop:2")
check("overlapping groups: web settled later, db still affected, named by a container still affected",
      open_failures(both_failed, web_ok), [{"name": "shop-db-1", "how": "failed", "services": ["db"]}])
check("overlapping groups: both settled by a later run of db and web",
      open_failures(both_failed, web_ok, run_of("20261012-030000-db", project="shop", workdir="/opt/shop",
                                                files=["/opt/shop/compose.yml"], services=["db"],
                                                containers=["shop-db-1"], image="acme/shop:1")), [])
same = dict(project="ops", workdir="/opt/ops", files=["/opt/ops/compose.yml"], image="acme/ops:1")
rolled = run_of("20261012-010000-a", "failed", services=["a"], rolled_back=True, rollback_status="ok", **same)
rb_failed = run_of("20261012-010001-b", services=["b"], rolled_back=True, rollback_status="failed", **same)
cut = run_of("20261012-010002-c", "running", services=["c"], pid=4242, **same)
other_ok = run_of("20261012-020000-d", services=["d"], **same)
check("a rollback that worked, a failed rollback, an aborted run: an unrelated later success hides none of them",
      open_failures(rolled, rb_failed, cut, other_ok),
      [{"name": "ops-b-1", "how": "rollback"}, {"name": "ops-c-1", "how": "aborted"}])
check("the run of c still runs: no failure",
      [o["name"] for o in open_failures(rolled, rb_failed, cut, other_ok, alive=lambda pid: pid == 4242)], ["ops-b-1"])
check("a later success of b's services settles its failed rollback",
      open_failures(rb_failed, run_of("20261012-020000-b", services=["b"], **same)), [])
check("a later failed run of a supersedes the rollback that worked: its failure counts",
      open_failures(rolled, run_of("20261012-020000-a", "failed", services=["a"], **same)),
      [{"name": "ops-a-1", "how": "failed"}])
# Entries written without services, containers or files (older versions):
# their scope is the container names they name, in any compose files of the
# project; a later run settles them only for those containers.
legacy = {"id": "20261001-010000-x", "kind": "update", "container": "x", "project": "p", "image": "acme/x:1",
          "status": "failed"}
check("an old entry without services: another container of the same image hides nothing",
      open_failures(legacy, {"id": "20261002-010000-y", "kind": "update", "container": "y", "project": "p",
                             "image": "acme/x:1", "status": "ok"}), [{"name": "x", "how": "failed"}])
check("an old entry without services: a later run naming its container settles it",
      open_failures(legacy, run_of("20261012-010000-x", project="p", workdir="/opt/p", services=["x"],
                                   containers=["x"], image="acme/x:1")), [])
check("an old entry settles a newer one's containers only, never its services",
      open_failures(run_of("20261001-010000-z", "failed", project="p", services=["z"], containers=["p-z-1"]),
                    {"id": "20261002-010000-x", "kind": "update", "container": "x", "project": "p",
                     "image": "postgres:16", "status": "ok"}), [{"name": "p-z-1", "how": "failed"}])
# The Status Board and the menu icon read state and summary from status.json.
board = facts()
board["failed"]["ops"] = open_failures(db16_failed, db17_ok)
en, de = (S.document(board, lang, NOW, VERSION) for lang in ("en", "de"))
check("status: the failure of db16 is danger, in the summary and the detail",
      (en["state"], en["summary"], by_id(en)["failed"].get("detail"), de["summary"]),
      ("danger", "1 failed update", "stack-db16-1", "1 fehlgeschlagenes Update"))
board["failed"]["ops"] = open_failures(both_failed, web_ok)
en, de = (S.document(board, lang, NOW, VERSION) for lang in ("en", "de"))
check("status: the services still affected after an overlapping run",
      (en["state"], en["summary"], by_id(en)["failed"].get("detail"), by_id(de)["failed"].get("detail")),
      ("danger", "1 failed update", "shop-db-1 (still affected: db)", "shop-db-1 (noch betroffen: db)"))

# ------------------------------------------------------- the radar
doc = S.document(facts(found=False), "en", NOW, VERSION)
check("no versions.json: the radar none, new versions none",
      [(by_id(doc)[i]["state"], by_id(doc)[i]["title"]) for i in ("radar", "new-versions")],
      [("none", "Version radar: no data"), ("none", "New versions")])
for hours, want_en, want_de in ((2, "2 h", "2 Std."), (0.2, "< 1 h", "< 1 Std."), (30, "30 h", "30 Std."),
                                (72, "3 days", "3 Tage")):
    gen = S.iso(NOW - timedelta(hours=hours))
    en, de = (by_id(S.document(facts(generated=gen), lang, NOW, VERSION))["radar"] for lang in ("en", "de"))
    old = hours > 24
    check(f"radar {hours} h old: {'warning' if old else 'ok'}, its age",
          (en["state"], en["value"], de["value"], en["checked_at"], en["stale_after"]),
          ("warning" if old else "ok", want_en, want_de, gen, 86400))
    if old:
        check("outdated radar: title and what to check, German with Sie",
              (en["title"], en["detail"], de["title"], de["detail"]),
              ("Version radar outdated", "No run for more than 24 hours: check diun-radar.timer.",
               "Versions-Radar veraltet", "Seit mehr als 24 Stunden kein Lauf: Prüfen Sie diun-radar.timer."))
check("a radar time that cannot be read: warning",
      by_id(S.document(facts(generated="yesterday"), "en", NOW, VERSION))["radar"]["state"], "warning")

# A radar run that reached no registry, not even on its second try: a warning
# that names DNS and the network. The error itself (with a resolver address)
# stays out of status.json, which names containers only.
nr_versions = {"generated": "2026-10-12T02:17:34Z", "images": [],
               "no_registry": {"error": "lookup ghcr.io on 127.0.0.53:53: server misbehaving", "images": 21}}
check("radar facts: no registry reached, without the error text",
      S.radar_facts(nr_versions), {"found": True, "generated": "2026-10-12T02:17:34Z", "reached": False})
check("radar facts: a run that reached a registry carries no mark", "reached" in S.radar_facts(radar()), False)
nr_facts = facts()
nr_facts["radar"] = S.radar_facts(nr_versions)
en, de = (S.document(nr_facts, lang, NOW, VERSION) for lang in ("en", "de"))
check("status: a radar run that reached no registry is a warning, in the summary and the title",
      (en["state"], en["summary"], by_id(en)["radar"]["state"], by_id(en)["radar"]["title"], de["summary"]),
      ("warning", "Version radar reached no registry", "warning", "Version radar reached no registry",
       "Versions-Radar erreichte keine Registry"))
check("status: its detail points to DNS and the network, the age is the value, no address in it",
      ("DNS" in by_id(en)["radar"].get("detail", ""), "DNS" in by_id(de)["radar"].get("detail", ""),
       by_id(en)["radar"].get("value"),
       "127.0.0.53" in json.dumps(en) + json.dumps(de)),
      (True, True, by_id(S.document(facts(), "en", NOW, VERSION))["radar"]["value"], False))
nr_facts["radar"] = dict(nr_facts["radar"], generated=S.iso(NOW - timedelta(days=2)))
check("status: an old run that reached no registry is outdated first",
      by_id(S.document(nr_facts, "en", NOW, VERSION))["radar"]["title"], "Version radar outdated")

# ------------------------------------------------------- state and summary
quiet_doc = S.document(facts(), "en", NOW, VERSION)
check("nothing found: ok, and it says so", (quiet_doc["state"], quiet_doc["summary"]),
      ("ok", "No updates or problems found"))
check("nothing found, German", S.document(facts(), "de", NOW, VERSION)["summary"], "Keine Updates oder Probleme gefunden")
check("items: ok with 0, in the contract's order",
      [(it["id"], it["state"], it.get("value")) for it in quiet_doc["items"]],
      [("updates", "ok", "0"), ("failed", "ok", "0"), ("radar", "ok", "< 1 h"), ("rollout", "ok", "0"),
       ("new-versions", "ok", "0")])
busy = facts(updates=["a", "b"], failed=[("app", "failed")], generated=S.iso(NOW - timedelta(hours=25)),
             rollout=["db"], news=["x", "y", "z"])
en, de = (S.document(busy, lang, NOW, VERSION) for lang in ("en", "de"))
check("the gravest first: danger, warning, info", (en["state"], en["summary"]),
      ("danger", "1 failed update, version radar outdated, 2 updates available, 1 roll-out pending, 3 new versions"))
check("the same in German", de["summary"],
      "1 fehlgeschlagenes Update, Versions-Radar veraltet, 2 Updates verfügbar, 1 Container auszurollen, 3 neue Versionen")
check("plurals and the first letter",
      [S.document(facts(failed=[("a", "failed"), ("b", "aborted")], generated=S.iso(NOW - timedelta(days=2)),
                        updates=["a"], rollout=["b", "c"], news=["n"]), lang, NOW, VERSION)["summary"]
       for lang in ("en", "de")]
      + [S.document(facts(generated=S.iso(NOW - timedelta(days=2)), updates=["a"]), "en", NOW, VERSION)["summary"]],
      ["2 failed updates, version radar outdated, 1 update available, 2 roll-outs pending, 1 new version",
       "2 fehlgeschlagene Updates, Versions-Radar veraltet, 1 Update verfügbar, 2 Container auszurollen, 1 neue Version",
       "Version radar outdated, 1 update available"])
items = by_id(en)
check("updates: info, count and names", (items["updates"]["state"], items["updates"]["value"], items["updates"]["detail"]),
      ("info", "2", "a, b"))
check("failed: danger, a failed rollback and an aborted run named as such",
      [by_id(S.document(facts(failed=[("app", "failed"), ("cut", "aborted"), ("old", "rollback")]), lang, NOW,
                        VERSION))["failed"]["detail"] for lang in ("en", "de")],
      ["app, cut (aborted), old (rollback failed)", "app, cut (abgebrochen), old (Rollback fehlgeschlagen)"])
check("at most five names, then how many more",
      by_id(S.document(facts(rollout=list("abcdefg")), "en", NOW, VERSION))["rollout"]["detail"], "a, b, c, d, e +2")
nodiun = S.document(facts(events=False), "en", NOW, VERSION)
check("no Diun events: updates none, the add-on still ok",
      (by_id(nodiun)["updates"]["state"], by_id(nodiun)["updates"]["detail"], nodiun["state"]),
      ("none", "Diun has reported nothing yet.", "ok"))
check("overall state by the contract",
      [S.overall([{"state": s} for s in states]) for states in
       (["ok", "info"], ["warning", "danger", "info"], ["none", "ok"], ["none"], [])],
      ["info", "danger", "ok", "none", "none"])

# ------------------------------------------------------- the contract's shape
ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")
ISO_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
LINK_RE = re.compile(r"^#/[A-Za-z0-9/_-]{0,80}$")      # the Status Board's rule (LINK_RE in its src/lib/status.js)
STATES = {"none", "ok", "info", "warning", "danger"}


def shape(doc):
    """[] if the document is schema 1 as the contract says, else what is wrong."""
    bad = []
    if list(doc) != ["schema", "addon", "label", "version", "checked_at", "state", "summary", "items"]:
        bad.append(("keys", list(doc)))
    if (doc.get("schema"), doc.get("addon"), doc.get("label")) != (1, "image-updates", "Image Update Monitor"):
        bad.append("head")
    if doc.get("state") not in STATES or not doc.get("summary") or not ISO_RE.match(doc.get("checked_at") or ""):
        bad.append("state, summary or checked_at")
    for it in doc.get("items") or []:
        if not ID_RE.match(it.get("id", "")) or it.get("state") not in STATES or not it.get("title") \
                or not ISO_RE.match(it.get("checked_at") or "") or not isinstance(it.get("stale_after"), int) \
                or not all(isinstance(it[k], str) for k in ("value", "detail") if k in it) \
                or ("link" in it and not (isinstance(it["link"], str) and LINK_RE.match(it["link"]))) \
                or set(it) - {"id", "state", "title", "value", "detail", "link", "checked_at", "stale_after"}:
            bad.append(it)
    return bad


check("every document has the contract's shape (schema 1)",
      [shape(S.document(f, lang, NOW, VERSION)) for f in (facts(), busy, facts(found=False, events=False))
       for lang in ("en", "de")], [[]] * 6)
check("every item links its place on the page, in every state and language (test/places.js follows them)",
      sorted({(it["id"], it.get("link")) for f in (facts(), busy, facts(found=False, events=False))
              for lang in ("en", "de") for it in S.document(f, lang, NOW, VERSION)["items"]}),
      [("failed", "#/backups/history"), ("new-versions", "#/updates/new-versions"), ("radar", "#/updates"),
       ("rollout", "#/updates/rollout"), ("updates", "#/updates/update")])
check("a link the Status Board would not take is caught", shape({**en, "items": [dict(en["items"][0], link="/x")]}) != [],
      True)
check("the version is the page's", en["version"], VERSION)
GERMAN_RE = re.compile(r"[äöüÄÖÜß]|\b(Sie|Keine|Neue|Versionen|Fehlgeschlagene?|auszurollen|ausstehend|abgebrochen"
                       r"|Std|Tage?|Daten|nicht|noch|seinen?|Lauf|Zeitpunkt|Statusdatei)\b")
english = []
for f in (facts(), busy, facts(found=False, events=False), facts(generated="?"),
          facts(failed=[("a", "aborted"), ("b", "rollback")], generated=S.iso(NOW - timedelta(days=3)))):
    d = S.document(f, "en", NOW, VERSION)
    english += [d["summary"]] + [it[k] for it in d["items"] for k in ("title", "value", "detail") if k in it]
check("English: no German left", sorted({t for t in english if GERMAN_RE.search(t)}), [])
check("the German check finds German",
      all(GERMAN_RE.search(t) for t in ("Ausrollen ausstehend", "3 Tage", "< 1 Std.", "Neue Versionen")), True)

# ------------------------------------------------------- status.json of the fixtures
fx = fixture_doc()
with open(os.path.join(FIX, "status.json"), encoding="utf-8") as f:
    stored = json.load(f)
check("test/fixtures/status.json is the status of the other fixtures (python3 status_test.py --write-fixture)",
      dict(fx, version=None), dict(stored, version=None))
check("fixture: what the page shows for them",
      (fx["state"], fx["summary"]), ("info", "2 updates available, 3 roll-outs pending, 17 new versions"))

# ------------------------------------------------------- the file and `status`
for d in ("radar", "diun", "backups"):
    os.makedirs(os.path.join(TMP, d), exist_ok=True)
check("no status.json, no radar: none and why",
      [(d["state"], d["summary"], d["items"], shape(d)) for d in (S.show(lang, NOW, root=False) for lang in ("en", "de"))],
      [("none", "No status yet: the version radar is not set up or has not run yet.", [], []),
       ("none", "Noch kein Status: Der Versions-Radar ist nicht eingerichtet oder noch nicht gelaufen.", [], [])])
with open(S.VERSIONS, "w", encoding="utf-8") as f:
    json.dump(radar(rec), f)
check("no status.json yet, but the radar: it comes with its next run",
      S.show("en", NOW, root=False)["summary"], "No status yet: it comes with the next run of the version radar.")
with open(S.STATUS, "w", encoding="utf-8") as f:
    f.write("{broken")
check("status.json unreadable: none", (S.show("en", NOW, root=False)["state"], S.show("de", NOW, root=False)["summary"]),
      ("none", "Die Statusdatei ist nicht lesbar."))

CONTAINERS = [ctn("app-1", "acme/app", "1.0.0", version="1.0.0", version_src="tag", project="alice",
                  files=["/home/alice/app/compose.yml"]),
              ctn("db-1", "postgres", "16", pulled=True, project="alice"),
              ctn("web-1", "acme/web", "latest", digest="sha256:w1")]
S.inventory = lambda: [dict(c) for c in CONTAINERS]
with open(S.EVENTS, "w", encoding="utf-8") as f:
    f.write(event("acme/web:latest", "sha256:w2") + "\n")
with open(S.DISMISSED, "w", encoding="utf-8") as f:
    f.write("{}")
with open(S.OPS, "w", encoding="utf-8") as f:
    f.write(json.dumps({"id": "20261011-010000-app-1", "kind": "update", "container": "app-1", "project": "alice",
                        "workdir": "/home/alice/app", "image": "acme/app:1.0.0", "status": "ok"}) + "\n")
written = S.refresh(now=NOW)
with open(S.STATUS, encoding="utf-8") as f:
    text = f.read()
check("refresh: status.json for everybody (644), nothing left behind",
      (oct(os.stat(S.STATUS).st_mode & 0o777), sorted(os.listdir(os.path.dirname(S.STATUS)))),
      ("0o644", ["status.json", "versions.json"]))
check("refresh: in English, with its facts, the rows of the page",
      (json.loads(text) == written, written["summary"], sorted(written["facts"])),
      (True, "1 update available, 1 roll-out pending, 1 new version",
       ["checked_at", "failed", "new-versions", "radar", "rollout", "updates"]))
check("status.json names containers only: no project, path or user name",
      [s for s in ("alice", "/home", "compose.yml", "workdir") if s in text], [])

de = S.show("de", NOW, root=False)
check("status --lang de from the file: German, without the facts, the contract's shape",
      ([it["title"] for it in de["items"]], "facts" in de, shape(de), de["checked_at"]),
      (["Updates verfügbar", "Fehlgeschlagene Updates", "Versions-Radar", "Ausrollen ausstehend", "Neue Versionen"],
       False, [], S.iso(NOW)))
with open(S.VERSIONS, "w", encoding="utf-8") as f:
    json.dump(radar(rec, generated=S.iso(NOW - timedelta(hours=30))), f)
check("the radar's age judged when asked: the file was fresh, versions.json is 30 h old now",
      (by_id(S.show("en", NOW, root=False))["radar"]["state"], S.show("en", NOW, root=False)["state"]),
      ("warning", "warning"))
with open(S.OPS, "a", encoding="utf-8") as f:
    f.write(json.dumps({"id": "20261012-020000-web-1", "kind": "update", "container": "web-1", "project": "",
                        "image": "acme/web:latest", "status": "failed"}) + "\n")
check("as root the history is read again, without root the file counts",
      [(by_id(S.show("en", NOW, root=r))["failed"]["state"], by_id(S.show("en", NOW, root=r))["failed"].get("detail"))
       for r in (True, False)],
      [("danger", "web-1"), ("ok", None)])

before = text
S.inventory = lambda: (_ for _ in ()).throw(RuntimeError("Cannot connect to the Docker daemon"))
try:
    S.refresh(now=NOW)
    raised = None
except RuntimeError as e:
    raised = str(e)
with open(S.STATUS, encoding="utf-8") as f:
    check("Docker does not answer: refresh raises, the last file stays", (raised, f.read() == before),
          ("Cannot connect to the Docker daemon", True))
S.inventory = lambda: [dict(c) for c in CONTAINERS]

# The Diun folder is writable from the Diun container: its files are data.
if hasattr(os, "symlink") and hasattr(os, "O_NOFOLLOW"):
    secret = os.path.join(TMP, "secret.jsonl")
    shutil.move(S.EVENTS, secret)
    os.symlink(secret, S.EVENTS)
    check("events.jsonl as a link is not followed", by_id(S.refresh(now=NOW))["updates"]["state"], "none")
    os.unlink(S.EVENTS)
    os.mkfifo(S.EVENTS)
    check("events.jsonl as a pipe is not read (and does not block)", S.read_data(S.EVENTS, 100), None)
    os.unlink(S.EVENTS)
    shutil.move(secret, S.EVENTS)
with open(S.EVENTS, "w", encoding="utf-8") as f:
    f.write(event("acme/web:latest", "sha256:w1", ts="2026-10-01T00:00:00Z") + "\n" + "x" * 300 + "\n" +
            event("acme/web:latest", "sha256:w3", ts="2026-10-11T00:00:00Z") + "\n")
check("a large events.jsonl: its newest lines, from the first whole one",
      S.events_by_tag(S.read_data(S.EVENTS, 250, tail=True))[0]["acme/web:latest"]["digest"], "sha256:w3")

# ------------------------------------------------------- diun-update.py status
U = load("upd", "diun-update.py")
TMPU = os.path.join(TMP, "upd")
os.makedirs(TMPU)
U.BASE = os.path.join(TMPU, "backups")
U.DUMPS, U.OPS, U.LOGS = (os.path.join(U.BASE, x) for x in ("dumps", "operations.jsonl", "logs"))
U.LOCK = os.path.join(TMPU, "update.lock")
U.CONF = os.path.join(TMPU, "image-updates.conf")
U.VERSIONS = os.path.join(TMPU, "versions.json")


@contextlib.contextmanager
def as_uid(uid):
    real = os.geteuid if hasattr(os, "geteuid") else None
    os.geteuid = lambda: uid
    try:
        yield
    finally:
        if real:
            os.geteuid = real
        else:
            del os.geteuid


def call(argv, uid=1000, entry=False):
    out = io.StringIO()
    with as_uid(uid), contextlib.redirect_stdout(out):
        try:
            rc = (U.entry if entry else U.main)(argv)
        except U.Abort as e:
            print(str(e))
            rc = e.code
    if U._lock_fh:
        U._lock_fh.close()
        U._lock_fh = None
    return rc, out.getvalue()


rc, out = call(["status", "--lang", "de"])
check("status --lang de without root: exit 0, one JSON object in German",
      (rc, json.loads(out)["schema"], by_id(json.loads(out))["rollout"]["title"]), (0, 1, "Ausrollen ausstehend"))
check("status --lang: en, de-DE, pt_BR, and bare status (English)",
      [by_id(json.loads(call(a)[1]))["rollout"]["title"] for a in
       (["status", "--lang", "en"], ["status", "--lang", "de-DE"], ["status", "--lang", "pt_BR"], ["status"])],
      ["Roll-out pending", "Ausrollen ausstehend", "Roll-out pending", "Roll-out pending"])
check("wrong calls: exit 64",
      [call(a)[0] for a in (["status", "--lang"], ["status", "de"], ["status", "--lang", "../de"],
                            ["status", "--lang", "de", "--now"], ["status", "--lang", "d"])],
      [64] * 5)
check("other verbs still need root", call(["backups"])[0], 77)

# The hook: after update, retag, rollback and cleanup status.json is written anew, silently.
real_refresh, U.refresh_status = U.refresh_status, lambda: None
without = call(["rollback", "20261012-010000-nothing"], uid=0, entry=True)
U.refresh_status = real_refresh
os.unlink(S.STATUS)
rc, out = call(["rollback", "20261012-010000-nothing"], uid=0, entry=True)
check("after a rollback (also one refused): status.json written, the output the same as without",
      (rc, os.path.exists(S.STATUS), out), (2, True, without[1]))
real_cleanup, U.cmd_cleanup = U.cmd_cleanup, lambda dry=False: 0     # no Docker here: the hook is what counts
for argv, uid, want in ((["cleanup"], 0, True), (["cleanup", "--dry-run"], 0, False),
                        (["update", "bad name!"], 0, False), (["rollback", "x"], 1000, False),
                        (["status", "--lang", "en"], 1000, False)):
    if os.path.exists(S.STATUS):
        os.unlink(S.STATUS)
    call(argv, uid=uid, entry=True)
    check(f"status.json {'written' if want else 'not written'} after {' '.join(argv)} "
          f"({'root' if uid == 0 else 'no root'})", os.path.exists(S.STATUS), want)
U.cmd_cleanup = real_cleanup
S.inventory = lambda: (_ for _ in ()).throw(RuntimeError("docker gone"))
rc, out = call(["rollback", "20261012-010000-nothing"], uid=0, entry=True)
check("the hook is silent when Docker does not answer", (rc, "docker gone" in out), (2, False))
S.inventory = lambda: [dict(c) for c in CONTAINERS]

# ------------------------------------------------------- the radar run writes it
R = load("radar", "diun-versions.py")
check("the radar and diun-update.py share this module", (R.diunstatus is S, U.diunstatus is S), (True, True))
R.collect = lambda: {}
out_dir = os.path.join(TMP, "radar-run")
os.makedirs(out_dir)
argv, sys.argv = sys.argv, ["diun-versions.py", "--output", os.path.join(out_dir, "versions.json")]
try:
    so = io.StringIO()
    with contextlib.redirect_stdout(so):
        rc = R.main()
    with open(os.path.join(out_dir, "status.json"), encoding="utf-8") as f:
        st = json.load(f)
    check("the radar run writes status.json next to versions.json, with its own time",
          (rc, st["facts"]["radar"]["generated"] == json.load(open(os.path.join(out_dir, "versions.json")))["generated"],
           "status: " in so.getvalue()), (0, True, True))
    S.inventory = lambda: (_ for _ in ()).throw(RuntimeError("docker gone"))
    se = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(se):
        rc = R.main()
    check("a status problem never fails the radar run", (rc, "status not written: docker gone" in se.getvalue()),
          (0, True))
finally:
    sys.argv = argv

# The menu entry: one block at the end of System, after Services (100), Status Board 101 to Host
# Settings 106; found by "azentrius"; the README in Cockpit's Help menu under the product name.
with open(os.path.join(os.path.dirname(ROOT), "plugin", "manifest.json"), encoding="utf-8") as f:
    entry = json.load(f)["menu"]["index"]
check("the menu entry sits with the other Azentrius add-ons and links the README",
      (entry["order"], any("azentrius" in k["matches"] for k in entry["keywords"]), entry["docs"]),
      (104, True, [{"label": "Image Update Monitor",
                    "url": "https://github.com/Azentrius/cockpit-image-update-monitor#readme"}]))

shutil.rmtree(TMP, ignore_errors=True)
print("\nALL CHECKS PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

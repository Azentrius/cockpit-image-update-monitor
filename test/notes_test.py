#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Offline tests for lib/diunnotes.py, the release notes of stage 3: the
source of an image (known, label, ghcr.io), the releases of an answer and
which of them an image gets, when a source is asked again, GitHub's rate
limit and errors, and the radar run writing releases.json. GitHub is a fake
here; a request to the real one fails the test."""
import importlib.util
import io
import json
import os
import sys
import tempfile
import urllib.error
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(os.path.dirname(HERE), "lib")
sys.path.insert(0, ROOT)
import diunnotes as N  # noqa: E402

fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(("OK   " if ok else "FAIL ") + label + ("" if ok else f"\n     got  {got!r}\n     want {want!r}"))


def no_network(repo):
    raise AssertionError("a request to the real GitHub: " + repo)


N.fetch = no_network
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def image(**labels):
    return {"Config": {"Labels": labels}}


# ------------------------------------------------------------- source_of ----
src = N.source_of
check("known: an official image on Docker Hub", src("redis", []), {"repo": "redis/redis", "via": "known"})
check("known: written the long way", src("docker.io/library/nextcloud", []), {"repo": "nextcloud/server", "via": "known"})
check("known wins over a label that names the Dockerfile's repository",
      src("nextcloud", [image(**{"org.opencontainers.image.source": "https://github.com/nextcloud/docker"})]),
      {"repo": "nextcloud/server", "via": "known"})
check("label: OCI source with .git",
      src("cubejs/cube", [image(**{"org.opencontainers.image.source": "https://github.com/cube-js/cube.git"})]),
      {"repo": "cube-js/cube", "via": "label"})
check("label: label-schema vcs-url, git@ form",
      src("acme/app", [image(**{"org.label-schema.vcs-url": "git@github.com:acme/app.git"})]),
      {"repo": "acme/app", "via": "label"})
check("label: a path below the repository",
      src("acme/app", [image(**{"org.opencontainers.image.source": "https://github.com/acme/mono/tree/main/app"})]),
      {"repo": "acme/mono", "via": "label"})
check("label: the second image of the tag (pulled) names it",
      src("acme/app", [image(), image(**{"org.opencontainers.image.source": "https://github.com/acme/app"})]),
      {"repo": "acme/app", "via": "label"})
check("label on another host: no source", src("acme/app", [image(**{"org.opencontainers.image.source": "https://gitlab.com/acme/app"})]), None)
check("ghcr.io: the owner is a GitHub account", src("ghcr.io/formbricks/formbricks", [image()]),
      {"repo": "formbricks/formbricks", "via": "ghcr"})
check("ghcr.io with a deeper path: no guess", src("ghcr.io/acme/app/worker", []), None)
check("nothing: no source", src("postgres", [image()]), None)
for bad in ("https://github.com/../etc", "https://github.com/acme/..", "https://github.com/acme/.hidden",
            "https://github.com/-acme/app", "https://github.com/ac--me/app", "https://github.com/acme/a b",
            "https://github.com/acme", "https://github.com.evil.example/a/b", "https://evil.example/github.com/a/b"):
    check(f"from_url: no name GitHub would take, so never in a request: {bad}", N.from_url(bad), None)
check("from_url: path, query and fragment left out",
      [N.from_url(u) for u in ("https://github.com/acme/app#readme", "https://github.com/acme/app?tab=x",
                               "https://github.com/acme/app/../../other")], ["acme/app", "acme/app", "acme/app"])

# --------------------------------------------------------------- versions ----
check("vkey: trailing zeros do not count", (N.vkey("16.14") == N.vkey("16.14.0"), N.vkey("1.10") > N.vkey("1.9.9")), (True, True))
REC = {"image": "cubejs/cube", "tag": "v1.6.6", "status": "minor", "source": {"repo": "cube-js/cube", "via": "label"},
       "installed": {"i1": {"version": "1.6.6", "src": "tag"}, "i2": {"version": None, "src": None}},
       "versions": {"1.6.6": {"patch": "v1.6.74", "minor": "v1.8.3", "major": None, "status": "minor",
                              "choices": {"newer": [{"tag": "v1.6.74", "level": "patch"}, {"tag": "v1.7.9", "level": "minor"}]}}},
       "tag_now": {"version": "1.6.6"}}
check("wanted: installed and every newer version the record names", N.wanted(REC),
      ({"1.6.6"}, {"1.6.74", "1.8.3", "1.7.9", "1.6.6"}))
OTHER = dict(REC, image="cubejs/cube", tag="v1.5", installed={"x": {"version": "1.5.2"}},
             versions={"1.5.2": {"patch": "v1.5.9", "status": "patch"}}, tag_now=None, status="patch")
check("demands: the oldest installed and the newest known version per source",
      {k: (v["floor"], v["top"]) for k, v in N.demands([REC, OTHER]).items()}, {"cube-js/cube": ("1.5.2", "1.8.3")})
check("demands: nothing newer, no source, no installed version: not asked",
      N.demands([dict(REC, status="current"), dict(REC, source=None), dict(REC, installed={})]), {})


def gh(tag, body="", **kw):
    """One release as GitHub's API answers it."""
    r = {"tag_name": tag, "name": kw.pop("name", tag), "body": body, "draft": False, "prerelease": False,
         "published_at": kw.pop("published", "2026-10-01T10:00:00Z"),
         "html_url": f"https://github.com/acme/app/releases/tag/{tag}"}
    r.update(kw)
    return r


rels = N.parse_releases([gh("v1.8.3", "## Breaking\n- x"), gh("v1.9.0-rc.1"), gh("v1.9.0", prerelease=True),
                         gh("v2.0.0", draft=True), gh("stable"), gh("nightly-dev"), gh("2026.10.0b7"),
                         gh("v1.8.2", "y" * (N.BODY_MAX + 5), name="Fixes"),
                         gh("v1.8.1", html_url="https://example.com/x"), "not a release"])
check("parse: drafts, pre-releases and tags without a version left out",
      [(r["tag"], r["version"], r["prefix"]) for r in rels], [("v1.8.3", "1.8.3", ""), ("v1.8.2", "1.8.2", ""), ("v1.8.1", "1.8.1", "")])
check("parse: the text cut at BODY_MAX and marked, a name only when it says more than the tag",
      (len(rels[1]["body"]), rels[1].get("cut"), rels[1].get("name"), "name" in rels[0], "cut" in rels[0]),
      (N.BODY_MAX, True, "Fixes", False, False))
check("parse: links only to github.com", (rels[0]["url"].startswith("https://github.com/"), rels[2]["url"]), (True, None))
check("parse: prefixes", [(r["prefix"], r["version"]) for r in N.parse_releases([gh("release-1.31.7"), gh("n8n@2.44.4"), gh("v26.9.22.3-stable")])],
      [("release-", "1.31.7"), ("n8n@", "2.44.4"), ("", "26.9.22.3")])

# Maintenance branches come in the order of their dates, not their versions (redis, 2026-10-20).
REDIS = N.parse_releases([gh(t) for t in ("8.10.2", "8.8.3", "8.6.7", "8.4.7", "8.2.10", "8.10.1", "7.4.12", "7.4.11", "7.2.9")])
got, more = N.choose(REDIS, {N.vkey("7.4.11"), N.vkey("8.10.2")}, "7.4.11")
check("choose: newer than the oldest installed version, by version, newest first",
      ([r["version"] for r in got], more), (["8.10.2", "8.10.1", "8.8.3", "8.6.7", "8.4.7", "8.2.10", "7.4.12"], False))
check("choose: no prefix in what is kept", "prefix" in got[0], False)
MIXED = N.parse_releases([gh("app-3.1.0"), gh("app-3.0.0"), gh("v1.2.0"), gh("v1.1.0"), gh("v1.0.0"), gh("v0.9.0")])
check("choose: the kind of tags whose versions the image has, not the bigger one",
      [r["tag"] for r in N.choose(MIXED, {N.vkey("3.0.0"), N.vkey("3.1.0")}, "3.0.0")[0]], ["app-3.1.0"])
check("choose: none of the image's versions: the kind with the most releases",
      [r["tag"] for r in N.choose(MIXED, {N.vkey("9.9")}, "1.0")[0]], ["v1.2.0", "v1.1.0"])
MANY = N.parse_releases([gh(f"v1.{i}.0") for i in range(40)])
kept, more = N.choose(MANY, {N.vkey("1.0.0")}, "1.0.0")
check("choose: KEEP of them, the newest, and more", (len(kept), kept[0]["version"], kept[-1]["version"], more),
      (N.KEEP, "1.39.0", f"1.{39 - N.KEEP + 1}.0", True))


# ---------------------------------------------------------------- refresh ----
class Fake:
    """GitHub: answers per repository (a list of releases, or an exception)
    and the rate limit left after each answer."""

    def __init__(self, answers, left=50):
        self.answers, self.left, self.asked = answers, left, []

    def __call__(self, repo):
        self.asked.append(repo)
        a = self.answers.get(repo, [])
        if isinstance(a, Exception):
            raise a
        self.left -= 1
        return a, self.left


def rec(repo, installed="1.0.0", newer="1.2.0", status="minor"):
    return {"image": "x/" + repo, "tag": "latest", "status": status, "source": {"repo": repo, "via": "label"},
            "installed": {"i": {"version": installed}}, "tag_now": {"version": newer}}


def http(code, **headers):
    return urllib.error.HTTPError("https://api.github.com/x", code, "x", headers, None)


ANSWER = [gh("v1.2.0", "Breaking: the config moved"), gh("v1.1.0", "Fixes"), gh("v1.0.0")]
fake = Fake({"acme/app": ANSWER})
doc = N.refresh([rec("acme/app")], {}, NOW, get=fake)
e = doc["repos"]["acme/app"]
check("refresh: asked, the releases since the installed version, newest first",
      (fake.asked, e["status"], [r["version"] for r in e["releases"]], e["floor"], e["top"], e["more"], e["checked"]),
      (["acme/app"], "ok", ["1.2.0", "1.1.0"], "1.0.0", "1.2.0", False, "2026-10-20T12:00:00Z"))
check("refresh: a release as the page reads it",
      e["releases"][0], {"tag": "v1.2.0", "version": "1.2.0", "published": "2026-10-01T10:00:00Z",
                         "url": "https://github.com/acme/app/releases/tag/v1.2.0", "body": "Breaking: the config moved"})
fake = Fake({"acme/app": ANSWER})
later = N.refresh([rec("acme/app")], doc, NOW + timedelta(hours=6), get=fake)
check("refresh: the answer kept within TTL, nothing asked", (fake.asked, later["repos"]["acme/app"]), ([], e))
N.refresh([rec("acme/app", newer="1.3.0")], doc, NOW + timedelta(minutes=30), get=fake)
check("refresh: a newer version within RECHECK: not asked yet", fake.asked, [])
N.refresh([rec("acme/app", newer="1.3.0")], doc, NOW + timedelta(hours=2), get=fake)
check("refresh: a newer version after RECHECK: asked again", fake.asked, ["acme/app"])
fake = Fake({"acme/app": ANSWER})
N.refresh([rec("acme/app", installed="0.9.0")], doc, NOW + timedelta(hours=2), get=fake)
check("refresh: an older installed version than the answer had: asked again", fake.asked, ["acme/app"])
fake = Fake({"acme/app": ANSWER})
N.refresh([rec("acme/app")], doc, NOW + N.TTL, get=fake)
check("refresh: after TTL: asked again", fake.asked, ["acme/app"])
check("refresh: a source with nothing newer any more is left out",
      N.refresh([rec("acme/app", status="current")], doc, NOW, get=Fake({}))["repos"], {})

fake = Fake({"acme/gone": http(404)})
missing = N.refresh([rec("acme/gone")], {}, NOW, get=fake)
check("refresh: 404 is 'missing'", (fake.asked, missing["repos"]["acme/gone"]["status"]), (["acme/gone"], "missing"))
fake = Fake({})
N.refresh([rec("acme/gone", newer="1.5.0")], missing, NOW + timedelta(hours=3), get=fake)
check("refresh: 'missing' is not asked again before TTL, not even for a newer version", fake.asked, [])
N.refresh([rec("acme/gone")], missing, NOW + N.TTL, get=fake)
check("refresh: 'missing' after TTL: asked again", fake.asked, ["acme/gone"])

# GitHub's limit: 403 with none left, 403 with Retry-After (the secondary limit), 429.
for label, err in (("403, none left", http(403, **{"X-RateLimit-Remaining": "0"})),
                   ("403 with Retry-After", http(403, **{"Retry-After": "60"})), ("429", http(429))):
    fake = Fake({"acme/a": err, "acme/b": ANSWER})
    got = N.refresh([rec("acme/a"), rec("acme/b")], {"repos": {"acme/b": dict(e)}}, NOW + N.TTL, get=fake)
    check(f"refresh: the limit ({label}): no further request, the last answer of the others kept",
          (fake.asked, got["repos"]["acme/a"]["status"], got["repos"]["acme/b"]), (["acme/a"], "limited", e))
fake = Fake({"acme/a": http(403, **{"X-RateLimit-Remaining": "0"})})
got = N.refresh([rec("acme/a")], {"repos": {"acme/a": dict(e)}}, NOW + N.TTL, get=fake)["repos"]["acme/a"]
check("refresh: limited, the last releases stay with their time",
      (got["status"], got["releases"], got["checked"], got["tried"]), ("limited", e["releases"], e["checked"], "2026-10-21T12:00:00Z"))
fake = Fake({"acme/a": http(500), "acme/b": ANSWER})
got = N.refresh([rec("acme/a"), rec("acme/b")], {}, NOW, get=fake)
check("refresh: a server error is an error of that source, the others are asked",
      (fake.asked, got["repos"]["acme/a"], got["repos"]["acme/b"]["status"]),
      (["acme/a", "acme/b"], {"tried": "2026-10-20T12:00:00Z", "status": "error", "error": "HTTP 500"}, "ok"))
check("refresh: an error is asked again after RECHECK",
      (N.due(got["repos"]["acme/a"], {"floor": "1.0.0", "top": "1.2.0"}, NOW + timedelta(minutes=59)),
       N.due(got["repos"]["acme/a"], {"floor": "1.0.0", "top": "1.2.0"}, NOW + N.RECHECK)), (False, True))
fake = Fake({"acme/a": urllib.error.URLError("Name or service not known"), "acme/b": ANSWER})
got = N.refresh([rec("acme/a"), rec("acme/b")], {}, NOW, get=fake)
check("refresh: no network: no further request this run, nothing invented for the others",
      (fake.asked, got["repos"]["acme/a"]["status"], "acme/b" in got["repos"]), (["acme/a"], "error", False))
fake = Fake({}, left=N.RESERVE + 1)
got = N.refresh([rec(f"acme/r{i}") for i in range(5)], {}, NOW, get=fake)
check("refresh: RESERVE requests left for others on this address", fake.asked, ["acme/r0"])
fake = Fake({})
got = N.refresh([rec(f"acme/r{i:02}") for i in range(N.MAX_REQUESTS + 5)], {}, NOW, get=fake)
check("refresh: at most MAX_REQUESTS per run, the rest on a later run",
      (len(fake.asked), len(got["repos"])), (N.MAX_REQUESTS, N.MAX_REQUESTS))
check("summary: sources, releases and problems",
      (N.summary(doc), N.summary(missing), N.summary({})), ("1 sources, 2 releases", "1 sources, 0 releases (missing)", "0 sources, 0 releases"))
check("load: no file, a broken one, not an object: empty",
      (N.load("/nonexistent/releases.json"), N.load(__file__)), ({}, {}))

# ------------------------------------------------------- the radar run ----
# diun-versions.py --output writes releases.json next to versions.json, after
# it; the records name their source. Docker, the registry and GitHub are fakes.
spec = importlib.util.spec_from_file_location("radar_notes", os.path.join(ROOT, "diun-versions.py"))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)
check("the radar and this test share the module", R.diunnotes is N, True)
R.run = lambda cmd, binary=False: (_ for _ in ()).throw(RuntimeError("no commands here: " + " ".join(cmd)))
R.list_tags = lambda repo: ["1.0.0", "1.1.0", "1.2.0"]
R.match_digest = lambda *a, **k: None
GROUPS = {("ghcr.io/acme/app", "1.0.0"): {"containers": ["app"], "images": {"ia": image()}},
          ("postgres", "16.4"): {"containers": ["db"], "images": {"ib": image()}}}
R.collect = lambda: GROUPS
R.diunstatus.refresh = lambda path, versions=None: {"summary": "not written in this test"}
OUT = os.path.join(tempfile.mkdtemp(prefix="radar-notes-"), "versions.json")
fake = Fake({"acme/app": ANSWER})
N.fetch = fake
saved = sys.argv
sys.argv = ["diun-versions.py", "--output", OUT]
out = io.StringIO()
real_stdout, sys.stdout = sys.stdout, out
try:
    rc = R.main()
finally:
    sys.stdout = real_stdout
    sys.argv = saved
    N.fetch = no_network
versions = json.load(open(OUT))
notes = json.load(open(os.path.join(os.path.dirname(OUT), "releases.json")))
by_image = {r["image"]: r for r in versions["images"]}
check("radar: the source in versions.json (ghcr.io), none for postgres",
      (by_image["ghcr.io/acme/app"].get("source"), "source" in by_image["postgres"]), ({"repo": "acme/app", "via": "ghcr"}, False))
check("radar: releases.json next to it, the releases since the installed version",
      (rc, fake.asked, [r["tag"] for r in notes["repos"]["acme/app"]["releases"]], notes["generated"] == versions["generated"]),
      (0, ["acme/app"], ["v1.2.0", "v1.1.0"], True))
check("radar: says so in its log", "release notes: 1 sources, 2 releases -> " in out.getvalue(), True)
mode = oct(os.stat(os.path.join(os.path.dirname(OUT), "releases.json")).st_mode & 0o777)
check("radar: releases.json readable for the page, like versions.json", mode, "0o644")
# GitHub unreachable while the registry works: versions.json as before, the
# release notes of the last run kept with their time.
N.fetch = Fake({"acme/app": urllib.error.URLError("timed out")})
real_stdout, sys.stdout = sys.stdout, io.StringIO()
sys.argv = ["diun-versions.py", "--output", OUT]
old_due = N.due
N.due = lambda old, need, now: True     # the runs follow each other within seconds
try:
    rc = R.main()
finally:
    sys.stdout = real_stdout
    sys.argv = saved
    N.due = old_due
    N.fetch = no_network
again = json.load(open(os.path.join(os.path.dirname(OUT), "releases.json")))["repos"]["acme/app"]
check("radar: GitHub down: exit 0, the releases kept, marked",
      (rc, again["status"], [r["tag"] for r in again["releases"]], again["checked"]), (0, "error", ["v1.2.0", "v1.1.0"], notes["repos"]["acme/app"]["checked"]))
# A failure in the module never fails the radar run.
saved_refresh = N.refresh
N.refresh = lambda *a, **k: (_ for _ in ()).throw(ValueError("broken"))
err = io.StringIO()
real_stdout, real_stderr, sys.stdout, sys.stderr = sys.stdout, sys.stderr, io.StringIO(), err
sys.argv = ["diun-versions.py", "--output", OUT]
try:
    rc = R.main()
finally:
    sys.stdout, sys.stderr = real_stdout, real_stderr
    sys.argv = saved
    N.refresh = saved_refresh
check("radar: release notes broken: exit 0, versions.json written, the error in the log",
      (rc, "release notes not written: broken" in err.getvalue()), (0, True))
saved_source = N.source_of
N.source_of = lambda *a: (_ for _ in ()).throw(TypeError("bad label"))
try:
    r = R.analyse((("ghcr.io/acme/app", "1.0.0"), GROUPS[("ghcr.io/acme/app", "1.0.0")]), {}, NOW)
finally:
    N.source_of = saved_source
check("radar: a label it cannot read: the record without a source, as before", ("source" in r, r["status"]), (False, "minor"))

print("\nALL CHECKS PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

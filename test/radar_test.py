#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Offline tests for diunlib / diun-versions.py / diun-inventory.py
(Docker, skopeo and the Docker Hub API are replaced by fakes built from the
real values seen on a production host on 2026-10-12)."""
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(os.path.dirname(HERE), "lib")
sys.path.insert(0, ROOT)
import diunlib as L  # noqa: E402


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, file))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


R = load("radar", "diun-versions.py")
fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(("OK   " if ok else "FAIL ") + label + ("" if ok else f"\n     got  {got!r}\n     want {want!r}"))


def img(env=(), labels=None, digests=(), created="2026-06-01T00:00:00Z"):
    return {"Config": {"Env": list(env), "Labels": labels or {}}, "RepoDigests": list(digests), "Created": created}


# ---------------------------------------------------------------- diunlib
lv = L.local_version
check("pg 16-alpine + PG_VERSION", lv("postgres", "16-alpine", img(["GOSU_VERSION=1.19", "PG_VERSION=16.14"])), ("16.14", "env:PG_VERSION"))
check("pgvector pg17", lv("pgvector/pgvector", "pg17", img(["PG_VERSION=17.10-1.pgdg12+1"])), ("17.10", "env:PG_VERSION"))
check("glitchtip latest", lv("glitchtip/glitchtip", "latest", img(["PYTHON_VERSION=3.14.5", "GLITCHTIP_VERSION=6.1.8"])), ("6.1.8", "env:GLITCHTIP_VERSION"))
check("actual-ai: only NODE_VERSION", lv("docker.io/sakowicz/actual-ai", "latest", img(["NODE_VERSION=22.21.1"])), (None, None))
check("pinned v2.1.4", lv("ghcr.io/plausible/community-edition", "v2.1.4", img()), ("2.1.4", "tag"))
check("redis 7-alpine", lv("redis", "7-alpine", img(["REDIS_VERSION=7.4.11"])), ("7.4.11", "env:REDIS_VERSION"))
check("nextcloud apache", lv("nextcloud", "apache", img(["PHP_VERSION=8.5.10", "NEXTCLOUD_VERSION=34.0.3"])), ("34.0.3", "env:NEXTCLOUD_VERSION"))
check("onlyoffice ignores PG_VERSION", lv("onlyoffice/documentserver", "9.4.0.1", img(["PG_VERSION=16"])), ("9.4.0.1", "tag"))
check("socket-proxy ignores VERSION=1", lv("ghcr.io/tecnativa/docker-socket-proxy", "v0.4.1", img(["VERSION=1", "HAPROXY_VERSION=3.2.4"])), ("0.4.1", "tag"))
check("env not fitting the tag", lv("postgres", "16-alpine", img(["PG_VERSION=17.2"])), (None, None))
check("python 3.12-slim -> env more exact", lv("python", "3.12-slim", img(["PYTHON_VERSION=3.12.12"])), ("3.12.12", "env:PYTHON_VERSION"))
check("valkey latest", lv("valkey/valkey", "latest", img(["VALKEY_VERSION=9.1.0"])), ("9.1.0", "env:VALKEY_VERSION"))
# mariadb names its package version with the Debian epoch "1:" (a test host,
# 2026-10-18: the history showed "11.8.8 -> -" after an update to 11.8.9)
check("mariadb 11 + MARIADB_VERSION with the epoch",
      lv("mariadb", "11", img(["GOSU_VERSION=1.19", "MARIADB_VERSION=1:11.8.9+maria~ubu2404"])), ("11.8.9", "env:MARIADB_VERSION"))
check("core: epoch skipped, nothing else changes",
      [L.core(x) for x in ("1:11.8.9+maria~ubu2404", "2:1.2", "v6.2.0", "17.10-1.pgdg12+1", "1:", "1:16", "16", "master")],
      ["11.8.9", "1.2", "6.2.0", "17.10", None, None, None, None])
# Eclipse Temurin names its release with "jdk-" and the build, in the JRE
# images too (vpn-host, 2026-10-18: hibiscus-server built FROM
# eclipse-temurin:21-jre-jammy, whose tag points to 21.0.12.1_1-jre-jammy;
# the base's version stayed null). The variables of that image's config:
TEMURIN_ENV = ["PATH=/opt/java/openjdk/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
               "JAVA_HOME=/opt/java/openjdk", "LANG=en_US.UTF-8", "LANGUAGE=en_US:en", "LC_ALL=en_US.UTF-8"]
check("eclipse-temurin 21-jre-jammy + JAVA_VERSION with jdk- and the build",
      lv("eclipse-temurin", "21-jre-jammy", img(TEMURIN_ENV + ["JAVA_VERSION=jdk-21.0.12.1+1"])), ("21.0.12.1", "env:JAVA_VERSION"))
check("eclipse-temurin: a release without a point version",
      lv("docker.io/library/eclipse-temurin", "21-jre", img(TEMURIN_ENV + ["JAVA_VERSION=jdk-21.0.8+9"])), ("21.0.8", "env:JAVA_VERSION"))
check("eclipse-temurin 8 names no dotted version",
      lv("eclipse-temurin", "8-jre", img(TEMURIN_ENV + ["JAVA_VERSION=jdk8u504-b01"])), (None, None))
check("JAVA_VERSION not fitting the tag", lv("eclipse-temurin", "17-jre", img(["JAVA_VERSION=jdk-21.0.12.1+1"])), (None, None))
check("JAVA_VERSION belongs to the Java image, not to an app on it",
      lv("ghcr.io/acme/app", "latest", img(TEMURIN_ENV + ["JAVA_VERSION=jdk-21.0.12.1+1"])), (None, None))
check("core: jdk- skipped, the variable and the tag read the same",
      [L.core(x) for x in ("jdk-21.0.12.1+1", "21.0.12.1_1-jre-jammy", "jdk-21.0.8+9", "21.0.8_9-jre", "jdk8u504-b01", "jdk-21")],
      ["21.0.12.1", "21.0.12.1", "21.0.8", "21.0.8", None, None])
check("clickhouse pinned 4 blocks", lv("clickhouse/clickhouse-server", "24.3.3.102-alpine", img()), ("24.3.3.102", "tag"))
check("reach latest", L.reachable("latest"), ["patch", "minor", "major"])
check("reach 16-alpine", L.reachable("16-alpine"), ["patch", "minor"])
check("reach 3.12-slim", L.reachable("3.12-slim"), ["patch"])
check("reach v2.1.4", L.reachable("v2.1.4"), [])
check("qualify", [L.qualify(x) for x in ("postgres", "docker.io/sakowicz/actual-ai", "ghcr.io/a/b", "glitchtip/glitchtip")],
      ["docker.io/library/postgres", "docker.io/sakowicz/actual-ai", "ghcr.io/a/b", "docker.io/glitchtip/glitchtip"])

# ------------------------------------------------------------- comparison
PG_TAGS = ["16", "16-alpine", "16.14-alpine", "16.15-alpine", "16.15-alpine3.22", "17.6-alpine", "18.0-alpine",
           "18.1-alpine", "18-alpine", "18.1", "16.14", "16.15", "18rc1-alpine"]
fs = R.find_scheme
check("scheme 16-alpine/16.14", fs("16.14", "16-alpine", set(PG_TAGS)), ("", 2, "-alpine"))
check("scheme apache", fs("34.0.3", "apache", {"34.0.3-apache", "34.0.4-apache", "34.0.3"}), ("", 3, "-apache"))
check("scheme v-prefix", fs("6.2.0", "latest", {"v6.2.0", "v6.3.1"}), ("v", 3, ""))
check("label 24.04 rejected", fs("24.04", "latest", {"0.30.11", "0.30.12"}, exact=True), None)
check("pgvector 17.10 has no scheme", fs("17.10", "pg17", {"pg17", "pg18", "0.8.0-pg17"}), None)
c = R.compare("16.14-alpine", PG_TAGS)
check("pg 16.14 -> minor/major", (c["minor"], c["major"], c["status"]), ("16.15-alpine", "18.1-alpine", "major"))
check("best_tag postfix", R.best_tag(["5.1.0-debian", "v5.1.0-debian", "5.1.0", "v5.1.0", "latest-debian", "latest"]), "5.1.0")
check("best_tag glitchtip", R.best_tag(["6.1", "6.1.8"]), "6.1.8")
check("best_tag nothing", R.best_tag(["latest", "stable"]), None)
check("newest_plain", R.newest_plain(["6.0.0", "6.0", "6", "sha256-abc.sig", "5.4.4", "6.0.2", "6.0.1", "latest"]), "6.0.2")

# ---------------------------------------------------------------- choices
FB_ALL = ["latest", "stable", "main", "edge", "4", "4.9", "4.9.3", "5", "5.0", "5.0.1", "5.0.2", "5.0.3", "5.1",
          "5.1.0", "5.1.1", "5.2.0", "5.3.0", "5.3.2", "5.4", "5.4.4", "5.4.5", "v5.4.5", "6", "6.0", "6.0.0", "6.0.1",
          "6.0.2", "6.1.0-rc.1"]
ch = R.choices("5.0.2", FB_ALL, set(FB_ALL))
check("choices: pin the running version", ch["pin"], "5.0.2")
check("choices: version lines with their newest version, broad first, none older than 5.0.2",
      [(x["tag"], x["newest"]) for x in ch["lines"]],
      [("6", "6.0.2"), ("6.0", "6.0.2"), ("5", "5.4.5"), ("5.4", "5.4.5"), ("5.1", "5.1.1"), ("5.0", "5.0.3")])
check("choices: every newer patch, the newest of each newer minor line, no rc, no v-prefix",
      [(x["tag"], x["level"]) for x in ch["newer"]],
      [("5.0.3", "patch"), ("5.4.5", "minor"), ("5.3.2", "minor"), ("5.2.0", "minor"), ("5.1.1", "minor"),
       ("6.0.2", "major")])
check("choices: floating tags without pre-releases", ch["floating"], ["latest", "stable"])
NC_ALL = ["apache", "stable-apache", "production-apache", "latest", "fpm", "33-apache", "33.0.9-apache", "34-apache",
          "34.0-apache", "34.0.3-apache", "34.0.4-apache", "35-apache", "35.0-apache", "35.0.1-apache", "35.0.1-fpm",
          "35.0.1"]
ch = R.choices("34.0.3-apache", NC_ALL, set(NC_ALL))
check("choices apache: the flavour stays (no fpm, no plain 35.0.1)",
      (ch["pin"], [x["tag"] for x in ch["lines"]], [(x["tag"], x["level"]) for x in ch["newer"]], ch["floating"]),
      ("34.0.3-apache", ["35-apache", "35.0-apache", "34-apache", "34.0-apache"],
       [("34.0.4-apache", "patch"), ("35.0.1-apache", "major")], ["apache", "stable-apache", "production-apache"]))
ch = R.choices("16.14-alpine", PG_TAGS, set(PG_TAGS))
check("choices 16-alpine: every newer minor, the newest of each newer major",
      (ch["pin"], [(x["tag"], x["newest"]) for x in ch["lines"]], [(x["tag"], x["level"]) for x in ch["newer"]]),
      ("16.14-alpine", [("18-alpine", "18.1-alpine"), ("16-alpine", "16.15-alpine")],
       [("16.15-alpine", "minor"), ("18.1-alpine", "major"), ("17.6-alpine", "major")]))
check("choices: no pin when the registry lacks the running version's tag",
      R.choices("16.13-alpine", PG_TAGS, set(PG_TAGS))["pin"], None)
VK_ALL = ["latest", "8", "8.1", "8.1.4", "9", "9.1", "9.1.0", "9.1.2", "9.2", "9.2.0-rc1"]
check("choices: a line without a released version (9.2 = rc) is left out",
      [(x["tag"], x["newest"]) for x in R.choices("9.1.0", VK_ALL, set(VK_ALL))["lines"]],
      [("9", "9.1.2"), ("9.1", "9.1.2")])
CH_ALL = (["24.3.3.102-alpine", "24.3.18.7-alpine", "24.3-alpine", "26.9.19-alpine", "26.9.19.9-alpine"] +
          [f"{y}.{m}{s}" for y, last in ((25, 12), (26, 9)) for m in range(1, last + 1) for s in ("-alpine", ".1.1-alpine")])
ch = R.choices("24.3.3.102-alpine", CH_ALL, set(CH_ALL))
check("choices with four blocks: only major/minor lines, the running line stays among many newer ones",
      [x["tag"] for x in ch["lines"]],
      [f"26.{m}-alpine" for m in range(9, 0, -1)] + ["25.12-alpine", "25.11-alpine", "24.3-alpine"])
MANY_TAGS = ([f"0.30.{i}" for i in range(11, 41)] + [f"0.{m}.{p}" for m in range(31, 61) for p in (0, 1)] +
             [f"{m}.{n}.0" for m in range(1, 21) for n in (0, 1)])
ch = R.choices("0.30.11", MANY_TAGS, set(MANY_TAGS))
check("choices are capped: 10 patches, 15 minor lines, 15 majors",
      (len(ch["newer"]), [ch["newer"][i]["tag"] for i in (0, 9, 10, 24, 25, 39)]),
      (40, ["0.30.40", "0.30.31", "0.60.1", "0.46.1", "20.1.0", "6.1.0"]))

# ---------------------------------------------------------- hub / registry
D_OLLAMA = "sha256:c484b703176aa19dfc0a54cbfb60ab8094b38faa04283fb77eba1d33319e5eca"
pages_seen = []


def fake_urlopen(req, timeout=0):
    url = req.full_url
    pages_seen.append(url)
    page = int(url.split("page=")[1]) if "page=" in url else 1
    results = [{"name": f"0.31.{i}-rc{page}", "digest": "sha256:x", "last_updated": "2026-09-01T00:00:00Z"} for i in range(100)]
    nxt = f"{R.HUB}ollama/ollama/tags?page_size=100&ordering=last_updated&page={page + 1}"
    if page == 3:
        results[50] = {"name": "0.30.11", "digest": D_OLLAMA, "last_updated": "2026-06-26T00:00:00Z"}
    return io.BytesIO(json.dumps({"results": results, "next": nxt}).encode())


R.urllib.request.urlopen = fake_urlopen
check("hub match ollama on page 3", R.hub_match("ollama/ollama", D_OLLAMA, None), "0.30.11")
check("hub stopped after the hit page", len(pages_seen), 3)
pages_seen.clear()
check("hub stops at old pages", R.hub_match("ollama/ollama", "sha256:nothing",
                                             datetime(2026, 10, 1, tzinfo=timezone.utc)), None)
check("hub: 1 page because page 1 is older than created-2d", len(pages_seen), 1)

MANIFESTS = {"6.0.2": b"m602", "6.0.1": b"m601", "5.4.5": b"m545"}
skopeo_calls = []


def fake_run(cmd, binary=False):
    if cmd[:3] == ["skopeo", "inspect", "--raw"]:
        t = cmd[3].rsplit(":", 1)[1]
        skopeo_calls.append(t)
        return MANIFESTS.get(t, b"other-" + t.encode())
    raise AssertionError(cmd)


R.run = fake_run
want = "sha256:" + hashlib.sha256(b"m545").hexdigest()
FB_TAGS = ["6.0.0", "6.0", "6", "sha256-x.sig", "5.4.4", "6.0.1", "5.4.5", "6.0.2", "latest"]
check("ghcr manifest match", R.registry_match("ghcr.io/formbricks/formbricks", want, FB_TAGS), "5.4.5")
check("ghcr checks newest first", skopeo_calls[:3], ["6.0.2", "6.0.1", "6.0.0"])

# The installed image may be older than the 20 newest version tags
# (Formbricks 5.0.2 behind 19 newer 5.x and three 6.x tags).
MANY = [f"5.{m}.{p}" for m in range(5) for p in range(6)] + ["6.0.0", "6.0.1", "6.0.2"]
MANIFESTS["5.0.2"] = b"m502"
want502 = "sha256:" + hashlib.sha256(b"m502").hexdigest()
skopeo_calls.clear()
check("20 newest tags miss an older build", R.registry_match("ghcr.io/formbricks/formbricks", want502, MANY), None)
check("... and stop after 20", len(skopeo_calls), 20)
check("a deeper search finds it", R.registry_match("ghcr.io/formbricks/formbricks", want502, MANY, 100), "5.0.2")

# What a floating tag points to right now, without a pull
MANIFESTS["latest"] = b"m602"
d602 = "sha256:" + hashlib.sha256(b"m602").hexdigest()
check("tag digest outside Docker Hub: the manifest", R.tag_digest("ghcr.io/formbricks/formbricks", "latest"), d602)
skopeo_calls.clear()
tn = R.resolve_tag("ghcr.io/formbricks/formbricks", "latest", FB_TAGS, None)
check("latest resolves to 6.0.2", tn, {"digest": d602, "version": "6.0.2", "tag": "6.0.2"})
skopeo_calls.clear()
check("same digest: the previous result is reused",
      (R.resolve_tag("ghcr.io/formbricks/formbricks", "latest", FB_TAGS, tn), skopeo_calls), (tn, ["latest"]))
MANIFESTS["latest"] = b"new-build"
dnew = "sha256:" + hashlib.sha256(b"new-build").hexdigest()
check("a build without a version tag: version unknown, digest kept",
      R.resolve_tag("ghcr.io/formbricks/formbricks", "latest", FB_TAGS, tn), {"digest": dnew, "version": None, "tag": None})
hub_urls = []


def hub_tag(req, timeout=0):
    hub_urls.append(req.full_url)
    return io.BytesIO(json.dumps({"name": "latest", "digest": D_OLLAMA}).encode())


real_urlopen, R.urllib.request.urlopen = R.urllib.request.urlopen, hub_tag
n_skopeo = len(skopeo_calls)
check("tag digest on Docker Hub: the API, no manifest",
      (R.tag_digest("ollama/ollama", "latest"), hub_urls, len(skopeo_calls) - n_skopeo),
      (D_OLLAMA, [R.HUB + "ollama/ollama/tags/latest"], 0))
check("official images live under library/", (R.tag_digest("postgres", "16-alpine"), hub_urls[-1]),
      (D_OLLAMA, R.HUB + "library/postgres/tags/16-alpine"))
R.urllib.request.urlopen = real_urlopen
check("hub path: host compared as a whole, names checked",
      [R.hub_path(x) for x in ("postgres", "ollama/ollama", "docker.io/sakowicz/actual-ai", "ghcr.io/a/b",
                               "evil.example/docker.io/x", "docker.io/../x", "docker.io/a/b?c")],
      ["library/postgres", "ollama/ollama", "sakowicz/actual-ai", None, None, None, None])
try:
    R.tag_digest("ollama/ollama", "latest/../../x")
    bad_tag = "accepted"
except ValueError:
    bad_tag = "refused"
check("a tag outside Docker's grammar never reaches a URL", bad_tag, "refused")

# A latest no version tag shares a digest with: pushed after the newest
# release? (a test host, 2026-10-16: MobSF's latest is a 4.5.4 build of its
# development branch, v4.5.3 the newest release)
MOBSF = "opensecurity/mobile-security-framework-mobsf"
D_MOBSF = "sha256:69851c52cca7b726b84278404c1d3d387a4937cd3770dccbf16f3e0dc00f2c7b"
MOBSF_TAGS = ["latest", "v4.5.3", "v4.5.2", "v4.4.6"]
HUB_TAGS = {"latest": {"digest": D_MOBSF, "tag_last_pushed": "2026-10-13T05:15:45.204517Z"},
            "v4.5.3": {"digest": "sha256:5113fbad", "tag_last_pushed": "2026-09-21T03:04:04.81Z"}}
hub_urls.clear()


def hub_tags(req, timeout=0):
    hub_urls.append(req.full_url)
    name = req.full_url.rsplit("/", 1)[1]
    if name not in HUB_TAGS:
        raise RuntimeError("HTTP Error 404: Not Found")
    return io.BytesIO(json.dumps(HUB_TAGS[name]).encode())


R.urllib.request.urlopen = hub_tags
real_md, R.match_digest = R.match_digest, lambda repo, digest, created, tags, limit=20: None
AFTER = {"pushed": "2026-10-13T05:15:45Z", "after": {"tag": "v4.5.3", "pushed": "2026-09-21T03:04:04Z"}}
check("latest pushed weeks after the newest release", R.build_after(MOBSF, "latest", MOBSF_TAGS), AFTER)
check("... asked Docker Hub for both tags", sorted(u.rsplit("/", 1)[1] for u in hub_urls), ["latest", "v4.5.3"])
tn = R.resolve_tag(MOBSF, "latest", MOBSF_TAGS, None)
check("tag_now without a version notes the release before", tn, {"digest": D_MOBSF, "version": None, "tag": None, **AFTER})
check("same digest from a radar before the note: the note is added",
      R.resolve_tag(MOBSF, "latest", MOBSF_TAGS, {"digest": D_MOBSF, "version": None, "tag": None}), tn)
HUB_TAGS["v4.5.4"] = {"digest": "sha256:5114", "tag_last_pushed": "2026-10-20T00:00:00Z"}
check("a release after the build: no note, from a reused result neither",
      R.resolve_tag(MOBSF, "latest", MOBSF_TAGS + ["v4.5.4"], tn), {"digest": D_MOBSF, "version": None, "tag": None})
HUB_TAGS["v4.5.4"] = {"digest": "sha256:5114", "last_updated": "2026-10-13T05:05:00Z"}
check("two builds of one release, minutes apart, do not count", R.build_after(MOBSF, "latest", MOBSF_TAGS + ["v4.5.4"]), None)
del HUB_TAGS["v4.5.4"]
hub_urls.clear()
check("not on Docker Hub, a tag with numbers, no release: Docker Hub not asked",
      (R.build_after("ghcr.io/a/b", "latest", MOBSF_TAGS), R.build_after(MOBSF, "16-alpine", MOBSF_TAGS),
       R.build_after(MOBSF, "latest", ["latest", "edge"]), hub_urls), (None, None, None, []))
del HUB_TAGS["v4.5.3"]
check("the note's lookup fails: tag_now without it",
      R.resolve_tag(MOBSF, "latest", MOBSF_TAGS, None), {"digest": D_MOBSF, "version": None, "tag": None})
R.match_digest = lambda repo, digest, created, tags, limit=20: "v4.5.3"
hub_urls.clear()
check("a version tag with the digest: no note, no further request",
      (R.resolve_tag(MOBSF, "latest", MOBSF_TAGS, None), len(hub_urls)),
      ({"digest": D_MOBSF, "version": "4.5.3", "tag": "v4.5.3"}, 1))
R.match_digest = real_md
R.urllib.request.urlopen = real_urlopen

# ---------------------------------------------------------------- analyse
TAGS = {
    "postgres": PG_TAGS,
    "glitchtip/glitchtip": ["6.1", "6.1.8", "6.2.0", "6.2", "7.0.1", "latest"],
    "docker.io/ollama/ollama": ["0.30.11", "0.30.12", "0.31.0", "0.31.0-rc1", "latest"],
    "pgvector/pgvector": ["pg17", "pg18", "0.8.0-pg17", "0.8.1-pg18"],
    "boky/postfix": ["latest"],
}
R.list_tags = lambda repo: TAGS[repo]
hub_calls, limits, tag_matches = [], [], []


def fake_match(repo, digest, created, tags, limit=20):
    """Installed images come with their creation date, tag_now lookups without."""
    if created is None:
        tag_matches.append(repo)
    else:
        hub_calls.append(repo)
        limits.append(limit)
    return "0.30.11"


R.match_digest = fake_match
tag_digests = []
R.tag_digest = lambda repo, tag: (tag_digests.append((repo, tag)), "sha256:now-" + tag)[1]
now = datetime(2026, 10, 12, tzinfo=timezone.utc)
groups = {
    ("postgres", "16-alpine"): {"containers": ["glitchtip-postgres", "docuseal-db"], "images": {
        "id1614": img(["PG_VERSION=16.14"]), "id1615": img(["PG_VERSION=16.15"])}},
    ("glitchtip/glitchtip", "latest"): {"containers": ["glitchtip-web"], "images": {"idg": img(["GLITCHTIP_VERSION=6.1.8"])}},
    ("docker.io/ollama/ollama", "latest"): {"containers": ["ollama"], "images": {
        "ido": img(labels={"org.opencontainers.image.version": "24.04"}, digests=["ollama/ollama@" + D_OLLAMA])}},
    ("pgvector/pgvector", "pg17"): {"containers": ["formbricks-db"], "images": {"idv": img(["PG_VERSION=17.10-1.pgdg12+1"])}},
}
recs = {k: R.analyse((k, g), {}, now) for k, g in groups.items()}
pg = recs[("postgres", "16-alpine")]
check("pg installed per image", {k: v["version"] for k, v in pg["installed"].items()}, {"id1614": "16.14", "id1615": "16.15"})
check("pg 16.14 vs 16.15", (pg["versions"]["16.14"]["minor"], pg["versions"]["16.15"]["minor"], pg["versions"]["16.15"]["major"]),
      ("16.15-alpine", None, "18.1-alpine"))
check("pg reach + db", (pg["reach"], pg["db"]), (["patch", "minor"], True))
gl = recs[("glitchtip/glitchtip", "latest")]
check("glitchtip 6.1.8 -> minor 6.2.0, major 7.0.1", (gl["versions"]["6.1.8"]["minor"], gl["versions"]["6.1.8"]["major"]), ("6.2.0", "7.0.1"))
ol = recs[("docker.io/ollama/ollama", "latest")]
check("ollama label rejected, digest 0.30.11", (ol["installed"]["ido"]["version"], ol["installed"]["ido"]["src"]), ("0.30.11", "digest"))
check("ollama newer 0.31.0 (no rc)", ol["versions"]["0.30.11"]["minor"], "0.31.0")
pv = recs[("pgvector/pgvector", "pg17")]
check("pgvector: version unknown-scheme, by_tag major pg18", (pv["versions"]["17.10"]["status"], pv["by_tag"]["major"]), ("unknown", "pg18"))
check("choices per installed version, none without a scheme",
      (ol["versions"]["0.30.11"]["choices"], pg["versions"]["16.15"]["choices"]["newer"][0], "choices" in pv["versions"]["17.10"]),
      ({"pin": "0.30.11", "lines": [], "newer": [{"tag": "0.30.12", "level": "patch"}, {"tag": "0.31.0", "level": "minor"}],
        "floating": ["latest"]}, {"tag": "18.1-alpine", "level": "major"}, False))
check("newest for ollama", ol["newest"], "0.31.0")
check("first lookup of an image searches deep", limits, [R.REGISTRY_CANDIDATES_DEEP])
check("floating tags resolved, exact ones not",
      sorted(tag_digests), [("docker.io/ollama/ollama", "latest"), ("glitchtip/glitchtip", "latest"),
                            ("pgvector/pgvector", "pg17"), ("postgres", "16-alpine")])
check("tag_now of ollama latest", ol["tag_now"], {"digest": "sha256:now-latest", "version": "0.30.11", "tag": "0.30.11"})
check("tag_now cache from the previous run", R.tag_now_cache([ol, gl, {"image": "x", "tag": "1"}])[
      ("docker.io/ollama/ollama", "latest")], ol["tag_now"])
hub_calls.clear()
tag_matches.clear()
again = R.analyse((("docker.io/ollama/ollama", "latest"), groups[("docker.io/ollama/ollama", "latest")]),
                  R.digest_cache([ol]), now, R.tag_now_cache([ol]))
check("unchanged digest: tag_now reused, no search", (again["tag_now"], hub_calls, tag_matches), (ol["tag_now"], [], []))
real_td, R.tag_digest = R.tag_digest, lambda repo, tag: (_ for _ in ()).throw(RuntimeError("hub down"))
broken = R.analyse((("glitchtip/glitchtip", "latest"), groups[("glitchtip/glitchtip", "latest")]), {}, now)
check("tag lookup fails: no tag_now, the record stays good", ("tag_now" in broken, broken["status"]), (False, "major"))
R.tag_digest = real_td

# cache: second run must not query again; miss is retried only after 7 days
cache = R.digest_cache([ol])
hub_calls.clear()
R.analyse((("docker.io/ollama/ollama", "latest"), groups[("docker.io/ollama/ollama", "latest")]), cache, now)
check("cached digest result reused", hub_calls, [])
miss = {"ido": {"version": None, "src": "digest", "ts": "2026-10-10T00:00:00Z"}}
R.analyse((("docker.io/ollama/ollama", "latest"), groups[("docker.io/ollama/ollama", "latest")]), miss, now)
check("recent miss not retried", hub_calls, [])
miss = {"ido": {"version": None, "src": "digest", "ts": "2026-09-01T00:00:00Z"}}
limits.clear()
R.analyse((("docker.io/ollama/ollama", "latest"), groups[("docker.io/ollama/ollama", "latest")]), miss, now)
check("old miss retried, only the newest tags", (hub_calls, limits), (["docker.io/ollama/ollama"], [R.REGISTRY_CANDIDATES]))

# registry error keeps previous good record, marks it stale
R.list_tags = lambda repo: (_ for _ in ()).throw(RuntimeError("registry down"))
err = R.analyse((("glitchtip/glitchtip", "latest"), groups[("glitchtip/glitchtip", "latest")]), {}, now)
check("error record keeps local version", (err["status"], err["installed"]["idg"]["version"]), ("error", "6.1.8"))
merged = R.keep_last_good([err], [gl])[0]
check("stale merge keeps the last tag_now too",
      (merged.get("stale"), merged["versions"]["6.1.8"]["minor"], merged["tag_now"]["digest"]), (True, "6.2.0", "sha256:now-latest"))


# a repository no registry shows: built or tagged on the server, or private
# (skopeo's answers on a real host, 2026-10-15, and of other registries)
def refuse(stderr):
    def list_tags(repo):
        raise R.subprocess.CalledProcessError(1, ["skopeo", "list-tags"], "", stderr)
    return list_tags


DENIED = ('time="2026-10-15T23:35:49Z" level=fatal msg="Error listing repository tags: fetching tags list: '
          'requested access to the resource is denied"')
LOCAL = (("hibiscus-server", "2.12.4"), {"containers": ["hibiscus-server"], "images": {"idh": img()}})
R.list_tags = refuse(DENIED)
hib = R.analyse(LOCAL, {}, now)
check("Docker Hub denies a name it does not have: unlisted, the tag still names the installed version",
      (hib["status"], hib["error"], hib["installed"]["idh"]["version"], "versions" in hib), ("unlisted", DENIED, "2.12.4", False))
ANSWERS = {
    "ghcr.io, private": ("fetching tags list: unauthorized: authentication required", "unlisted"),
    "registry without the name": ("fetching tags list: name unknown: repository name not known to registry", "unlisted"),
    "rate limit": ("toomanyrequests: You have reached your pull rate limit", "error"),
    "server error": ("fetching tags list: received unexpected HTTP status: 503 Service Unavailable", "error"),
    "no DNS": ("pinging container registry: dial tcp: lookup registry-1.docker.io: no such host", "error"),
}
got = {}
for label, (stderr, _) in ANSWERS.items():
    R.list_tags = refuse(stderr)
    got[label] = R.analyse(LOCAL, {}, now)["status"]
check("skopeo's answer decides: not in a registry, or a registry error",
      got, {label: want for label, (_, want) in ANSWERS.items()})
R.list_tags = lambda repo: (_ for _ in ()).throw(R.subprocess.TimeoutExpired(["skopeo"], R.TIMEOUT))
check("a timeout is a registry error", R.analyse(LOCAL, {}, now)["status"], "error")
check("unlisted without an earlier result stays as it is", R.keep_last_good([hib], [])[0], hib)
R.list_tags = refuse(DENIED)
gone = R.keep_last_good([R.analyse((("glitchtip/glitchtip", "latest"), groups[("glitchtip/glitchtip", "latest")]),
                                   {}, now)], [gl])[0]
check("a repository the registry no longer shows keeps its last good result, marked stale",
      (gone["status"], gone.get("stale"), gone["error"], gone["versions"]["6.1.8"]["minor"]), ("major", True, DENIED, "6.2.0"))
check("unlisted sorts after registry errors", sorted(["unlisted", "error", "current"], key=R.ORDER.get),
      ["current", "error", "unlisted"])

# ------------------------------------------------------------ built images
# An image built on the server (a test host, 2026-10-18: caddy-caddy is
# xcaddy on caddy:2-builder, copied onto caddy:2): its Dockerfile's bases.
CADDY = """FROM caddy:2-builder AS builder
RUN xcaddy build --with github.com/caddy-dns/hetzner/v2

FROM caddy:2
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
"""
check("Dockerfile: the build stage and the base of the image that runs",
      R.parse_from(CADDY), [{"image": "caddy", "tag": "2-builder", "final": False}, {"image": "caddy", "tag": "2", "final": True}])
MULTI = """ARG PY=3.12
FROM --platform=$BUILDPLATFORM golang:1.23 AS build
FROM python:${PY}-slim AS base
FROM base AS final
FROM scratch AS empty
FROM ghcr.io/acme/tool:v2@sha256:abc AS tool
FROM ${UNSET}/x:1
from final
"""
check("Dockerfile: ARG defaults, stages followed, scratch and unknown variables left out, pins marked",
      R.parse_from(MULTI), [{"image": "golang", "tag": "1.23", "final": False},
                            {"image": "python", "tag": "3.12-slim", "final": True},
                            {"image": "ghcr.io/acme/tool", "tag": "v2", "final": False, "pinned": True}])
check("Dockerfile: nothing external", R.parse_from("FROM scratch\nCOPY x /x\n"), [])
check("times: offset, Z, Diun's form",
      [R.iso(R.parse_ts(s)) for s in ("2026-06-15T20:35:36.213970238+02:00", "2026-10-14T00:20:11Z", "2026-10-14 01:36:06 +0000 UTC")],
      ["2026-06-15T18:35:36Z", "2026-10-14T00:20:11Z", "2026-10-14T01:36:06Z"])

old_caddy = img(["CADDY_VERSION=v2.11.4"], created="2026-06-15T20:35:36.213970238+02:00")
old_caddy["Architecture"] = "amd64"
new_caddy = img(["CADDY_VERSION=v2.11.7"], created="2026-10-18T02:59:10Z")
new_caddy["Architecture"] = "amd64"
HUB_INFO = {"caddy/2": {"tag_last_pushed": "2026-10-16T09:00:00Z",          # a new list of platforms
                        "images": [{"architecture": "arm64", "os": "linux", "last_pushed": "2026-10-16T09:00:00Z"},
                                   {"architecture": "amd64", "os": "linux", "last_pushed": "2026-10-14T00:20:11Z"}]},
            "caddy/2-builder": {"tag_last_pushed": "2026-10-14T00:25:00Z", "images": []}}
hub_asked = []
R.hub_tag = lambda path, tag: (hub_asked.append(f"{path}:{tag}"), HUB_INFO[f"{path.split('/')[-1]}/{tag}"])[1]
R.list_tags = lambda repo: ["2", "2-builder", "2.11.4", "2.11.7", "2.11.7-builder", "latest"]
R.resolve_tag = lambda repo, tag, tags, prev: {"digest": "sha256:x", "version": "2.11.7", "tag": "2.11.7"}
b = R.base_state({"image": "caddy", "tag": "2", "final": True}, old_caddy)
check("base: built in June from 2.11.4, caddy:2 is 2.11.7 since 14 October -> newer",
      (b["version"], b["now"], b["pushed"], b["newer"]), ("2.11.4", "2.11.7", "2026-10-14T00:20:11Z", True))
b = R.base_state({"image": "caddy", "tag": "2", "final": True}, new_caddy)
check("base: rebuilt after the push, same version -> not newer (amd64 counts, not the new list)",
      (b["version"], b["now"], b["newer"]), ("2.11.7", "2.11.7", False))
b = R.base_state({"image": "caddy", "tag": "2-builder", "final": False}, new_caddy)
check("base: a build stage has no version of its own, only the push time counts", ("version" in b, b["newer"]), (False, False))
hub_asked.clear()
check("base: a pinned base never moves and asks nobody",
      (R.base_state({"image": "caddy", "tag": "2", "final": True, "pinned": True}, old_caddy)["newer"], hub_asked), (False, []))

# A Java app built FROM eclipse-temurin:21-jre-jammy (vpn-host, 2026-10-18:
# hibiscus-server). The tag points to 21.0.12.1_1-jre-jammy (the same digest
# on Docker Hub), pushed for amd64 on 17 October; the build carries
# Temurin's JAVA_VERSION.
check("base eclipse-temurin: the version tag behind 21-jre-jammy",
      R.best_tag(["21-jre-jammy", "21.0.12.1_1-jre-jammy"]), "21.0.12.1_1-jre-jammy")
caddy_tags, caddy_now = R.list_tags, R.resolve_tag
HUB_INFO["eclipse-temurin/21-jre-jammy"] = {"tag_last_pushed": "2026-10-17T05:17:35Z", "images": [
    {"architecture": "amd64", "os": "linux", "last_pushed": "2026-10-17T05:17:32Z"}]}
R.list_tags = lambda repo: ["21", "21-jre", "21-jre-jammy", "21.0.12.1_1-jre", "21.0.12.1_1-jre-jammy", "21.0.12.1_1-jdk-jammy"]
R.resolve_tag = lambda repo, tag, tags, prev: {"digest": "sha256:t", "version": "21.0.12.1", "tag": "21.0.12.1_1-jre-jammy"}
hib = img(TEMURIN_ENV + ["JAVA_VERSION=jdk-21.0.12.1+1"], created="2026-10-18T03:10:00Z")
hib["Architecture"] = "amd64"
b = R.base_state({"image": "eclipse-temurin", "tag": "21-jre-jammy", "final": True}, hib)
check("base eclipse-temurin: the build carries 21.0.12.1, the tag points to it -> current",
      (b["version"], b["now"], b["newer"]), ("21.0.12.1", "21.0.12.1", False))
hib["Config"]["Env"] = TEMURIN_ENV + ["JAVA_VERSION=jdk-21.0.8+9"]
b = R.base_state({"image": "eclipse-temurin", "tag": "21-jre-jammy", "final": True}, hib)
check("base eclipse-temurin: built after the push on an older local base (no --pull) -> newer by version",
      (b["version"], b["now"], b["newer"]), ("21.0.8", "21.0.12.1", True))
R.list_tags, R.resolve_tag = caddy_tags, caddy_now
skopeo_asked = []


def fake_inspect(cmd, binary=False):
    skopeo_asked.append(cmd[-1])
    return json.dumps({"Created": "2026-10-10T08:00:00.5Z"})


R.run = fake_inspect
b = R.base_state({"image": "ghcr.io/acme/base", "tag": "1.4.2", "final": False}, new_caddy)
check("base outside Docker Hub: the image's creation time; an exact tag is its own version",
      (b["now"], b["pushed"], b["newer"], skopeo_asked), ("1.4.2", "2026-10-10T08:00:00Z", False, ["docker://ghcr.io/acme/base:1.4.2"]))
R.list_tags = lambda repo: (_ for _ in ()).throw(RuntimeError("registry down"))
b = R.base_state({"image": "caddy", "tag": "2", "final": True}, old_caddy)
check("base: a registry error is kept, nothing is claimed", ("error" in b, "newer" in b), (True, False))

# The radar: an unlisted image built by compose gets "built" and "base".
WORK = tempfile.mkdtemp()
with open(os.path.join(WORK, "Dockerfile"), "w") as f:
    f.write(CADDY)
COMPOSE_LABELS = {"com.docker.compose.project": "caddy", "com.docker.compose.service": "caddy",
                  "com.docker.compose.project.working_dir": WORK,
                  "com.docker.compose.project.config_files": os.path.join(WORK, "docker-compose.yml")}
composed = []


def fake_compose(cmd, binary=False):
    composed.append(cmd)
    return json.dumps({"services": {"caddy": {"build": {"context": WORK, "dockerfile": "Dockerfile"}}}})


R.run = fake_compose
R.list_tags = lambda repo: (_ for _ in ()).throw(R.subprocess.CalledProcessError(1, ["skopeo"], "", DENIED)) \
    if repo == "caddy-caddy" else ["2", "2-builder", "2.11.7", "latest"]
built = R.analyse((("caddy-caddy", "latest"), {"containers": ["caddy"], "labels": COMPOSE_LABELS,
                                                "images": {"idc": old_caddy}}), {}, now)
check("radar: unlisted, built here: build time and both bases",
      (built["status"], built["built"], [(x["image"], x["tag"], x["final"], x.get("newer")) for x in built["base"]]),
      ("unlisted", "2026-06-15T18:35:36Z", [("caddy", "2-builder", False, True), ("caddy", "2", True, True)]))
check("radar: the compose file is read with its project and files",
      composed[-1], ["docker", "compose", "-p", "caddy", "--project-directory", WORK,
                     "-f", os.path.join(WORK, "docker-compose.yml"), "config", "--format", "json"])
plain = R.analyse((("hibiscus-server", "2.12.4"), {"containers": ["hib"], "labels": {}, "images": {"idh": img()}}), {}, now)
check("radar: an unlisted image without compose stays as it was", ("base" in plain, "built" in plain), (False, False))
R.run = lambda cmd, binary=False: json.dumps({"services": {"caddy": {"image": "caddy:2"}}})
check("radar: a compose service that is not built here has no bases",
      "base" in R.analyse((("caddy-caddy", "latest"), {"containers": ["caddy"], "labels": COMPOSE_LABELS,
                                                        "images": {"idc": old_caddy}}), {}, now), False)

print("\nALL CHECKS PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

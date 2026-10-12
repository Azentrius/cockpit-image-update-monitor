#!/usr/bin/env python3
"""Offline tests for diunlib / diun-versions.py / diun-inventory.py
(Docker, skopeo and the Docker Hub API are replaced by fakes built from the
real values seen on a production host on 2026-10-12)."""
import hashlib
import importlib.util
import io
import json
import os
import sys
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

# ---------------------------------------------------------------- analyse
TAGS = {
    "postgres": PG_TAGS,
    "glitchtip/glitchtip": ["6.1", "6.1.8", "6.2.0", "6.2", "7.0.1", "latest"],
    "docker.io/ollama/ollama": ["0.30.11", "0.30.12", "0.31.0", "0.31.0-rc1", "latest"],
    "pgvector/pgvector": ["pg17", "pg18", "0.8.0-pg17", "0.8.1-pg18"],
    "boky/postfix": ["latest"],
}
R.list_tags = lambda repo: TAGS[repo]
hub_calls = []
R.match_digest = lambda repo, digest, created, tags: (hub_calls.append(repo), "0.30.11")[1]
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
check("newest for ollama", ol["newest"], "0.31.0")

# cache: second run must not query again; miss is retried only after 7 days
cache = R.digest_cache([ol])
hub_calls.clear()
R.analyse((("docker.io/ollama/ollama", "latest"), groups[("docker.io/ollama/ollama", "latest")]), cache, now)
check("cached digest result reused", hub_calls, [])
miss = {"ido": {"version": None, "src": "digest", "ts": "2026-10-10T00:00:00Z"}}
R.analyse((("docker.io/ollama/ollama", "latest"), groups[("docker.io/ollama/ollama", "latest")]), miss, now)
check("recent miss not retried", hub_calls, [])
miss = {"ido": {"version": None, "src": "digest", "ts": "2026-09-01T00:00:00Z"}}
R.analyse((("docker.io/ollama/ollama", "latest"), groups[("docker.io/ollama/ollama", "latest")]), miss, now)
check("old miss retried", hub_calls, ["docker.io/ollama/ollama"])

# registry error keeps previous good record, marks it stale
R.list_tags = lambda repo: (_ for _ in ()).throw(RuntimeError("registry down"))
err = R.analyse((("glitchtip/glitchtip", "latest"), groups[("glitchtip/glitchtip", "latest")]), {}, now)
check("error record keeps local version", (err["status"], err["installed"]["idg"]["version"]), ("error", "6.1.8"))
merged = R.keep_last_good([err], [gl])[0]
check("stale merge", (merged.get("stale"), merged["versions"]["6.1.8"]["minor"]), (True, "6.2.0"))

print("\nALL CHECKS PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

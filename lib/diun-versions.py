#!/usr/bin/env python3
"""Versions-Radar for the Cockpit Image-Updates plugin.

Diun only notices when the SAME tag is rebuilt (new digest). This script
answers the questions Diun cannot:

  1. Which version is really installed?   (latest -> 0.30.11, 16-alpine -> 16.14)
  2. Which newer versions exist?          (patch / minor / major)

Installed version, first match wins:
  tag       the tag itself is an exact version (v2.1.4, 24.3.3.102-alpine)
  env       the app's version variable in the image (PG_VERSION=16.14,
            GLITCHTIP_VERSION=6.1.8) - must fit the tag (16-alpine -> 16.x)
  label     org.opencontainers.image.version, but only if that version
            exists as a tag in the registry (ollama inherits "24.04" from
            its Ubuntu base image - that label is rejected)
  digest    which version tag in the registry points to exactly the
            installed image: Docker Hub API (tag list with digests, no pull
            quota) or, for other registries, the manifests of the newest
            version tags (skopeo inspect --raw). Results are cached per
            image id in the output file (positive forever, misses 7 days).

Newer versions are compared in the scheme of the installed version
(prefix + number of numeric blocks + suffix): 16-alpine with 16.14
installed -> 16.15-alpine / 18.0-alpine; -rc / -beta / -jdk variants never
count. "reach" tells which levels the configured tag delivers by itself
(16-alpine: patch + minor; latest: all; v2.1.4: none - change the tag).

Database images are flagged ("db": true): the plugin shows their major
updates as grey info, because a DB major means a migration (dump/restore,
pg_upgrade), never a plain image swap.

Lives outside /var/lib/diun on purpose: that directory is writable from the
Diun container, and anything Cockpit or systemd runs as root must not be.

Usage:
  diun-versions.py                    JSON lines on stdout
  diun-versions.py --table            human-readable table
  diun-versions.py --output FILE      write JSON document atomically
                                      (used by diun-radar.service, every 6h)

Read-only towards Docker and the registries; the only write is --output.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from diunlib import (PLAIN_RE, core, local_version, norm, nums_of,  # noqa: E402
                     qualify, reachable, scheme, split_ref)

DB_RE = re.compile(r"(^|/)(postgres|postgis|pgvector|timescaledb|mariadb|mysql|mongo|"
                   r"redis|valkey|keydb|clickhouse-server|influxdb|couchdb|"
                   r"elasticsearch|opensearch|cassandra)$")
TIMEOUT = 120              # seconds per registry call (postgres has thousands of tags)
WORKERS = 4                # parallel images; keep registries friendly
HUB = "https://hub.docker.com/v2/repositories/"
HUB_PAGES = 10             # x 100 tags, newest first
REGISTRY_CANDIDATES = 20   # manifests checked per image outside Docker Hub
RETRY_MISS = timedelta(days=7)
LEVELS = ("patch", "minor", "major")
ORDER = {"major": 0, "minor": 1, "patch": 2, "current": 3, "unknown": 4, "error": 5}


def run(cmd, binary=False):
    return subprocess.run(cmd, capture_output=True, text=not binary,
                          timeout=TIMEOUT, check=True).stdout


def docker_json(*args):
    return json.loads(run(["docker", *args]))


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    try:
        return datetime.strptime((s or "")[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ------------------------------------------------------------ inventory ----
def collect():
    """{(repo, tag): {"containers": [names], "images": {id: inspect}}} for all
    running containers; "images" also holds the newer local image the tag
    points to when it was pulled but not rolled out yet."""
    ids = run(["docker", "ps", "-q"]).split()
    groups = {}
    if not ids:
        return groups
    containers = docker_json("inspect", *ids)
    tag_to_id = {}
    for line in run(["docker", "image", "ls", "--no-trunc",
                     "--format", "{{.Repository}}:{{.Tag}} {{.ID}}"]).splitlines():
        ref, _, iid = line.rpartition(" ")
        if ref and not ref.endswith(":<none>"):
            tag_to_id[ref] = iid
    wanted = {}
    for c in containers:
        ref = c["Config"]["Image"]
        key = split_ref(ref)
        g = groups.setdefault(key, {"containers": [], "ids": set()})
        g["containers"].append(c["Name"].lstrip("/"))
        g["ids"].add(c["Image"])
        local = "" if "@" in ref else tag_to_id.get(f"{norm(key[0])}:{key[1]}", "")
        if local:
            g["ids"].add(local)
        wanted.update({i: None for i in g["ids"]})
    images = {i["Id"]: i for i in docker_json("image", "inspect", *sorted(wanted))}
    for g in groups.values():
        g["images"] = {i: images[i] for i in sorted(g.pop("ids")) if i in images}
    return groups


# -------------------------------------------------------------- registry ----
def list_tags(repo):
    out = run(["skopeo", "list-tags", "docker://" + qualify(repo)])
    return json.loads(out).get("Tags") or []


def repo_digest(repo, image):
    """Registry digest of a local image for this repo (sha256:...) or ''."""
    want = norm(repo)
    for d in image.get("RepoDigests") or []:
        name, _, digest = d.partition("@")
        if norm(name) == want and digest:
            return digest
    return ""


def best_tag(names):
    """Most specific version tag: 6.1.8 over 6.1, 5.1.0 over v5.1.0-debian."""
    cands = []
    for t in names:
        s = scheme(t)
        if s and s[1] >= 2:
            cands.append(((s[1], s[2] == "", s[0] == ""), t))
    return max(cands)[1] if cands else None


def hub_match(path, digest, created):
    """Docker Hub API: tags with their digests, newest first (no pull quota)."""
    url = f"{HUB}{path}/tags?page_size=100&ordering=last_updated"
    stop_before = (created - timedelta(days=2)) if created else None
    hits = []
    for _ in range(HUB_PAGES):
        req = urllib.request.Request(url, headers={"User-Agent": "diun-radar"})
        with urllib.request.urlopen(req, timeout=30) as r:
            doc = json.load(r)
        oldest = None
        for t in doc.get("results") or []:
            if t.get("digest") == digest:
                hits.append(t.get("name") or "")
            oldest = parse_iso(t.get("last_updated")) or oldest
        if hits:
            break
        if stop_before and oldest and oldest < stop_before:
            break               # older pages cannot point to this image any more
        url = doc.get("next") or ""
        if not url.startswith(HUB):
            break
    return best_tag(hits)


def registry_match(qref, digest, tags):
    """Other registries: compare the manifests of the newest version tags."""
    cands = sorted({t for t in tags if PLAIN_RE.match(t)},
                   key=lambda t: nums_of(PLAIN_RE.match(t).group(1)), reverse=True)
    for t in cands[:REGISTRY_CANDIDATES]:
        raw = run(["skopeo", "inspect", "--raw", f"docker://{qref}:{t}"], binary=True)
        if "sha256:" + hashlib.sha256(raw).hexdigest() == digest:
            return t
    return None


def match_digest(repo, digest, created, tags):
    q = qualify(repo)
    if q.startswith("docker.io/"):
        return hub_match(q[len("docker.io/"):], digest, created)
    return registry_match(q, digest, tags)


# ------------------------------------------------------------ comparison ----
def find_scheme(version, tag, tagset, exact=False):
    """Scheme in which `version` exists as a tag, tried in this order:
    the configured tag's prefix/suffix (16-alpine -> 16.14-alpine),
    for words like 'apache' that word as suffix (34.0.3-apache), then plain
    (6.1.8) and v-prefixed (v6.2.0). exact=False additionally accepts the
    configured tag's own scheme if the registry uses it at all."""
    n = len(version.split("."))
    sc = scheme(tag)
    cands = [(sc[0], n, sc[2])] if sc else [("", n, "-" + tag), ("v", n, "-" + tag)]
    cands += [("", n, ""), ("v", n, "")]
    for p, _, s in cands:
        if p + version + s in tagset:
            return p, n, s
    if exact or not sc:
        return None
    p, _, s = cands[0]
    if any((scheme(t) or ())[:3] == (p, n, s) for t in tagset):
        return p, n, s
    return None


def compare(tag, tags):
    """Newest patch / minor / major in `tags` that follow `tag`'s scheme."""
    prefix, n, suffix, cur = scheme(tag)
    newer = []
    for t in tags:
        s = scheme(t)
        if s and s[:3] == (prefix, n, suffix) and s[3] > cur:
            newer.append((s[3], t))

    def best(pred):
        xs = [x for x in newer if pred(x[0])]
        return max(xs)[1] if xs else None

    res = {
        "patch": best(lambda v: v[:2] == cur[:2]) if n >= 3 else None,
        "minor": best(lambda v: v[0] == cur[0] and v[:2] != cur[:2]) if n >= 2 else None,
        "major": best(lambda v: v[0] != cur[0]),
    }
    res["status"] = ("major" if res["major"] else "minor" if res["minor"]
                     else "patch" if res["patch"] else "current")
    return res


def newest_plain(tags):
    """Highest plain version tag (1.2 / v1.2.3), for display when the
    installed version is unknown."""
    best = None
    for t in tags:
        m = PLAIN_RE.match(t)
        if m:
            k = nums_of(m.group(1))
            if best is None or k > best[0]:
                best = (k, t)
    return best[1] if best else None


# --------------------------------------------------------------- analyse ----
def resolve(repo, tag, iid, image, tagset, tags, cache, now):
    """Installed version of one local image: {"version", "src", ["tag"]}."""
    v, src = local_version(repo, tag, image)
    if v:
        return {"version": v, "src": src}
    label = core(((image.get("Config") or {}).get("Labels") or {}).get("org.opencontainers.image.version"))
    if label and tagset and find_scheme(label, tag, tagset, exact=True):
        return {"version": label, "src": "label"}
    hit = cache.get(iid)
    if hit and hit.get("src") == "digest":
        if hit.get("version") or (now - (parse_iso(hit.get("ts")) or now)) < RETRY_MISS:
            return hit
    digest = repo_digest(repo, image)
    if not digest or not tags:
        return {"version": None, "src": None}
    found = match_digest(repo, digest, parse_iso(image.get("Created")), tags)
    return {"version": core(found) if found else None, "src": "digest", "tag": found, "ts": iso(now)}


def analyse(item, cache, now):
    (repo, tag), group = item
    rec = {"image": repo, "tag": tag, "containers": sorted(group["containers"]),
           "db": bool(DB_RE.search(norm(repo).lower())), "reach": reachable(tag)}
    tags, error = [], None
    try:
        tags = list_tags(repo)
    except subprocess.CalledProcessError as e:
        error = (e.stderr or str(e)).strip()[-300:]
    except Exception as e:  # timeout, bad JSON, ...
        error = str(e)[-300:]
    tagset = set(tags)

    installed = {}
    for iid, image in group["images"].items():
        try:
            installed[iid] = resolve(repo, tag, iid, image, tagset, tags, cache, now)
        except Exception as e:  # Hub API down, manifest error, ...
            installed[iid] = {"version": None, "src": None, "error": str(e)[-200:]}
    rec["installed"] = installed
    if error:
        rec["status"], rec["error"] = "error", error
        return rec

    # Newer versions, relative to every installed version of this tag.
    rec["versions"] = {}
    for v in sorted({x["version"] for x in installed.values() if x.get("version")}):
        sch = find_scheme(v, tag, tagset)
        if not sch:
            rec["versions"][v] = {"status": "unknown"}
            continue
        as_tag = sch[0] + v + sch[2]
        rec["versions"][v] = dict(compare(as_tag, tags), as_tag=as_tag,
                                  scheme=sch[0] + ".".join(["N"] * sch[1]) + sch[2])
    # Relative to the configured tag itself (fallback when the installed
    # version is unknown; pg17 -> pg18).
    if scheme(tag):
        rec["by_tag"] = compare(tag, tags)
    rec["newest"] = newest_plain(tags)
    states = [x["status"] for x in rec["versions"].values()] + \
             ([rec["by_tag"]["status"]] if "by_tag" in rec and not rec["versions"] else [])
    rec["status"] = min(states, key=ORDER.get) if states else "unknown"
    return rec


def load_previous(path):
    try:
        with open(path) as f:
            return json.load(f).get("images", []) or []
    except (OSError, ValueError, AttributeError):
        return []


def digest_cache(prev):
    """{image id: resolved entry} of earlier digest lookups."""
    cache = {}
    for r in prev:
        installed = r.get("installed") if isinstance(r, dict) else None
        for iid, e in (installed or {}).items():
            if isinstance(e, dict) and e.get("src") == "digest":
                cache[iid] = e
    return cache


def keep_last_good(recs, prev):
    """A registry hiccup must not wipe known results: for every record that
    failed this run, reuse the previous good result and mark it stale."""
    prev = {(r.get("image"), r.get("tag")): r for r in prev if isinstance(r, dict)}
    out = []
    for r in recs:
        old = prev.get((r["image"], r["tag"]))
        if r["status"] == "error" and old and old.get("status") not in (None, "error"):
            merged = dict(old)
            merged.update(containers=r["containers"], db=r["db"], reach=r["reach"], stale=True,
                          error=r["error"], checked=old.get("checked"),
                          installed={**(old.get("installed") or {}), **r["installed"]})
            out.append(merged)
        else:
            out.append(r)
    return out


def write_atomic(path, doc):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".versions-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def table(recs):
    head = ("Container", "Image", "Konfiguriert", "Installiert", "Quelle", "Patch", "Minor", "Major", "Status")
    rows = []
    for r in recs:
        inst = [x for x in (r.get("installed") or {}).values()]
        for x in inst or [{"version": None, "src": None}]:
            v = x.get("version")
            cmp = (r.get("versions") or {}).get(v) if v else None
            if not cmp or cmp.get("status") == "unknown":
                cmp = r.get("by_tag") or {}
            status = r["status"] if r["status"] != "error" else "FEHLER: " + r.get("error", "")[:60]
            if r.get("stale"):
                status += " (alt)"
            if r.get("db") and cmp.get("major"):
                status += " [DB]"
            rows.append((", ".join(r["containers"]), r["image"], r["tag"], v or "?",
                         (x.get("src") or "-") + (f" ({x['tag']})" if x.get("tag") else ""),
                         cmp.get("patch") or "-", cmp.get("minor") or "-", cmp.get("major") or "-",
                         status if v or cmp else status + f"; neueste: {r.get('newest') or '-'}"))
    widths = [max(len(str(x[i])) for x in [head] + rows) for i in range(len(head))]
    for row in [head] + rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(row, widths)).rstrip())


def main():
    ap = argparse.ArgumentParser(description="Version radar for running containers")
    ap.add_argument("--table", action="store_true", help="human-readable table")
    ap.add_argument("--output", metavar="FILE", help="write JSON document atomically")
    args = ap.parse_args()

    try:
        groups = collect()
    except Exception as e:
        # Never overwrite good data with an empty list because Docker hiccuped.
        print(f"docker not reachable: {e}", file=sys.stderr)
        return 1

    prev = load_previous(args.output) if args.output else []
    cache = digest_cache(prev)
    now = now_utc()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        recs = list(pool.map(lambda it: analyse(it, cache, now), groups.items()))
    for r in recs:
        r["checked"] = iso(now)
    if args.output:
        recs = keep_last_good(recs, prev)
    recs.sort(key=lambda r: (ORDER.get(r["status"], 9), r["image"]))

    if args.output:
        write_atomic(args.output, {"generated": iso(now), "images": recs})
        errors = sum(1 for r in recs if r["status"] == "error" or r.get("stale"))
        unknown = sum(1 for r in recs for x in (r.get("installed") or {}).values() if not x.get("version"))
        print(f"{len(recs)} images checked, {errors} with registry errors, "
              f"{unknown} without a known version -> {args.output}")
    elif args.table:
        table(recs)
    else:
        for r in recs:
            print(json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

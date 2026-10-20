#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Versions-Radar for Image Update Monitor, the Cockpit add-on.

Diun only notices when the SAME tag is rebuilt (new digest). This script
answers the questions Diun cannot:

  1. Which version is really installed?   (latest -> 0.30.11, 16-alpine -> 16.14)
  2. Which newer versions exist?          (patch / minor / major)

Installed version, first match wins:
  tag       the tag itself is an exact version (v2.1.4, 24.3.3.102-alpine);
            a line tag (v1.6 while the registry has v1.6.74, 24.12-alpine
            next to 24.12.6.70-alpine) names only its line: its number counts
            only when neither label nor digest name the release
  env       the app's version variable in the image (PG_VERSION=16.14,
            GLITCHTIP_VERSION=6.1.8) - must fit the tag (16-alpine -> 16.x)
  label     org.opencontainers.image.version, but only if that version
            exists as a tag in the registry (ollama inherits "24.04" from
            its Ubuntu base image - that label is rejected)
  digest    which version tag in the registry points to exactly the
            installed image: Docker Hub API (tag list with digests, no pull
            quota) or, for other registries, the manifests of the newest
            version tags (skopeo inspect --raw; the first lookup of an image
            goes further back). Results are cached per image id in the
            output file (positive forever, misses 7 days).

What a floating tag (latest, stable, 16-alpine, 5) or a line tag (v1.6)
points to right now is resolved the same way, without a pull: its digest in the registry and the
version tag with that digest ("tag_now", e.g. latest = 6.0.2). Reused while
the digest stays the same. A latest that no version tag shares a digest with
is often a development build: on Docker Hub the radar notes the newest release
it was pushed after (MobSF: latest = a build after v4.5.3).

Newer versions are compared in the scheme of the installed version
(prefix + number of numeric blocks + suffix): 16-alpine with 16.14
installed -> 16.15-alpine / 18.0-alpine; -rc / -beta / -jdk variants never
count. "reach" tells which levels the configured tag delivers by itself
(16-alpine: patch + minor; latest: all; v2.1.4: none - change the tag).
"choices" is the short list a selection field offers instead (pin the
running version, version lines like 5 or 16-alpine, newer exact versions,
latest/stable), never older than what runs.

Database images are flagged ("db": true): the plugin shows their major
updates as grey info, because a DB major means a migration (dump/restore,
pg_upgrade), never a plain image swap.

An image no registry shows gets the status "unlisted" instead of "error": it
was built or tagged on this server (hibiscus-server:2.12.4 means
docker.io/library/hibiscus-server, which does not exist), or it is private
and the radar has no login. There is nothing newer to find for it.

Lives outside /var/lib/diun on purpose: that directory is writable from the
Diun container, and anything Cockpit or systemd runs as root must not be.

Usage:
  diun-versions.py                    JSON lines on stdout
  diun-versions.py --table            human-readable table
  diun-versions.py --output FILE      write JSON document atomically
                                      (used by diun-radar.service, every 6h),
                                      then status.json next to it (diunstatus:
                                      the add-on's status for the Status Board)

Read-only towards Docker and the registries; the only writes are --output
and its status.json.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from diunlib import (PLAIN_RE, core, local_version, norm, nums_of,  # noqa: E402
                     qualify, reachable, scheme, split_ref, tag_digits)
import diunstatus  # noqa: E402

DB_RE = re.compile(r"(^|/)(postgres|postgis|pgvector|timescaledb|mariadb|mysql|mongo|"
                   r"redis|valkey|keydb|clickhouse-server|influxdb|couchdb|"
                   r"elasticsearch|opensearch|cassandra)$")
TIMEOUT = 120              # seconds per registry call (postgres has thousands of tags)
WORKERS = 4                # parallel images; keep registries friendly
HUB = "https://hub.docker.com/v2/repositories/"
HUB_PAGES = 10             # x 100 tags, newest first
REGISTRY_CANDIDATES = 20   # manifests checked per image outside Docker Hub
REGISTRY_CANDIDATES_DEEP = 100   # the first lookup of an installed image goes further back
CHOICES_PATCH = 10         # newer patches of the installed minor line offered for selection
CHOICES_LINE = 15          # newest version per newer minor line, and per newer major
CHOICES_LINES = 12         # version lines (5, 5.4, 16-alpine)
# Docker's reference grammar: path components and tag names. Anything else
# never ends up in a Docker Hub URL.
HUB_PATH_RE = re.compile(r"^[a-z0-9]+(?:[._-]+[a-z0-9]+)*(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)+$")
TAG_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
# What skopeo says when the registry shows no repository by that name. Docker
# Hub answers "requested access to the resource is denied" both for a name that
# does not exist and for a private one, ghcr.io and quay.io "unauthorized",
# other registries "name unknown". Timeouts, rate limits and server errors stay
# registry errors: the next run tries again.
UNLISTED_RE = re.compile(r"requested access to the resource is denied|unauthorized|authentication required"
                         r"|name unknown|repository name not known", re.I)
RETRY_MISS = timedelta(days=7)
BUILD_AFTER_GAP = timedelta(days=1)   # see build_after
BASE_NEWER_GAP = timedelta(hours=1)   # see base_state
DOCKERFILE_MAX = 1 << 20              # Dockerfiles larger than this are not read
L_PROJECT = "com.docker.compose.project"
L_SERVICE = "com.docker.compose.service"
L_WORKDIR = "com.docker.compose.project.working_dir"
L_FILES = "com.docker.compose.project.config_files"
FROM_RE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?\s*$", re.I)
ARG_RE = re.compile(r"^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)(?:=(\S*))?\s*$", re.I)
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
TIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?")
LEVELS = ("patch", "minor", "major")
ORDER = {"major": 0, "minor": 1, "patch": 2, "current": 3, "unknown": 4, "error": 5, "unlisted": 6}


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
        g = groups.setdefault(key, {"containers": [], "ids": set(), "labels": {}})
        g["containers"].append(c["Name"].lstrip("/"))
        labels = c["Config"].get("Labels") or {}
        if not g["labels"] and labels.get(L_PROJECT):
            g["labels"] = labels
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


def registry_match(qref, digest, tags, limit=REGISTRY_CANDIDATES):
    """Other registries: compare the manifests of the newest version tags."""
    cands = sorted({t for t in tags if PLAIN_RE.match(t)},
                   key=lambda t: nums_of(PLAIN_RE.match(t).group(1)), reverse=True)
    for t in cands[:limit]:
        raw = run(["skopeo", "inspect", "--raw", f"docker://{qref}:{t}"], binary=True)
        if "sha256:" + hashlib.sha256(raw).hexdigest() == digest:
            return t
    return None


def hub_path(repo):
    """Repository path on Docker Hub ('library/postgres', 'ollama/ollama'),
    or None for other registries and names that do not follow Docker's
    reference grammar. The registry host is compared as a whole."""
    host, _, path = qualify(repo).partition("/")
    return path if host == "docker.io" and HUB_PATH_RE.match(path) else None


def match_digest(repo, digest, created, tags, limit=REGISTRY_CANDIDATES):
    path = hub_path(repo)
    if path:
        return hub_match(path, digest, created)
    return registry_match(qualify(repo), digest, tags, limit)


def hub_tag(path, tag):
    """One tag on Docker Hub: its digest, when it was pushed, ..."""
    if not TAG_NAME_RE.match(tag):
        raise ValueError(f"not a tag name: {tag!r}")
    req = urllib.request.Request(f"{HUB}{path}/tags/{tag}", headers={"User-Agent": "diun-radar"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def tag_digest(repo, tag):
    """Registry digest a tag points to right now, without a pull: the Docker
    Hub API (no pull quota) or the manifest (skopeo inspect --raw)."""
    if not TAG_NAME_RE.match(tag):
        raise ValueError(f"not a tag name: {tag!r}")
    path = hub_path(repo)
    if path:
        return hub_tag(path, tag).get("digest") or ""
    raw = run(["skopeo", "inspect", "--raw", f"docker://{qualify(repo)}:{tag}"], binary=True)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def build_after(repo, tag, tags):
    """A tag without numbers (latest, main) that no version tag shares a
    digest with: was it pushed after the newest release? MobSF builds latest
    from its development branch, 4.5.4 while v4.5.3 is the newest release.
    Returns {"pushed": <tag>, "after": {"tag": <release>, "pushed": <release>}}
    or None. Only Docker Hub tells when a tag was pushed. A day apart at
    least, so that two builds of one release (latest a few minutes after
    v4.5.3) do not count."""
    path = hub_path(repo)
    release = newest_plain(tags)
    if not path or not release or tag_digits(tag):
        return None

    def pushed(t):
        info = hub_tag(path, t)
        return parse_iso(info.get("tag_last_pushed") or info.get("last_updated"))
    own, rel = pushed(tag), pushed(release)
    if not own or not rel or own - rel < BUILD_AFTER_GAP:
        return None
    return {"pushed": iso(own), "after": {"tag": release, "pushed": iso(rel)}}


def resolve_tag(repo, tag, tags, prev):
    """What a floating tag points to now: {"digest", "version", "tag"}, the
    version from the version tag with the same digest (None if there is
    none). The previous result is reused while the digest stays the same.
    Without a version, build_after is asked again on every run: a new
    release can come out while the tag stays where it is."""
    digest = tag_digest(repo, tag)
    if not digest:
        return None
    if prev and prev.get("digest") == digest:
        res = {k: v for k, v in prev.items() if k not in ("pushed", "after")}
    else:
        found = match_digest(repo, digest, None, tags)
        res = {"digest": digest, "version": core(found) if found else None, "tag": found}
    if not res.get("version"):
        try:
            res.update(build_after(repo, tag, tags) or {})
        except Exception:  # Hub API down: the digest still counts, without the note
            pass
    return res


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


def choices(as_tag, tags, tagset):
    """What a selection field can offer instead of the installed version
    `as_tag` (stage 2), in its scheme and never older than it:
      pin       as_tag itself, if the registry has it (to stop following a
                floating tag), else None
      lines     major and minor lines (5, 5.4, 16-alpine) with the newest
                version tag in each, broad lines first; only lines with a
                released version in them, the running version's own lines
                always
      newer     exact versions with their level: every newer patch of the
                installed minor line, the newest version of each newer minor
                line of the same major, the newest version of each newer
                major; newest first per level
      floating  latest / stable / production (with the flavour suffix, e.g.
                stable-apache), never pre-releases."""
    prefix, n, suffix, cur = scheme(as_tag)
    exact, lines = [], {}
    for t in tags:
        s = scheme(t)
        if not s or s[0] != prefix or s[2] != suffix:
            continue
        if s[1] == n and s[3] > cur:
            exact.append((s[3], t))
        elif s[1] <= min(n - 1, 2) and s[3] >= cur[:s[1]]:
            lines[t] = s[3]     # 26.9.19-alpine next to 26.9.19.9-alpine is no line
    exact.sort(reverse=True)
    newer = [{"tag": t, "level": "patch"} for v, t in exact if v[:2] == cur[:2]][:CHOICES_PATCH]
    for level, line in (("minor", lambda v: v[:2] if v[0] == cur[0] else None),
                        ("major", lambda v: v[:1] if v[0] != cur[0] else None)):
        seen = set()
        for v, t in exact:
            key = line(v) if v[:2] != cur[:2] else None
            if key and key not in seen and len(seen) < CHOICES_LINE:
                seen.add(key)
                newer.append({"tag": t, "level": level})
    known = exact + [(cur, as_tag)]

    def newest_in(nums):
        hits = [x for x in known if x[0][:len(nums)] == nums]
        return max(hits)[1] if hits else None

    # A line without a released version may point to a pre-release (valkey
    # 9.2 before 9.2.0): left out. The running version's lines (5 and 5.0 for
    # 5.0.2) always stay, the others fill up newest first.
    def order(x):
        return scheme(x[2])[3], -len(x[0])

    found = [(v, t, newest_in(v)) for t, v in lines.items()]
    found = [x for x in found if x[2]]
    own = [x for x in found if x[0] == cur[:len(x[0])]]
    rest = sorted((x for x in found if x[0] != cur[:len(x[0])]), key=order, reverse=True)
    keep = sorted(own + rest[:max(CHOICES_LINES - len(own), 0)], key=order, reverse=True)
    line_list = [{"tag": t, "newest": newest} for v, t, newest in keep]
    words = ("latest", "stable", "production")
    flavour = [suffix.lstrip("-")] if suffix.startswith("-") else []
    floating = [w for w in flavour + [w + suffix for w in words] if w in tagset]
    return {"pin": as_tag if as_tag in tagset else None, "lines": line_list, "newer": newer,
            "floating": floating}


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


# --------------------------------------------------------- built images ----
# An image built on this server (compose `build:`) is in no registry, but the
# images its Dockerfile starts FROM are: caddy-caddy is Caddy with a DNS plugin
# built by xcaddy on caddy:2-builder and copied onto caddy:2. When those move
# on after the build, rebuilding brings the new base.
def parse_ts(s):
    """'2026-06-15T20:35:36.21+02:00' / '…Z' / '2026-10-14 01:36:06 +0000 UTC'
    -> aware datetime in UTC, or None."""
    m = TIME_RE.match(s or "")
    if not m:
        return None
    t = datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    z = m.group(3)
    if z and z != "Z":
        sign = 1 if z[0] == "+" else -1
        t -= sign * timedelta(hours=int(z[1:3]), minutes=int(z[-2:]))
    return t


def parse_from(text):
    """The images a Dockerfile starts FROM: [{"image", "tag", "final"[, "pinned"]}].
    "final" marks the base of the image that runs (the last stage, followed
    through stage names). Stage references, scratch and references with
    variables the global ARGs do not fill are left out."""
    args, stage_base, found, last, seen_from = {}, {}, [], None, False
    for line in text.splitlines():
        m = ARG_RE.match(line)
        if m and not seen_from:
            args[m.group(1)] = m.group(2) or ""
            continue
        m = FROM_RE.match(line)
        if not m:
            continue
        seen_from = True
        ref = VAR_RE.sub(lambda v: args.get(v.group(1) or v.group(2), v.group(0)), m.group(1))
        key = ref.lower()
        if key in stage_base:
            entry = stage_base[key]
        elif "$" in ref or key == "scratch":
            entry = None
        else:
            repo, tag = split_ref(ref)
            entry = {"image": repo, "tag": tag, "final": False}
            if "@" in ref:
                entry["pinned"] = True
            found.append(entry)
        if m.group(2):
            stage_base[m.group(2).lower()] = entry
        last = entry
    if last is not None:
        last["final"] = True
    out = []
    for e in found:
        same = [x for x in out if (x["image"], x["tag"]) == (e["image"], e["tag"])]
        if same:
            same[0]["final"] = same[0]["final"] or e["final"]
        else:
            out.append(e)
    return out


def dockerfile_of(labels):
    """Path of the Dockerfile a compose service is built from, or None (not a
    compose service, or not built on this server)."""
    project, service = labels.get(L_PROJECT), labels.get(L_SERVICE)
    workdir, files = labels.get(L_WORKDIR), labels.get(L_FILES)
    if not (project and service and workdir and files):
        return None
    cmd = ["docker", "compose", "-p", project, "--project-directory", workdir]
    for f in files.split(","):
        cmd += ["-f", f]
    cfg = json.loads(run(cmd + ["config", "--format", "json"]))
    build = ((cfg.get("services") or {}).get(service) or {}).get("build")
    if not build:
        return None
    if isinstance(build, str):
        build = {"context": build}
    context = build.get("context") or workdir
    if "://" in context or context.startswith("git@"):
        return None                          # a remote context: nothing to read here
    if not os.path.isabs(context):
        context = os.path.join(workdir, context)
    name = build.get("dockerfile") or "Dockerfile"
    return name if os.path.isabs(name) else os.path.join(context, name)


def base_pushed(repo, tag, image):
    """When the registry got the base tag's current image for this server's
    platform: Docker Hub tells per platform (a new list of platforms alone
    changes nothing); other registries give the image's creation time."""
    path = hub_path(repo)
    if path:
        info = hub_tag(path, tag)
        arch = image.get("Architecture") or "amd64"
        for i in info.get("images") or []:
            if i.get("architecture") == arch and (i.get("os") or "linux") == "linux" and i.get("last_pushed"):
                return parse_ts(i["last_pushed"])
        return parse_ts(info.get("tag_last_pushed") or info.get("last_updated"))
    out = run(["skopeo", "inspect", f"docker://{qualify(repo)}:{tag}"])
    return parse_ts(json.loads(out).get("Created"))


def base_state(entry, image):
    """One base image of a local build: what its tag points to now ("now"),
    the version the build carries ("version", final stage only, from the
    image's variables), when the registry got it ("pushed") and whether it is
    newer than the build. A pinned base (FROM x@sha256:…) never moves."""
    out = dict(entry)
    if entry.get("pinned"):
        out["newer"] = False
        return out
    repo, tag = entry["image"], entry["tag"]
    try:
        if entry.get("final"):
            out["version"] = local_version(repo, tag, image)[0]
        tags = list_tags(repo)
        if tag_digits(tag) < 2:
            out["now"] = (resolve_tag(repo, tag, tags, None) or {}).get("version")
        else:
            out["now"] = core(tag)
        pushed = base_pushed(repo, tag, image)
        out["pushed"] = iso(pushed) if pushed else None
        built = parse_ts(image.get("Created"))
        later = bool(pushed and built and pushed - built > BASE_NEWER_GAP)
        ahead = bool(out.get("version") and out.get("now") and nums_of(out["now"]) > nums_of(out["version"]))
        out["newer"] = later or ahead
    except Exception as e:  # registry or Hub API down, bad JSON, …
        out["error"] = str(e)[-200:]
    return out


def built_from(group):
    """For an image built on this server: (build time, [base_state…]) or None."""
    path = dockerfile_of(group.get("labels") or {})
    if not path:
        return None
    path = os.path.realpath(path)
    if not os.path.isfile(path) or os.path.getsize(path) > DOCKERFILE_MAX:
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        bases = parse_from(f.read())
    if not bases:
        return None
    images = sorted(group["images"].values(), key=lambda i: parse_ts(i.get("Created")) or datetime.min.replace(tzinfo=timezone.utc))
    image = images[-1]
    built = parse_ts(image.get("Created"))
    return (iso(built) if built else None), [base_state(b, image) for b in bases]


# --------------------------------------------------------------- analyse ----
def line_tag(tag, tags):
    """A tag with version numbers that names a line, not a release: the
    registry has longer versions of the same scheme under it (v1.6 ->
    v1.6.74, 10.0 -> 10.0.0.1, 24.12-alpine -> 24.12.6.70-alpine). An exact
    tag has none (16.14-alpine, v2.1.4); tags with fewer than two numbers
    (latest, 16-alpine) float anyway."""
    sc = scheme(tag)
    if not sc or sc[1] < 2:
        return False
    for t in tags:
        x = scheme(t)
        if x and x[0] == sc[0] and x[2] == sc[2] and x[1] > sc[1] and x[3][:sc[1]] == sc[3]:
            return True
    return False


def resolve(repo, tag, iid, image, tagset, tags, cache, now, line=False):
    """Installed version of one local image: {"version", "src", ["tag"]}.
    line: the configured tag is a line tag (line_tag). Its number then counts
    only when neither the label nor the digest names the release: as
    {"version", "src": "tag", "digest_ts"}, where digest_ts keeps the miss
    for the cache."""
    v, src = local_version(repo, tag, image)
    if v and not (line and src == "tag"):
        return {"version": v, "src": src}
    label = core(((image.get("Config") or {}).get("Labels") or {}).get("org.opencontainers.image.version"))
    if label and tagset and find_scheme(label, tag, tagset, exact=True):
        return {"version": label, "src": "label"}
    res = by_digest(repo, iid, image, tags, cache, now)
    if v and not res.get("version"):
        return {"version": v, "src": "tag", "digest_ts": res.get("ts")}
    return res


def by_digest(repo, iid, image, tags, cache, now):
    """Which version tag points to exactly this image (src "digest"),
    cached per image id: hits for good, misses for RETRY_MISS."""
    hit = cache.get(iid)
    if hit and hit.get("src") == "digest":
        if hit.get("version") or (now - (parse_iso(hit.get("ts")) or now)) < RETRY_MISS:
            return hit
    digest = repo_digest(repo, image)
    if not digest or not tags:
        return {"version": None, "src": None}
    # The first lookup of an image goes further back; a retried miss only
    # needs the newest tags (an old image does not get new version tags).
    limit = REGISTRY_CANDIDATES if hit else REGISTRY_CANDIDATES_DEEP
    found = match_digest(repo, digest, parse_iso(image.get("Created")), tags, limit)
    return {"version": core(found) if found else None, "src": "digest", "tag": found, "ts": iso(now)}


def analyse(item, cache, now, tag_cache=None):
    (repo, tag), group = item
    rec = {"image": repo, "tag": tag, "containers": sorted(group["containers"]),
           "db": bool(DB_RE.search(norm(repo).lower())), "reach": reachable(tag)}
    tags, error, unlisted = [], None, False
    try:
        tags = list_tags(repo)
    except subprocess.CalledProcessError as e:
        error = (e.stderr or str(e)).strip()[-300:]
        unlisted = bool(UNLISTED_RE.search(e.stderr or ""))
    except Exception as e:  # timeout, bad JSON, ...
        error = str(e)[-300:]
    tagset = set(tags)

    line = line_tag(tag, tags)
    installed = {}
    for iid, image in group["images"].items():
        try:
            installed[iid] = resolve(repo, tag, iid, image, tagset, tags, cache, now, line)
        except Exception as e:  # Hub API down, manifest error, ...
            installed[iid] = {"version": None, "src": None, "error": str(e)[-200:]}
    rec["installed"] = installed
    if error:
        rec["status"], rec["error"] = "unlisted" if unlisted else "error", error
        if unlisted:
            try:
                found = built_from(group)
            except Exception:  # compose or the Dockerfile unreadable: the record as it was
                found = None
            if found:
                rec["built"], rec["base"] = found
        return rec

    # A floating tag or a line tag: which version it points to right now.
    if tag_digits(tag) < 2 or line:
        try:
            now_ = resolve_tag(repo, tag, tags, (tag_cache or {}).get((repo, tag)))
        except Exception:  # Hub API down, manifest error: no tag_now this run
            now_ = None
        if now_:
            rec["tag_now"] = now_

    # Newer versions, relative to every installed version of this tag.
    rec["versions"] = {}
    for v in sorted({x["version"] for x in installed.values() if x.get("version")}):
        sch = find_scheme(v, tag, tagset)
        if not sch:
            rec["versions"][v] = {"status": "unknown"}
            continue
        as_tag = sch[0] + v + sch[2]
        rec["versions"][v] = dict(compare(as_tag, tags), as_tag=as_tag,
                                  scheme=sch[0] + ".".join(["N"] * sch[1]) + sch[2],
                                  choices=choices(as_tag, tags, tagset))
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
    """{image id: resolved entry} of earlier digest lookups; the miss of a
    line tag that fell back to its number (digest_ts) counts as a miss."""
    cache = {}
    for r in prev:
        installed = r.get("installed") if isinstance(r, dict) else None
        for iid, e in (installed or {}).items():
            if isinstance(e, dict) and e.get("src") == "digest":
                cache[iid] = e
            elif isinstance(e, dict) and e.get("digest_ts"):
                cache[iid] = {"version": None, "src": "digest", "ts": e["digest_ts"]}
    return cache


def tag_now_cache(prev):
    """{(image, tag): tag_now} of the previous run."""
    return {(r.get("image"), r.get("tag")): r["tag_now"] for r in prev
            if isinstance(r, dict) and isinstance(r.get("tag_now"), dict)}


def kept_installed(old, new):
    """`installed` of a record whose registry failed this run: the new entry
    per image ID, except where it only guessed from the tag. Without the tag
    list a line tag (v1.6) cannot be told from an exact one, so its number
    (1.6) stands in for the release; the previous run's entry for the same
    image ID wins then when it names that release (1.6.74) or the same
    number. An entry from the label, a variable or the digest cache stays,
    and nothing moves to another image ID."""
    out = dict(old or {})
    for iid, e in (new or {}).items():
        o = out.get(iid)
        guess = isinstance(e, dict) and e.get("src") == "tag" and e.get("version")
        ov = o.get("version") if isinstance(o, dict) else None
        if guess and ov and (ov == e["version"] or ov.startswith(e["version"] + ".")):
            continue
        out[iid] = e
    return out


def keep_last_good(recs, prev):
    """A registry hiccup must not wipe known results: for every record that
    failed this run, reuse the previous good result and mark it stale. That
    holds for a repository the registry suddenly no longer shows, too."""
    prev = {(r.get("image"), r.get("tag")): r for r in prev if isinstance(r, dict)}
    out = []
    for r in recs:
        old = prev.get((r["image"], r["tag"]))
        failed = ("error", "unlisted")
        if r["status"] in failed and old and old.get("status") not in (None, *failed):
            merged = dict(old)
            merged.update(containers=r["containers"], db=r["db"], reach=r["reach"], stale=True,
                          error=r["error"], checked=old.get("checked"),
                          installed=kept_installed(old.get("installed"), r["installed"]))
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
            tn = (r.get("tag_now") or {}).get("version")
            after = ((r.get("tag_now") or {}).get("after") or {}).get("tag")
            note = f" (={tn})" if tn else f" (>{after})" if after else ""
            rows.append((", ".join(r["containers"]), r["image"], r["tag"] + note, v or "?",
                         (x.get("src") or "-") + (f" ({x['tag']})" if x.get("tag") else ""),
                         cmp.get("patch") or "-", cmp.get("minor") or "-", cmp.get("major") or "-",
                         status if v or cmp else status + f"; neueste: {r.get('newest') or '-'}"))
    widths = [max(len(str(x[i])) for x in [head] + rows) for i in range(len(head))]
    for row in [head] + rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(row, widths)).rstrip())


RETRY_PAUSE = 30            # seconds before the one retry of a run that reached no registry


def reached_none(recs):
    """No registry answered this run: every image ended in a registry error.
    That is the server's DNS or network, not the registries (build-host,
    2026-10-20: the resolver answered "server misbehaving" for every name).
    "unlisted" is an answer (access denied), so it counts as reached."""
    return bool(recs) and all(r.get("status") == "error" for r in recs)


def common_error(recs):
    """The most frequent error of a run, shortened."""
    errs = [str(r.get("error") or "") for r in recs if r.get("error")]
    return max(sorted(set(errs)), key=errs.count)[-300:] if errs else ""


def check_all(groups, cache, tags_now, now=None, sleep=None, clock=None):
    """analyse() for every image. A run that reached no registry is tried once
    more after RETRY_PAUSE seconds. Returns (records, time of the run,
    no_registry): no_registry is {"error", "images"} when the second try
    reached none either, else None."""
    clock = clock or now_utc

    def run(at):
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            return list(pool.map(lambda it: analyse(it, cache, at, tags_now), groups.items()))
    now = now or clock()
    recs = run(now)
    if reached_none(recs):
        print(f"no registry reached ({common_error(recs)}); trying again in {RETRY_PAUSE} s", file=sys.stderr)
        (sleep or time.sleep)(RETRY_PAUSE)
        now = clock()
        recs = run(now)
    return recs, now, ({"error": common_error(recs), "images": len(recs)} if reached_none(recs) else None)


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
    cache, tags_now = digest_cache(prev), tag_now_cache(prev)
    recs, now, no_registry = check_all(groups, cache, tags_now)
    for r in recs:
        r["checked"] = iso(now)
    if args.output:
        recs = keep_last_good(recs, prev)
    recs.sort(key=lambda r: (ORDER.get(r["status"], 9), r["image"]))

    if args.output:
        doc = {"generated": iso(now), "images": recs}
        if no_registry:
            doc["no_registry"] = no_registry
        write_atomic(args.output, doc)
        errors = sum(1 for r in recs if r["status"] == "error" or r.get("stale"))
        unlisted = sum(1 for r in recs if r["status"] == "unlisted")
        unknown = sum(1 for r in recs for x in (r.get("installed") or {}).values() if not x.get("version"))
        print(f"{len(recs)} images checked, {errors} with registry errors, {unlisted} not in a registry, "
              f"{unknown} without a known version -> {args.output}")
        if no_registry:
            print(f"no registry reached, also on the second try: {no_registry['error']}")
        # The status for the Status Board; a problem there never fails the run.
        status = os.path.join(os.path.dirname(os.path.abspath(args.output)), "status.json")
        try:
            print(f"status: {diunstatus.refresh(status, versions=doc)['summary']} -> {status}")
        except Exception as e:
            print(f"status not written: {e}", file=sys.stderr)
    elif args.table:
        table(recs)
    else:
        for r in recs:
            print(json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

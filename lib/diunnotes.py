# SPDX-License-Identifier: Apache-2.0
"""Release notes for Image Update Monitor (stage 3): where an image's source
is on GitHub, and its releases between the installed and the newest version.

The version radar (diun-versions.py --output) uses this module: source_of()
names the source of every image in versions.json ("source"), refresh() fetches
the releases of the images that have something newer and writes them to
releases.json next to versions.json. The page reads that file when an update
or a version change dialog opens; the browser never talks to GitHub.

Source of an image, first match wins:
  known   KNOWN below: images that name no source (most images on Docker Hub)
          or name the repository of their Dockerfile, not of the program
  label   org.opencontainers.image.source or org.label-schema.vcs-url, when it
          points to github.com (https://github.com/cube-js/cube.git)
  ghcr    ghcr.io/<owner>/<name>: the owner is a GitHub account, the name is
          taken as the repository (ghcr.io/formbricks/formbricks)

GitHub allows 60 requests per hour without a login. One request per source
(the newest 100 releases), at most MAX_REQUESTS per run, none below RESERVE
left, and an answer is kept: a source is asked again after TTL, or after
RECHECK when the radar knows a version newer than the last answer had, or an
installed version older than its oldest. A rate limit (429, or 403 with none
left or Retry-After) or a network error stops the run's requests; the last
answer stays, marked with its time.

No side effects on import. Reads nothing but the image labels and GitHub."""

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from diunlib import core, norm

API = "https://api.github.com/repos/"
PER_PAGE = 100             # releases per request, the newest first
TIMEOUT = 20               # seconds per request
MAX_REQUESTS = 20          # per radar run
RESERVE = 5                # requests left for anything else on this address
TTL = timedelta(hours=24)  # an answer is asked again after this
RECHECK = timedelta(hours=1)   # earliest new request, whatever changed
KEEP = 25                  # releases kept per source, the newest first
BODY_MAX = 8000            # characters of a release's text kept
LEVELS = ("patch", "minor", "major")

# Images whose GitHub repository holds the releases of the program they run.
# Keys as norm() writes them, lower case. Each one checked against the
# repository's release tags (the image's versions, with or without v).
KNOWN = {
    "actualbudget/actual-server": "actualbudget/actual",
    "boky/postfix": "bokysan/docker-postfix",
    "caddy": "caddyserver/caddy",
    "clickhouse/clickhouse-server": "ClickHouse/ClickHouse",
    "defectdojo/defectdojo-django": "DefectDojo/django-DefectDojo",
    "defectdojo/defectdojo-nginx": "DefectDojo/django-DefectDojo",
    "docuseal/docuseal": "docusealco/docuseal",
    "ghcr.io/home-assistant/home-assistant": "home-assistant/core",
    "ghcr.io/immich-app/immich-server": "immich-app/immich",
    "gitea/gitea": "go-gitea/gitea",
    "grafana/grafana": "grafana/grafana",
    "homeassistant/home-assistant": "home-assistant/core",
    "louislam/uptime-kuma": "louislam/uptime-kuma",
    "n8nio/n8n": "n8n-io/n8n",
    "nextcloud": "nextcloud/server",
    "nginx": "nginx/nginx",
    "ollama/ollama": "ollama/ollama",
    "onlyoffice/documentserver": "ONLYOFFICE/DocumentServer",
    "paperlessngx/paperless-ngx": "paperless-ngx/paperless-ngx",
    "portainer/portainer-ce": "portainer/portainer",
    "redis": "redis/redis",
    "sakowicz/actual-ai": "sakowicz/actual-ai",
    "traefik": "traefik/traefik",
    "vaultwarden/server": "dani-garcia/vaultwarden",
}
SOURCE_LABELS = ("org.opencontainers.image.source", "org.label-schema.vcs-url")
# GitHub's names: an account (letters, digits, single hyphens, at most 39) and
# a repository (letters, digits, . _ -, never . or ..). Nothing else ends up in
# a request.
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,99}$")
GITHUB_URL_RE = re.compile(r"^(?:https?://|git@)?(?:www\.)?github\.com[/:]([^/\s]+)/([^/#?\s]+?)(?:\.git)?(?:[/#?].*)?$",
                           re.I)
# A release tag: anything before the version (v, release-, n8n@), the version
# with two blocks at least, anything after it (-stable, -lts).
NOTE_TAG_RE = re.compile(r"^(?P<prefix>.*?)v?(?P<ver>[0-9]+(?:\.[0-9]+)+)(?P<suffix>.*)$")
PRE_RE = re.compile(r"alpha|beta|rc|pre|preview|dev|nightly|snapshot|canary|^[.-]?[ab][0-9]", re.I)


def repo_name(owner, name):
    """'owner/name' when both are valid GitHub names, else None."""
    if OWNER_RE.match(owner or "") and NAME_RE.match(name or "") and name not in (".", ".."):
        return f"{owner}/{name}"
    return None


def from_url(url):
    """'owner/name' of a github.com URL (https, git@, with .git or a path), or None."""
    m = GITHUB_URL_RE.match((url or "").strip())
    return repo_name(m.group(1), m.group(2)) if m else None


def source_of(repo, images):
    """{"repo": "owner/name", "via": "known|label|ghcr"} or None. images: the
    `docker image inspect` dicts of the tag (installed and pulled)."""
    key = norm(repo).lower()
    if key in KNOWN:
        return {"repo": KNOWN[key], "via": "known"}
    for image in images or ():
        labels = ((image or {}).get("Config") or {}).get("Labels") or {}
        for name in SOURCE_LABELS:
            found = from_url(labels.get(name))
            if found:
                return {"repo": found, "via": "label"}
    parts = norm(repo).split("/")
    if len(parts) == 3 and parts[0].lower() == "ghcr.io":
        found = repo_name(parts[1], parts[2])
        if found:
            return {"repo": found, "via": "ghcr"}
    return None


# ------------------------------------------------------------- versions ----
def vkey(version):
    """Numbers of a version without trailing zeros: 16.14 and 16.14.0 are the
    same release; None for anything else."""
    try:
        nums = [int(x) for x in str(version).split(".")]
    except ValueError:
        return None
    while len(nums) > 1 and nums[-1] == 0:
        nums.pop()
    return tuple(nums)


def wanted(rec):
    """(installed versions, newer versions) of a versions.json record, as
    version strings: what an update or a version change can go from and to."""
    installed = {x.get("version") for x in (rec.get("installed") or {}).values()
                 if isinstance(x, dict) and x.get("version")}
    newer = set()
    for cmp in (rec.get("versions") or {}).values():
        if not isinstance(cmp, dict):
            continue
        newer.update(core(cmp.get(level)) for level in LEVELS)
        newer.update(core((c or {}).get("tag")) for c in ((cmp.get("choices") or {}).get("newer") or []))
    newer.add((rec.get("tag_now") or {}).get("version"))
    return {v for v in installed if vkey(v)}, {v for v in newer if v and vkey(v)}


def demands(recs):
    """{repo: {"floor", "top", "keys"}} for the records with something newer:
    the oldest installed and the newest known version, and every version of
    interest (to choose the right releases of a repository with several)."""
    out = {}
    for r in recs:
        src = r.get("source") if isinstance(r, dict) else None
        if not isinstance(src, dict) or not src.get("repo") or r.get("status") not in LEVELS:
            continue
        installed, newer = wanted(r)
        if not installed or not newer:
            continue
        d = out.setdefault(src["repo"], {"floor": None, "top": None, "keys": set()})
        low, high = min(installed, key=vkey), max(newer, key=vkey)
        if d["floor"] is None or vkey(low) < vkey(d["floor"]):
            d["floor"] = low
        if d["top"] is None or vkey(high) > vkey(d["top"]):
            d["top"] = high
        d["keys"].update(vkey(v) for v in installed | newer)
    return out


def parse_releases(items):
    """The releases of an answer with a version in their tag, without drafts
    and pre-releases: [{"tag", "version", "prefix", ...}]."""
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or it.get("draft") or it.get("prerelease"):
            continue
        tag = str(it.get("tag_name") or "")
        m = NOTE_TAG_RE.match(tag)
        if not m or PRE_RE.search(m.group("suffix")):
            continue
        body = str(it.get("body") or "")
        url = str(it.get("html_url") or "")
        rel = {"tag": tag, "version": m.group("ver"), "prefix": m.group("prefix"),
               "published": it.get("published_at") or it.get("created_at"),
               "url": url if url.startswith("https://github.com/") else None,
               "body": body[:BODY_MAX]}
        name = str(it.get("name") or "").strip()
        if name and name != tag:
            rel["name"] = name[:200]
        if len(body) > BODY_MAX:
            rel["cut"] = True
        out.append(rel)
    return out


def choose(releases, keys, floor):
    """The releases of the image: in a repository with several kinds of tags
    (v1.2.3 next to app-1.2.3) the kind whose versions the image has (else the
    one with the most releases); newer than the oldest installed version, the
    newest first, KEEP of them. Returns (releases, more): more when older ones
    in range were left out."""
    groups = {}
    for r in releases:
        groups.setdefault(r["prefix"], []).append(r)
    if not groups:
        return [], False
    best = max(groups.values(), key=lambda g: (sum(1 for r in g if vkey(r["version"]) in keys), len(g)))
    low = vkey(floor)
    newer = sorted((r for r in best if vkey(r["version"]) > low), key=lambda r: vkey(r["version"]), reverse=True)
    out = [{k: v for k, v in r.items() if k != "prefix" and v is not None} for r in newer[:KEEP]]
    return out, len(newer) > KEEP


# ---------------------------------------------------------------- GitHub ----
class Limited(Exception):
    """GitHub's rate limit: no more requests this run."""


def fetch(repo):
    """The newest releases of a repository: (list, rate limit left or None).
    HTTPError 404: no such public repository."""
    req = urllib.request.Request(f"{API}{repo}/releases?per_page={PER_PAGE}",
                                 headers={"User-Agent": "diun-radar", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r), left_of(r.headers)


def left_of(headers):
    try:
        return int((headers or {}).get("X-RateLimit-Remaining"))
    except (TypeError, ValueError):
        return None


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    try:
        return datetime.strptime((s or "")[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def due(old, need, now):
    """Whether a source is asked again: never asked; not within RECHECK of the
    last try; after TTL; or sooner when the last try failed, or the radar now
    knows a newer version or an older installed one than that answer had."""
    if not isinstance(old, dict):
        return True
    tried = parse_iso(old.get("tried")) or parse_iso(old.get("checked"))
    if tried and now - tried < RECHECK:
        return False
    checked = parse_iso(old.get("checked"))
    if not checked or now - checked >= TTL or old.get("status") not in ("ok", "missing"):
        return True
    if old.get("status") == "missing":
        return False
    before_floor, before_top = vkey(old.get("floor")), vkey(old.get("top"))
    return (not before_floor or vkey(need["floor"]) < before_floor
            or not before_top or vkey(need["top"]) > before_top)


def refresh(recs, prev, now, get=None):
    """releases.json for the records of a radar run: {"generated", "repos":
    {repo: entry}}, entry {"status": "ok|missing|error|limited", "checked",
    "tried", "floor", "top", "more", "releases", "error"}. prev: the last
    releases.json (or {}). Sources without anything newer are left out."""
    get = get or fetch
    old_repos = (prev or {}).get("repos") if isinstance(prev, dict) else None
    old_repos = old_repos if isinstance(old_repos, dict) else {}
    repos, sent, stop, left = {}, 0, None, None
    for repo, need in sorted(demands(recs).items()):
        old = old_repos.get(repo)
        entry = dict(old) if isinstance(old, dict) else {}
        if not due(old, need, now):
            repos[repo] = entry
            continue
        if stop or sent >= MAX_REQUESTS or (left is not None and left <= RESERVE):
            if entry:
                repos[repo] = entry            # asked on a later run
            continue
        sent += 1
        entry["tried"] = iso(now)
        try:
            items, left = get(repo)
        except urllib.error.HTTPError as e:
            left = left_of(e.headers)
            if e.code == 404:
                entry = {"status": "missing", "checked": iso(now), "tried": iso(now)}
            elif e.code == 429 or (e.code == 403 and (left == 0 or (e.headers or {}).get("Retry-After"))):
                stop = "limited"
                entry.update(status="limited", error=f"HTTP {e.code}")
            else:
                entry.update(status="error", error=f"HTTP {e.code}")
            repos[repo] = entry
            continue
        except Exception as e:  # network down, timeout, bad JSON: no further requests this run
            stop = "error"
            entry.update(status="error", error=str(e)[-200:])
            repos[repo] = entry
            continue
        releases, more = choose(parse_releases(items), need["keys"], need["floor"])
        full = isinstance(items, list) and len(items) >= PER_PAGE
        entry = {"status": "ok", "checked": iso(now), "tried": iso(now), "floor": need["floor"],
                 "top": need["top"], "releases": releases,
                 "more": more or (full and bool(releases) and vkey(releases[-1]["version"]) > vkey(need["floor"]))}
        repos[repo] = entry
    return {"generated": iso(now), "repos": repos}


def load(path):
    try:
        with open(path) as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def summary(doc):
    """One line for the radar's log: how many sources, releases and problems."""
    repos = (doc or {}).get("repos") or {}
    n = sum(len(e.get("releases") or []) for e in repos.values())
    bad = sorted({e.get("status") for e in repos.values()} - {"ok", None})
    return f"{len(repos)} sources, {n} releases" + (f" ({', '.join(bad)})" if bad else "")

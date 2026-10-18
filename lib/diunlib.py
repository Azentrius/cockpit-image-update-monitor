# SPDX-License-Identifier: Apache-2.0
"""Shared helpers of Image Update Monitor (inventory + version radar).

Lives in /usr/lib/cockpit-image-updates next to the scripts that import it
(root-owned, not mounted into any container). No side effects on import.

Version vocabulary used everywhere:
  configured tag   what the compose file / docker run says: latest, 16-alpine, v2.1.4
  version (core)   dotted numbers only, at least two blocks: 6.1.8, 16.14, 0.30.11
  scheme           prefix + number of numeric blocks + suffix of a tag:
                   v2.1.4 -> ("v", 3, ""), 16.14-alpine -> ("", 2, "-alpine")
"""

import re

TAG_RE = re.compile(r"^(?P<prefix>[^0-9]*)(?P<ver>[0-9]+(?:\.[0-9]+)*)(?P<suffix>.*)$")
CORE_RE = re.compile(r"^(?:[0-9]+:|jdk-)?v?([0-9]+(?:\.[0-9]+)+)")   # >= 2 numeric blocks, Debian epoch "1:" and "jdk-" skipped
PLAIN_RE = re.compile(r"^v?([0-9]+(?:\.[0-9]+){1,3})$")     # 1.2 / v1.2.3 / 1.2.3.4

# Images whose version variable is not called <NAME>_VERSION.
PG_FAMILY = ("postgres", "postgresql", "postgis", "pgvector", "timescaledb", "timescaledb-ha")
ENV_ALIASES = {name: ("PG_VERSION",) for name in PG_FAMILY}
ENV_ALIASES.update({
    "mariadb": ("MARIADB_VERSION",),
    "mysql": ("MYSQL_VERSION",),
    "percona-server": ("PS_VERSION", "MYSQL_VERSION"),
    "eclipse-temurin": ("JAVA_VERSION",),
})


def split_ref(ref):
    """'reg:5000/a/b:tag@sha256:..' -> ('reg:5000/a/b', 'tag'); no tag -> 'latest'."""
    ref = ref.split("@", 1)[0]
    if ":" in ref.rsplit("/", 1)[-1]:          # colon in last path part = tag
        repo, tag = ref.rsplit(":", 1)         # (registry:5000/x keeps its port)
        return repo, tag
    return ref, "latest"


def norm(repo):
    """docker.io/library/postgres -> postgres (how `docker images` lists it)."""
    for p in ("docker.io/", "index.docker.io/"):
        if repo.startswith(p):
            repo = repo[len(p):]
    if repo.startswith("library/"):
        repo = repo[len("library/"):]
    return repo


def qualify(repo):
    """Fully qualified name for skopeo (avoids short-name resolution)."""
    repo = norm(repo)
    first = repo.split("/", 1)[0]
    if "/" in repo and ("." in first or ":" in first or first == "localhost"):
        return repo
    if "/" not in repo:
        repo = "library/" + repo
    return "docker.io/" + repo


def scheme(tag):
    """(prefix, number of blocks, suffix, numbers) or None for 'latest', 'apache' ..."""
    m = TAG_RE.match(tag or "")
    if not m:
        return None
    nums = tuple(int(x) for x in m["ver"].split("."))
    return m["prefix"], len(nums), m["suffix"], nums


def nums_of(version):
    return tuple(int(x) for x in version.split("."))


def core(value):
    """'v6.2.0' -> '6.2.0', '17.10-1.pgdg12+1' -> '17.10', 'master' / '16' -> None.
    The official mariadb image names its package version with the Debian
    epoch: MARIADB_VERSION=1:11.8.9+maria~ubu2404 -> '11.8.9'. Eclipse
    Temurin names its release, in the JRE images too, with "jdk-" and the
    build: JAVA_VERSION=jdk-21.0.12.1+1 -> '21.0.12.1', as the tag
    21.0.12.1_1-jre-jammy reads."""
    m = CORE_RE.match((value or "").strip())
    return m.group(1) if m else None


def env_names(repo):
    """Candidate variable names that carry the APP version of this image
    (never NODE_VERSION & co. of the runtime underneath)."""
    parts = norm(repo).lower().split("/")
    names = list(ENV_ALIASES.get(parts[-1], ()))
    for p in (parts[-1], parts[-2] if len(parts) > 1 else ""):
        if p:
            names.append(re.sub(r"[^a-z0-9]", "_", p).upper() + "_VERSION")
    out = []
    for n in names:
        if n not in out:
            out.append(n)
    return out


def local_version(repo, tag, image):
    """Exact version an image carries, from local data only (no registry).

    image = `docker image inspect` dict. Returns (version, source) with
    source "tag" or "env:NAME", or (None, None).
      v2.1.4 / 24.3.3.102-alpine   -> the tag itself is exact
      16-alpine + PG_VERSION=16.14 -> 16.14 (variable must fit the tag: 16.x)
      latest + GLITCHTIP_VERSION   -> 6.1.8
    """
    sc = scheme(tag)
    tag_nums = sc[3] if sc else ()
    env = {}
    for e in ((image or {}).get("Config") or {}).get("Env") or []:
        k, _, v = e.partition("=")
        env[k] = v
    for name in env_names(repo):
        v = core(env.get(name))
        if not v:
            continue
        n = nums_of(v)
        if tag_nums and n[:len(tag_nums)] != tag_nums:
            continue                    # variable belongs to something else
        if len(n) > len(tag_nums):
            return v, "env:" + name
    if sc and sc[1] >= 2:
        return ".".join(str(x) for x in tag_nums), "tag"
    return None, None


def tag_digits(tag):
    """How many leading version blocks the configured tag fixes: latest -> 0,
    16-alpine -> 1, 3.12-slim -> 2, v2.1.4 -> 3."""
    sc = scheme(tag)
    return sc[1] if sc else 0


def reachable(tag):
    """Which update levels the configured tag delivers by itself (a plain
    pull of the same tag) - the rest needs a changed tag in the compose file."""
    k = tag_digits(tag)
    return [lvl for lvl, need in (("patch", 2), ("minor", 1), ("major", 0)) if k <= need]


# Containers of CI jobs: the Forgejo runner and Gitea's act_runner name them
# FORGEJO-ACTIONS-TASK-<n>_WORKFLOW-<id>_JOB-<job> (a job's service containers
# carry the same prefix) and mount a volume at /var/run/act. They live as long
# as the job, belong to no compose project and cannot be updated.
CI_JOB_RE = re.compile(r"^(FORGEJO|GITEA)-ACTIONS-TASK-[0-9]+_", re.I)
# Images of the runners that start them: updating one ends its running jobs.
CI_RUNNER_RE = re.compile(r"(^|/)(forgejo/runner|gitea/act_runner)$")


def is_ci_job(c):
    """c: `docker inspect` of a container."""
    if ((c.get("Config") or {}).get("Labels") or {}).get("com.docker.compose.project"):
        return False
    if CI_JOB_RE.match((c.get("Name") or "").lstrip("/")):
        return True
    return any((m or {}).get("Destination") == "/var/run/act" for m in c.get("Mounts") or [])

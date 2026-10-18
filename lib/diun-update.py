#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Update / rollback / backup helper for Image Update Monitor, the Cockpit add-on.

Called by the plugin via cockpit.spawn with a FIXED set of commands only:

  plan <container>              read-only: what an update would touch (JSON),
                                the version it brings (radar) and the size of
                                the jump, or that it is unknown
  update <container> [--no-dump]
                                1. dump every supported database container of
                                   the compose project (pg_dumpall / mariadb-dump)
                                2. tag the running image as <repo>:rollback-<stamp>
                                3. docker compose pull + up -d --no-deps for every
                                   service of the project that uses the same image
                                   and was started from the same compose files
                                   (a failed pull that leaves the old image
                                   stops here, exit 5)
                                4. wait until the new containers run (healthy),
                                   then watch the services that depend on them;
                                   if anything fails, name the most likely cause
                                5. clean up this project by the rules (see cleanup)
                                6. start the version radar in the background
  rollback <op-id>              point the tag back at the rollback image and
                                recreate the same services (data is NOT restored)
  log <op-id>                   read-only: the kept output of that update (and
                                of its rollback)
  backups                       read-only: dumps, rollback images, history and
                                what the cleanup rules would delete (JSON)
  delete-dump <project/file>    delete one dump file
  delete-image <repo:rollback-…> remove one rollback image tag
  cleanup [--dry-run]           delete old dumps and unused rollback images by
                                the rules in /etc/image-updates.conf (also run
                                daily by diun-cleanup.timer)
  radar                         run the version radar now and wait for it
  retag <container> <tag> --dry-run
                                read-only: the plan of a tag change (stage 2):
                                what the radar offers the tag as, the version
                                it brings, where the tag is written (compose
                                file or .env), every line before and after and
                                a fingerprint of it; or why it cannot be done
  retag <container> <tag> --expect <fingerprint> [--no-dump]
                                the tag change itself, only if the plan made
                                again has the same fingerprint: dumps, rollback
                                tag, byte-exact backup of the files, the lines
                                written (atomically), `docker compose config`
                                checked, then the update steps 3 to 6 with the
                                new tag. A rollback restores the files.

Only retag changes the compose or .env files, and only the lines it showed.
Images built locally or pinned by digest are not updated.

Everything the addon writes lives in /var/backups/image-updates (root only):
  dumps/<project>/<stamp>_<container>.sql.gz
  compose/<project>/<stamp>/    the files a tag change wrote, as they were
  operations.jsonl              one JSON line per update / tag change / rollback
  logs/<op-id>.log              output of the update (.rollback.log: of its rollback)

The machine-readable result of update/rollback/cleanup is the last line:
  @@RESULT {"op": "...", "status": "ok|nochange|failed", ...}

Lives in /usr/lib/cockpit-image-updates (root-owned, not mounted into any
container). DIUN_LANG=de|en selects the language of the log messages.
"""

import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from diunlib import CI_JOB_RE, CI_RUNNER_RE, core, is_ci_job, local_version, nums_of, tag_digits  # noqa: E402

BASE = "/var/backups/image-updates"
DUMPS = os.path.join(BASE, "dumps")
OPS = os.path.join(BASE, "operations.jsonl")
LOGS = os.path.join(BASE, "logs")
LOG_MAX = 512 * 1024      # characters kept of one operation's output
LOCK = "/run/lock/diun-update.lock"
CONF = "/etc/image-updates.conf"
VERSIONS = "/var/lib/diun-radar/versions.json"
RADAR_UNIT = "diun-radar.service"

L_PROJECT = "com.docker.compose.project"
L_SERVICE = "com.docker.compose.service"
L_WORKDIR = "com.docker.compose.project.working_dir"
L_FILES = "com.docker.compose.project.config_files"
L_ENVFILES = "com.docker.compose.project.environment_file"
L_HASH = "com.docker.compose.config-hash"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
OP_RE = re.compile(r"^\d{8}-\d{6}-[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
DUMP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/\d{8}-\d{6}_[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.sql\.gz$")
ROLLBACK_TAG_RE = re.compile(r"^rollback-\d{8}-\d{6}$")
PG_RE = re.compile(r"(^|/)(postgres|postgresql|postgis|pgvector|timescaledb[a-z0-9-]*)$")
MY_RE = re.compile(r"(^|/)(mariadb|mysql|percona-server)$")
# Markers the official images set themselves - catches renamed/derived images.
PG_ENV = ("PG_MAJOR=", "PG_VERSION=")
MY_ENV = ("MARIADB_VERSION=", "MARIADB_MAJOR=", "MYSQL_MAJOR=")
NODUMP_RE = re.compile(r"(^|/)(redis|valkey|keydb|memcached|clickhouse-server|mongo|influxdb|elasticsearch|opensearch)$")

HEALTH_TIMEOUT = 180     # seconds to wait for recreated containers
STABLE_SECONDS = 15      # without a healthcheck: must keep running this long
WATCH_ROUNDS = 5         # dependent services: checked this often, 3 s apart
FREE_MARGIN = 1 << 30    # keep at least 1 GiB free beyond the dump estimate

LANG = "de" if os.environ.get("DIUN_LANG", "en") == "de" else "en"


def say(de, en):
    print(de if LANG == "de" else en, flush=True)


def pick(de, en):
    return de if LANG == "de" else en


class Abort(Exception):
    def __init__(self, code, de, en):
        super().__init__(de if LANG == "de" else en)
        self.code = code


# --------------------------------------------------------------- helpers ----
def run(cmd, check=True, capture=True, timeout=600):
    p = subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, cmd, p.stdout, p.stderr)
    return p.stdout if capture else ""


STREAM_TAIL = []          # last lines of the last stream() call, for the failure reason


def stream(cmd):
    """Run a command, pass its output through to our stdout line by line and
    keep the last lines in STREAM_TAIL."""
    print("+ " + " ".join(cmd), flush=True)
    tail = deque(maxlen=50)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    for line in p.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        tail.append(line.rstrip("\n"))
    rc = p.wait()
    STREAM_TAIL[:] = list(tail)
    return rc


def docker_json(*args):
    return json.loads(run(["docker", *args]))


def inspect(name):
    if not NAME_RE.match(name):
        raise Abort(64, f"Ungültiger Containername: {name}", f"Invalid container name: {name}")
    try:
        return docker_json("inspect", "--type", "container", name)[0]
    except subprocess.CalledProcessError:
        raise Abort(2, f"Container {name} nicht gefunden.", f"Container {name} not found.")


def split_ref(ref):
    ref = ref.split("@", 1)[0]
    if ":" in ref.rsplit("/", 1)[-1]:
        repo, tag = ref.rsplit(":", 1)
        return repo, tag
    return ref, "latest"


def norm_ref(ref):
    """docker.io/library/postgres:16 -> postgres:16 (as `docker image ls` lists it)."""
    for p in ("docker.io/", "index.docker.io/"):
        if ref.startswith(p):
            ref = ref[len(p):]
    return ref[len("library/"):] if ref.startswith("library/") else ref


def image_id(ref):
    try:
        return run(["docker", "image", "inspect", "--format", "{{.Id}}", ref]).strip()
    except subprocess.CalledProcessError:
        return ""


def stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


_lock_fh = None     # held open until the process exits


def lock():
    """One update / rollback / cleanup at a time (exit 75 for the second)."""
    global _lock_fh
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    fh = open(LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise Abort(75, "Es läuft bereits ein Update oder Rollback.",
                    "An update or rollback is already running.")
    _lock_fh = fh


def ensure_base():
    for d in (BASE, DUMPS, LOGS):
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)


def systemctl(*args, timeout=60):
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=timeout)


def radar_installed():
    try:
        return systemctl("cat", RADAR_UNIT).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def start_radar():
    """After an update or rollback: refresh versions.json in the background,
    so versions only the radar can tell (label, digest) show up within
    minutes instead of after the next 6-hour run."""
    if not radar_installed():
        return
    try:
        ok = systemctl("start", "--no-block", RADAR_UNIT).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if ok:
        say("Versions-Radar gestartet – die Spalte „Installiert“ ist in wenigen Minuten aktuell.",
            "Version radar started – the Installed column is up to date within a few minutes.")


def radar_record(ref):
    """The radar's record for this image and tag (versions.json), or None."""
    repo, tag = split_ref(ref)
    try:
        with open(VERSIONS) as f:
            for r in json.load(f).get("images") or []:
                if r.get("image") == repo and r.get("tag") == tag:
                    return r
    except (OSError, ValueError, AttributeError):
        pass
    return None


def same_series(rec, version):
    """True if the radar found `version` among the registry's version tags,
    so what a floating tag points to (tag_now) counts in the same numbering.
    pgvector:pg17 runs PostgreSQL 17.10 (PG_VERSION) while its tags count
    pgvector releases (0.8.7-pg17): 17.10 -> 0.8.7 is no jump at all."""
    v = ((rec or {}).get("versions") or {}).get(version) or {}
    return v.get("status") not in (None, "unknown")


def jump_of(installed, target):
    """How big the step from one version to the other is: "major", "minor",
    "patch" (by the first block that differs, as the radar names them) or
    "same"; None if either is unknown."""
    try:
        a, b = nums_of(installed), nums_of(target)
    except (AttributeError, ValueError):
        return None
    if a == b:
        return "same"
    return "major" if a[:1] != b[:1] else "minor" if a[:2] != b[:2] else "patch"


def refines(found, version):
    """`found` names the release of what `version` names only by its line:
    1.6.74 for 1.6 (a line tag such as v1.6), or a version for none."""
    return bool(found) and (not version or found.startswith(version + "."))


def version_of(ref, image_id):
    """Version of a local image for the history and the plan: the app's
    version variable or the tag (diunlib), otherwise what the radar found for
    this image ID (label, digest). The radar's version also wins when it
    only refines the tag's number: a line tag (cubejs/cube:v1.6) names its
    line, the radar the release (1.6.74). None if unknown. Never fails -
    the history is only info."""
    if not image_id:
        return None
    repo, tag = split_ref(ref)
    v = src = None
    try:
        v, src = local_version(repo, tag, docker_json("image", "inspect", image_id)[0])
    except Exception:
        pass
    if v and src != "tag":
        return v
    found = None
    try:
        with open(VERSIONS) as f:
            for r in json.load(f).get("images") or []:
                if r.get("image") == repo and r.get("tag") == tag:
                    found = ((r.get("installed") or {}).get(image_id) or {}).get("version")
                    break
    except Exception:
        pass
    return found if refines(found, v) else v


def radar_versions():
    """{image ID: version} from the radar's last run, for history entries
    whose versions the update could not tell yet: the radar the update starts
    names them by label or digest a few minutes later. {} without the file."""
    known = {}
    try:
        with open(VERSIONS) as f:
            for r in json.load(f).get("images") or []:
                for iid, v in (r.get("installed") or {}).items():
                    if isinstance(v, dict) and v.get("version"):
                        known.setdefault(iid, v["version"])
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return known


PG_SERVER = {"postgres", "postmaster"}
MY_SERVER = {"mysqld", "mariadbd", "mysqld_safe"}


def command_words(c):
    """Program names in the container's effective command (entrypoint + cmd,
    shell strings split), e.g. {"docker-entrypoint.sh", "postgres", "-c"}."""
    cfg = c["Config"]
    words = set()
    for a in (cfg.get("Entrypoint") or []) + (cfg.get("Cmd") or []):
        words.update(os.path.basename(w) for w in str(a).split())
    return words


def runs_server(c, servers):
    """True if the container's command starts the database server: the server
    is named in it, or the command is options only (`command:
    --log-bin=binlog`, `-c max_connections=200`) for the official images'
    docker-entrypoint.sh, which then puts the server in front of them
    (mariadbd, mysqld, postgres)."""
    if command_words(c) & servers:
        return True
    cfg = c["Config"]
    entry = [os.path.basename(str(a)) for a in cfg.get("Entrypoint") or []]
    cmd = [str(a) for a in cfg.get("Cmd") or []]
    return entry == ["docker-entrypoint.sh"] and bool(cmd) and cmd[0].startswith("-")


def kind_of(c):
    """Database type of a container (inspect dict). The image is recognised by
    name or by the version variables the official postgres / mariadb / mysql
    images set; it only counts as a database if its command actually starts
    the server - a helper container that merely reuses the image (sh -c …,
    migrations, backups) is not dumped."""
    repo = split_ref(c["Config"]["Image"])[0].lower()
    env = c["Config"].get("Env") or []
    if PG_RE.search(repo) or any(e.startswith(PG_ENV) for e in env):
        return "postgres" if runs_server(c, PG_SERVER) else None
    if MY_RE.search(repo) or any(e.startswith(MY_ENV) for e in env):
        return "mysql" if runs_server(c, MY_SERVER) else None
    if NODUMP_RE.search(repo):
        return "unsupported"
    return None


# --------------------------------------------------------------- compose ----
def compose_ctx(c):
    labels = c["Config"].get("Labels") or {}
    project, service = labels.get(L_PROJECT), labels.get(L_SERVICE)
    workdir, files = labels.get(L_WORKDIR), labels.get(L_FILES)
    if not (project and service and workdir and files):
        return None
    return {
        "project": project,
        "workdir": workdir,
        "files": [f for f in files.split(",") if f],
        "env_files": [f for f in (labels.get(L_ENVFILES) or "").split(",") if f],
    }


def compose_cmd(ctx):
    cmd = ["docker", "compose", "-p", ctx["project"], "--project-directory", ctx["workdir"]]
    for f in ctx["files"]:
        cmd += ["-f", f]
    for e in ctx["env_files"]:
        if os.path.exists(e):
            cmd += ["--env-file", e]
    return cmd


def same_files(a, b):
    """Started from the same compose files (and project directory)."""
    la, lb = a["Config"].get("Labels") or {}, b["Config"].get("Labels") or {}
    return (la.get(L_WORKDIR), la.get(L_FILES)) == (lb.get(L_WORKDIR), lb.get(L_FILES))


def project_containers(project):
    names = run(["docker", "ps", "-a", "--filter", f"label={L_PROJECT}={project}",
                 "--format", "{{.Names}}"]).split()
    return [inspect(n) for n in names]


def service_containers(project, service):
    names = run(["docker", "ps", "-a",
                 "--filter", f"label={L_PROJECT}={project}",
                 "--filter", f"label={L_SERVICE}={service}",
                 "--format", "{{.Names}}"]).split()
    return names


# ------------------------------------------------------------------ plan ----
def make_plan(name):
    c = inspect(name)
    ref = c["Config"]["Image"]
    plan = {"container": name, "image": ref, "updatable": False, "reason": None,
            "project": None, "workdir": None, "services": [], "containers": [],
            "dbs": [], "free_bytes": None, "config_changed": False,
            "pulled": False, "old_image": c["Image"],
            "installed_version": None, "tag_digits": None, "jump_unknown": False,
            "target_version": None, "jump": None,
            "jobs": [], "depends_on": [], "dependents": [], "job_order": [], "shared_networks": [],
            "ci_jobs": []}

    ctx = compose_ctx(c)
    if not ctx:
        plan["reason"] = "not-compose"
        return plan, None
    plan["project"], plan["workdir"] = ctx["project"], ctx["workdir"]
    missing = [f for f in ctx["files"] if not os.path.exists(f)]
    if missing:
        plan["reason"] = "compose-file-missing"
        plan["missing"] = missing
        return plan, ctx
    if "@" in ref:
        plan["reason"] = "digest-pinned"
        return plan, ctx

    members = project_containers(ctx["project"])
    # Together only with containers started from the same compose files: a
    # project can be split over files that are started one by one
    # (docker-compose.yml with runner, runner-2, runner-3 and
    # docker-compose.emu.yml with runner-emu, all on one image), and compose
    # knows only the services of the files it is given. The others get the
    # new image as "pulled" and are rolled out from their own files.
    same = [m for m in members if m["Config"]["Image"] == ref and same_files(m, c)]
    services = sorted({(m["Config"]["Labels"] or {}).get(L_SERVICE) for m in same} - {None})
    plan["services"] = services
    plan["containers"] = sorted(m["Name"].lstrip("/") for m in same)

    try:
        cfg = json.loads(run(compose_cmd(ctx) + ["config", "--format", "json"]))
    except subprocess.CalledProcessError as e:
        plan["reason"] = "compose-config-error"
        plan["detail"] = (e.stderr or "").strip()[-400:]
        return plan, ctx
    ctx["cfg"] = cfg
    missing = [s for s in services if s not in (cfg.get("services") or {})]
    if missing:
        plan["reason"] = "service-missing"
        plan["missing"] = missing
        return plan, ctx
    for s in services:
        svc = (cfg.get("services") or {}).get(s) or {}
        if svc.get("build") is not None:
            plan["reason"] = "built-locally"
            return plan, ctx
    relations(plan, cfg, members, same, services, ctx["project"])

    # Has the compose file changed since these containers were created?
    try:
        out = run(compose_cmd(ctx) + ["config", "--hash", ",".join(services)])
        current = dict(line.split()[:2] for line in out.splitlines() if len(line.split()) >= 2)
        for m in same:
            lbl = m["Config"]["Labels"] or {}
            if current.get(lbl.get(L_SERVICE)) not in (None, lbl.get(L_HASH)):
                plan["config_changed"] = True
    except Exception:
        pass

    tagid = image_id(ref)
    plan["pulled"] = bool(tagid) and tagid != c["Image"]

    # A tag that fixes no version number (latest, stable …) follows every
    # release. If the running version is unknown too, nobody can tell how big
    # the jump is: the plugin warns and asks for an extra confirmation.
    plan["installed_version"] = version_of(ref, c["Image"])
    plan["tag_digits"] = tag_digits(split_ref(ref)[1])
    plan["jump_unknown"] = plan["installed_version"] is None and plan["tag_digits"] == 0

    # What the update brings: the image pulled already, otherwise what the
    # tag points to in the registry right now (radar, "tag_now") - if that
    # counts in the same numbering as the installed version.
    if plan["pulled"]:
        target = version_of(ref, tagid)
    else:
        rec = radar_record(ref) or {}
        now_ = rec.get("tag_now") or {}
        same = bool(now_.get("digest")) and now_["digest"] in repo_digests(c["Image"])
        if same:
            target = plan["installed_version"]
        elif plan["installed_version"] is None or same_series(rec, plan["installed_version"]):
            target = now_.get("version")
        else:
            target = None
    plan["target_version"] = target
    plan["jump"] = jump_of(plan["installed_version"], target)

    for m in members:
        k = kind_of(m)
        if not k:
            continue
        db = {"name": m["Name"].lstrip("/"), "kind": k,
              "running": bool(m["State"].get("Running")), "size": None}
        if k == "postgres" and db["running"]:
            try:
                db["size"] = int(run(["docker", "exec", db["name"], "sh", "-c",
                                      'psql -U "${POSTGRES_USER:-postgres}" -d postgres -Atc '
                                      '"select coalesce(sum(pg_database_size(datname)),0) from pg_database"'],
                                     timeout=60).strip() or 0)
            except Exception:
                db["size"] = None
        plan["dbs"].append(db)

    # A CI runner: its running jobs break off when it is recreated.
    if CI_RUNNER_RE.search(norm_ref(split_ref(ref)[0]).lower()):
        plan["ci_jobs"] = running_ci_jobs()

    plan["free_bytes"] = shutil.disk_usage(BASE if os.path.isdir(BASE) else os.path.dirname(BASE)).free
    plan["updatable"] = True
    return plan, ctx


def running_ci_jobs():
    """The containers of CI jobs that run now: [{"name", "image", "started"}]."""
    try:
        ids = run(["docker", "ps", "-q"]).split()
        found = json.loads(run(["docker", "inspect", *ids])) if ids else []
    except (subprocess.CalledProcessError, ValueError):
        return []
    return sorted(({"name": c["Name"].lstrip("/"), "image": c["Config"]["Image"],
                    "started": (c.get("State") or {}).get("StartedAt") or ""}
                   for c in found if is_ci_job(c)), key=lambda j: j["name"])


def is_job(c):
    """A one-off container such as a migration: not running, restart policy
    "no" or "on-failure"."""
    policy = ((c.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or "no"
    return not (c.get("State") or {}).get("Running") and policy in ("no", "on-failure")


def relations(plan, cfg, members, same, services, project):
    """What the update touches besides the services of its image, from
    `docker compose config` and the containers:
      jobs             one-off containers among them (migrations), run again
      depends_on       services they wait for, with the condition
      dependents       services that wait for them; watched after the update
      job_order        services of the same image that do not wait for a job
                       of that image (can start before the migration is done)
      shared_networks  other compose projects on the same user-defined network
                       (a hint: being on a network does not prove use)"""
    svc_cfg = cfg.get("services") or {}
    svc_of = {m["Name"].lstrip("/"): (m["Config"]["Labels"] or {}).get(L_SERVICE) for m in members}
    plan["jobs"] = sorted(m["Name"].lstrip("/") for m in same if is_job(m))
    job_svcs = sorted({svc_of[n] for n in plan["jobs"]} - {None})

    deps = {}
    for s in services:
        for d, spec in sorted(((svc_cfg.get(s) or {}).get("depends_on") or {}).items()):
            if d not in services:
                deps.setdefault(d, (spec or {}).get("condition") or "service_started")
    plan["depends_on"] = [{"service": d, "condition": c} for d, c in sorted(deps.items())]

    for s in sorted(svc_cfg):
        if s in services or not set((svc_cfg[s] or {}).get("depends_on") or {}) & set(services):
            continue
        plan["dependents"].append({"service": s, "containers": sorted(n for n, v in svc_of.items() if v == s)})

    for j in job_svcs:
        waiting = []
        for s in services:
            if s in job_svcs:
                continue
            cond = (((svc_cfg.get(s) or {}).get("depends_on") or {}).get(j) or {}).get("condition")
            if cond != "service_completed_successfully":
                waiting.append(s)
        if waiting:
            plan["job_order"].append({"job": j, "services": waiting})

    nets = set()
    for m in same:
        nets.update(((m.get("NetworkSettings") or {}).get("Networks") or {}).keys())
    for n in sorted(nets - {"bridge", "host", "none", project + "_default"}):
        try:
            out = run(["docker", "ps", "-a", "--filter", "network=" + n,
                       "--format", '{{.Names}}\t{{.Label "com.docker.compose.project"}}'])
        except subprocess.CalledProcessError:
            continue
        others = set()
        for line in out.splitlines():
            name, _, proj = line.partition("\t")
            if proj != project and (proj or not CI_JOB_RE.match(name)):   # CI jobs come and go
                others.add(proj or name)
        if others:
            plan["shared_networks"].append({"network": n, "projects": sorted(others)})


# ----------------------------------------------------------------- retag ----
# Stage 2: change the configured tag. Where the tag is written is found in the
# text of the compose and .env files (no YAML library, no round trip), so the
# lines that would change can be shown exactly before anything is written.
TAG_ARG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
FP_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_LINE_RE = re.compile(r"^(?P<pre>[ ]*image:[ \t]*)(?P<q>[\"']?)(?P<val>[^\"'\s#]+)(?P=q)"
                           r"(?P<post>[ \t]*(?:#.*)?)$")
KEY_LINE_RE = re.compile(r"^(?P<indent>[ ]*)(?P<key>[A-Za-z0-9._-]+):[ \t]*(?:#.*)?$")
ENV_LINE_RE = re.compile(r"^(?P<pre>[ \t]*(?:export[ \t]+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*)"
                         r"(?P<q>[\"']?)(?P<val>.*?)(?P=q)(?P<post>[ \t]+#.*|[ \t]*)$")
VAR_RE = re.compile(r"\$(?:\{(?P<b>[A-Za-z_][A-Za-z0-9_]*)(?P<op>:?-(?P<default>[^}]*))?\}"
                    r"|(?P<s>[A-Za-z_][A-Za-z0-9_]*))")
TAG_VAR_RE = re.compile(r"^(?P<repo>[^$]+):(?P<expr>\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*)$")
NEXTCLOUD_RE = re.compile(r"(^|/)nextcloud$")
TEXT_MAX = 1 << 20        # compose and .env files larger than this are not read


def same_ref(a, b):
    """Same image reference, however Docker Hub names are written."""
    if not a or not b:
        return False
    (ra, ta), (rb, tb) = split_ref(a), split_ref(b)
    return ta == tb and norm_ref(ra) == norm_ref(rb)


def read_lines(path):
    """(lines with their line endings, sha256) of a compose or .env file, or
    None if it is no regular file, larger than TEXT_MAX or not UTF-8."""
    real = os.path.realpath(path)
    try:
        if not os.path.isfile(real) or os.path.getsize(real) > TEXT_MAX:
            return None
        with open(real, "rb") as f:
            data = f.read()
        return data.decode("utf-8").splitlines(keepends=True), hashlib.sha256(data).hexdigest()
    except (OSError, UnicodeDecodeError):
        return None


def bare(line):
    return line.rstrip("\r\n")


def indent_of(line):
    return len(line) - len(line.lstrip(" "))


def parent_key(lines, i):
    """(index, key) of the nearest line above i with less indentation (blank
    lines and comments skipped); key is None when that line is no plain
    `name:` key (an anchor `x-app: &app`, a list item ...)."""
    ind = indent_of(lines[i])
    for j in range(i - 1, -1, -1):
        s = bare(lines[j])
        if not s.strip() or s.lstrip().startswith("#"):
            continue
        if indent_of(s) < ind:
            m = KEY_LINE_RE.match(s)
            return j, (m["key"] if m else None)
    return None, None


def service_of_line(lines, i):
    """The service whose block holds line i as a direct key
    (services.<name>.image), or None: an image in an x- block, under an
    anchor or anywhere else."""
    j, key = parent_key(lines, i)
    if key is None or indent_of(lines[j]) == 0:
        return None
    k, top = parent_key(lines, j)
    return key if top == "services" and indent_of(lines[k]) == 0 else None


def interpolate(val, env):
    """Compose's ${VAR}, $VAR, ${VAR:-default} and ${VAR-default} with the
    .env values; None when a variable has neither a value nor a default."""
    missing = []

    def sub(m):
        name, op = m["b"] or m["s"], m["op"] or ""
        v = env.get(name)
        if op.startswith(":-"):
            return v if v else m["default"]
        if op.startswith("-"):
            return v if v is not None else m["default"]
        if v is None:
            missing.append(name)
            return ""
        return v

    out = VAR_RE.sub(sub, val)
    return None if missing else out


def var_uses(lines):
    """{name: count} of variable references outside comment lines."""
    uses = {}
    for line in lines:
        if line.lstrip().startswith("#"):
            continue
        for m in VAR_RE.finditer(line):
            n = m["b"] or m["s"]
            uses[n] = uses.get(n, 0) + 1
    return uses


def offered(choices, tag):
    """(kind, level, newest) under which the radar offers tag for the
    installed version (versions.json "choices"), or None."""
    if tag == choices.get("pin"):
        return "pin", None, None
    for x in choices.get("newer") or []:
        if x.get("tag") == tag:
            return "newer", x.get("level"), None
    for x in choices.get("lines") or []:
        if x.get("tag") == tag:
            return "line", None, x.get("newest")
    if tag in (choices.get("floating") or []):
        return "floating", None, None
    return None


def git_root(path):
    """The git repository a project directory lies in, or None (a hint only:
    the add-on never commits)."""
    p = os.path.realpath(path)
    while True:
        if os.path.exists(os.path.join(p, ".git")):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def locate_tag(ctx, ref, services, new_tag):
    """Where the tag of `ref` is written for these services and how each
    line would read with new_tag: {"changes": [...], "variable": name or
    None}, or a refusal {"reason", "detail"}.
      image: repo:5.0.2                 that line (every service of the image)
      image: repo:${FB_VERSION}         the variable's line in the .env file,
                                        if it is set there once and used for
                                        this image only
      image: ${FB_IMAGE}                the same, with the whole reference
    Anything else (anchors, a variable with a default only or used
    elsewhere, a reference built from several variables) is refused."""
    cfg_svcs = (ctx.get("cfg") or {}).get("services") or {}
    wanted = set(services) | {s for s, v in cfg_svcs.items() if same_ref((v or {}).get("image"), ref)}
    defs, env_lines = {}, []
    # Without an --env-file compose reads <project directory>/.env itself.
    for f in ctx["env_files"] or [os.path.join(ctx["workdir"], ".env")]:
        got = read_lines(f) if os.path.exists(f) else None
        if got is None:
            continue
        env_lines += got[0]
        for i, line in enumerate(got[0]):
            m = ENV_LINE_RE.match(bare(line))
            if m:
                defs.setdefault(m["key"], []).append((f, i, m, got[1]))
    env = {k: v[-1][2]["val"] for k, v in defs.items()}
    tag = split_ref(ref)[1]
    changes, found, unresolved, var_refs, compose_lines = [], set(), set(), {}, []
    for f in ctx["files"]:
        got = read_lines(f)
        if got is None:
            return {"reason": "unreadable", "detail": f}
        lines, digest = got
        compose_lines += lines
        for i, line in enumerate(lines):
            m = IMAGE_LINE_RE.match(bare(line))
            if not m:
                continue
            val, svc = m["val"], service_of_line(lines, i)
            resolved = interpolate(val, env)
            if resolved is None:
                unresolved.add(svc)
                continue
            if not same_ref(resolved, ref):
                continue
            if svc is None:
                return {"reason": "anchors", "detail": f"{f}:{i + 1}"}
            if svc not in wanted:
                continue                   # a later file gives this service another image
            found.add(svc)
            if "$" not in val:
                new = m["pre"] + m["q"] + split_ref(val)[0] + ":" + new_tag + m["q"] + m["post"]
                changes.append({"file": f, "line": i + 1, "old": bare(line), "new": new,
                                "service": svc, "sha256": digest})
                continue
            whole, part = VAR_RE.fullmatch(val), TAG_VAR_RE.match(val)
            expr = val if whole else (part["expr"] if part else "")
            vm = VAR_RE.fullmatch(expr)
            if not vm:
                return {"reason": "variable-complex", "detail": f"{f}:{i + 1}"}
            var_refs.setdefault(vm["b"] or vm["s"], []).append((svc, bool(whole)))
    missing = sorted(wanted - found)
    if missing:
        return {"reason": "variable-complex" if set(missing) & unresolved else "not-found",
                "detail": ", ".join(missing)}
    if len(var_refs) > 1:
        return {"reason": "variable-complex", "detail": ", ".join(sorted(var_refs))}
    uses = var_uses(compose_lines + env_lines)
    for name, refs in var_refs.items():
        if len(defs.get(name, [])) != 1:
            return {"reason": "variable-complex", "detail": name}
        if uses.get(name, 0) != len(refs):
            return {"reason": "variable-shared", "detail": name}
        f, i, m, digest = defs[name][0]
        if all(whole for _, whole in refs) and same_ref(m["val"], ref):
            value = split_ref(m["val"])[0] + ":" + new_tag
        elif not any(whole for _, whole in refs) and m["val"] == tag:
            value = new_tag
        else:
            return {"reason": "variable-complex", "detail": name}
        changes.append({"file": f, "line": i + 1, "old": m.string,
                        "new": m["pre"] + m["q"] + value + m["q"] + m["post"],
                        "service": None, "sha256": digest})
    return {"changes": changes, "variable": next(iter(var_refs), None)}


def fingerprint(new_tag, changes):
    """What the page showed, as one value: the new tag and every change with
    its file checksum. `retag` without --dry-run only writes when the plan it
    makes again gives the same fingerprint."""
    data = {"tag": new_tag, "changes": [[c["file"], c["line"], c["old"], c["new"], c["sha256"]] for c in changes]}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def make_retag(name, new_tag):
    """Read-only plan of a tag change (`retag … --dry-run`): the plan of an
    update plus what the radar offers the new tag as, the version it brings,
    where the tag is written and every line before and after; or why it
    cannot be done here. Returns (plan, compose context)."""
    if not TAG_ARG_RE.match(new_tag):
        raise Abort(64, f"Ungültiger Tag: {new_tag}", f"Invalid tag: {new_tag}")
    plan, ctx = make_plan(name)
    repo, old_tag = split_ref(plan["image"])
    rt = plan["retag"] = {"tag": new_tag, "from_tag": old_tag, "image": repo + ":" + new_tag,
                          "choice": None, "level": None, "newest": None,
                          "possible": False, "reason": None, "detail": None,
                          "variable": None, "changes": [], "git": None, "fingerprint": None}

    def refuse(reason, detail=None):
        rt["reason"], rt["detail"] = reason, detail
        return plan, ctx

    if not plan["updatable"]:
        return refuse(plan["reason"], plan.get("detail"))
    if new_tag == old_tag:
        return refuse("same-tag")
    rec = radar_record(plan["image"]) or {}
    installed = plan["installed_version"]
    choices = ((rec.get("versions") or {}).get(installed) or {}).get("choices") if installed else None
    if not choices:
        return refuse("no-choices")
    kind = offered(choices, new_tag)
    if not kind:
        return refuse("not-offered")
    rt["choice"], rt["level"], rt["newest"] = kind
    target = {"pin": installed, "newer": core(new_tag), "line": core(rt["newest"]), "floating": None}[kind[0]]
    plan["target_version"], plan["jump"] = target, jump_of(installed, target)
    plan["jump_unknown"] = target is None

    def major(v):
        return nums_of(v)[0]

    # A database major needs a migration (stage 5); a floating tag can bring one.
    if rec.get("db") and (target is None or major(target) != major(installed)):
        return refuse("db-major")
    # Nextcloud upgrades one major version per step only.
    if NEXTCLOUD_RE.search(norm_ref(repo)) and (target is None or major(target) - major(installed) > 1):
        return refuse("major-skip")
    got = locate_tag(ctx, plan["image"], plan["services"], new_tag)
    if got.get("reason"):
        return refuse(got["reason"], got.get("detail"))
    rt["changes"], rt["variable"] = got["changes"], got["variable"]
    rt["git"] = git_root(ctx["workdir"])
    rt["fingerprint"] = fingerprint(new_tag, rt["changes"])
    rt["possible"] = True
    return plan, ctx


def sha256_of(path):
    with open(os.path.realpath(path), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def changed_bytes(path, changes):
    """The file's bytes with the planned lines replaced (line endings kept),
    or None when the file is no longer the one that was planned."""
    got = read_lines(path)
    if got is None or any(c["sha256"] != got[1] for c in changes):
        return None
    lines = got[0]
    for c in changes:
        i = c["line"] - 1
        if i >= len(lines) or bare(lines[i]) != c["old"]:
            return None
        lines[i] = c["new"] + lines[i][len(bare(lines[i])):]
    return "".join(lines).encode("utf-8")


def write_like(path, data):
    """Replace a file atomically with data, keeping its owner and mode (a
    symlink stays; the file it points to is replaced)."""
    real = os.path.realpath(path)
    st = os.stat(real)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(real), prefix="." + os.path.basename(real) + ".",
                               suffix=".imgupd")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chown(tmp, st.st_uid, st.st_gid)
        os.chmod(tmp, st.st_mode & 0o7777)
        os.replace(tmp, real)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backup_files(project, st, paths):
    """Byte-exact copies of the files a tag change writes, in
    compose/<project>/<stamp>/ (root only). Returns (folder,
    [{"path", "backup", "sha256"}])."""
    if not NAME_RE.match(project):
        raise Abort(64, f"Ungültiger Projektname: {project}", f"Invalid project name: {project}")
    top = os.path.join(BASE, "compose")
    d = os.path.join(top, project, st)
    os.makedirs(d, mode=0o700, exist_ok=True)
    for x in (top, os.path.dirname(d), d):
        os.chmod(x, 0o700)
    saved = []
    for i, p in enumerate(paths, 1):
        with open(os.path.realpath(p), "rb") as f:
            data = f.read()
        b = os.path.join(d, f"{i}_{os.path.basename(p)}")
        fd = os.open(b, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        saved.append({"path": p, "backup": b, "sha256": hashlib.sha256(data).hexdigest()})
    return d, saved


def restore_files(saved):
    """Put the saved bytes back, after a failed check or on rollback."""
    for x in saved:
        with open(x["backup"], "rb") as f:
            write_like(x["path"], f.read())


def drop_backup(folder):
    """Remove the backup of a tag change that stopped before any container
    changed, and the project and compose folders if they are empty then."""
    shutil.rmtree(folder, ignore_errors=True)
    for parent in (os.path.dirname(folder), os.path.dirname(os.path.dirname(folder))):
        try:
            os.rmdir(parent)
        except OSError:
            break


def running_state(names):
    """{name: (StartedAt, RestartCount)} of the containers that run now."""
    out = {}
    for n in names:
        try:
            st = docker_json("inspect", "--type", "container", n)[0].get("State") or {}
        except (subprocess.CalledProcessError, ValueError, IndexError, OSError):
            continue
        if st.get("Running"):
            out[n] = (st.get("StartedAt"), st.get("RestartCount", 0))
    return out


def watch_dependents(before):
    """After the update: the dependent services (not recreated) must keep
    running, without new restarts, and stay healthy if they have a health
    check. Returns the names of those that do not."""
    if not before:
        return []
    say(f"Beobachte abhängige Dienste: {', '.join(sorted(before))} …",
        f"Watching dependent services: {', '.join(sorted(before))} …")
    bad = set()
    for _ in range(WATCH_ROUNDS):
        time.sleep(3)
        for n, (started, restarts) in before.items():
            try:
                st = docker_json("inspect", "--type", "container", n)[0].get("State") or {}
            except (subprocess.CalledProcessError, ValueError, IndexError, OSError):
                bad.add(n)
                continue
            health = (st.get("Health") or {}).get("Status")
            if (not st.get("Running") or st.get("StartedAt") != started
                    or st.get("RestartCount", 0) > restarts or health == "unhealthy"):
                bad.add(n)
    if not bad:
        say("Abhängige Dienste laufen weiter.", "Dependent services keep running.")
    return sorted(bad)


# ----------------------------------------------------------------- dumps ----
PG_DUMP = 'exec pg_dumpall -U "${POSTGRES_USER:-postgres}"'
MY_DUMP = ('P="${MARIADB_ROOT_PASSWORD:-$MYSQL_ROOT_PASSWORD}"; '
           'if command -v mariadb-dump >/dev/null 2>&1; then D=mariadb-dump; else D=mysqldump; fi; '
           'exec "$D" --all-databases --single-transaction --routines --events -uroot -p"$P"')
DUMP_DONE = {"postgres": b"database cluster dump complete", "mysql": b"-- Dump completed"}


def dump_db(project, db, st):
    say(f"Datenbank-Dump von {db['name']} ({db['kind']}) …",
        f"Dumping database {db['name']} ({db['kind']}) …")
    if not db["running"]:
        raise Abort(5, f"{db['name']} läuft nicht – kein Dump möglich. Update abgebrochen.",
                    f"{db['name']} is not running – cannot dump. Update aborted.")
    pdir = os.path.join(DUMPS, project)
    os.makedirs(pdir, mode=0o700, exist_ok=True)
    final = os.path.join(pdir, f"{st}_{db['name']}.sql.gz")
    tmp = final + ".part"
    script = PG_DUMP if db["kind"] == "postgres" else MY_DUMP
    tail = b""
    t0 = time.time()
    errf = tempfile.TemporaryFile()
    proc = subprocess.Popen(["docker", "exec", db["name"], "sh", "-c", script],
                            stdout=subprocess.PIPE, stderr=errf)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6) as gz:
        for chunk in iter(lambda: proc.stdout.read(1 << 16), b""):
            gz.write(chunk)
            tail = (tail + chunk)[-4096:]
    rc = proc.wait()
    errf.seek(0)
    err = errf.read().decode(errors="replace").strip()
    errf.close()
    if rc != 0 or DUMP_DONE[db["kind"]] not in tail:
        os.unlink(tmp)
        msg = err[-400:] if err else f"exit {rc}"
        raise Abort(5, f"Dump von {db['name']} fehlgeschlagen ({msg}). Update abgebrochen – nichts geändert.",
                    f"Dump of {db['name']} failed ({msg}). Update aborted – nothing changed.")
    os.replace(tmp, final)
    size = os.path.getsize(final)
    say(f"  -> {final} ({human(size)}, {time.time() - t0:.0f} s)",
        f"  -> {final} ({human(size)}, {time.time() - t0:.0f} s)")
    return os.path.relpath(final, DUMPS)


# ---------------------------------------------------------------- health ----
def wait_healthy(project, services):
    deadline = time.time() + HEALTH_TIMEOUT
    names = sorted({n for s in services for n in service_containers(project, s)})
    say(f"Warte auf {', '.join(names)} (max. {HEALTH_TIMEOUT} s) …",
        f"Waiting for {', '.join(names)} (max {HEALTH_TIMEOUT} s) …")
    last_report = 0
    restarting = set()      # containers seen in the state "restarting" before
    while True:
        states, bad, pending = {}, [], []
        for n in names:
            info = docker_json("inspect", "--type", "container", n)[0]
            st = info["State"]
            health = (st.get("Health") or {}).get("Status")
            status = st.get("Status")
            states[n] = status + (f"/{health}" if health else "")
            policy = ((info.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or "no"
            if status == "exited" and st.get("ExitCode") == 0 and policy in ("no", "on-failure"):
                continue        # one-shot job (e.g. migrations) finished cleanly
            # "restarting": the container exited and waits for its next start.
            # Docker counts the restart only when it happens, so RestartCount
            # can still be 0: seen restarting a second time, it is a restart
            # loop. A single crash that recovers does not count.
            again = status == "restarting" and n in restarting
            if status == "restarting":
                restarting.add(n)
            if status in ("exited", "dead") or health == "unhealthy" or (
                    status == "restarting" and (st.get("RestartCount", 0) > 0 or again)):
                bad.append(n)
            elif health:
                if health != "healthy":
                    pending.append(n)
            else:
                started = st.get("StartedAt", "")[:19]
                try:
                    age = time.time() - datetime.strptime(started, "%Y-%m-%dT%H:%M:%S").replace(
                        tzinfo=timezone.utc).timestamp()
                except ValueError:
                    age = 0
                if status != "running" or age < STABLE_SECONDS:
                    pending.append(n)
        if bad:
            return False, bad, states
        if not pending:
            say("Alle Container laufen: " + ", ".join(f"{k}={v}" for k, v in states.items()),
                "All containers running: " + ", ".join(f"{k}={v}" for k, v in states.items()))
            return True, [], states
        if time.time() > deadline:
            return False, pending, states
        if time.time() - last_report > 15:
            say("  … " + ", ".join(f"{k}={v}" for k, v in states.items()),
                "  … " + ", ".join(f"{k}={v}" for k, v in states.items()))
            last_report = time.time()
        time.sleep(3)


def show_logs(names):
    for n in names:
        say(f"--- letzte Log-Zeilen von {n} ---", f"--- last log lines of {n} ---")
        try:
            lines = container_logs(n, 30)
        except (OSError, subprocess.SubprocessError):
            lines = []
        for ln in lines:
            print(ln)
        sys.stdout.flush()


# ------------------------------------------------------ why it failed ----
LOG_TAIL = 80            # log lines searched for the cause
REASON_MAX = 300
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# Stack frames and similar lines that never explain a failure by themselves.
FRAME_RE = re.compile(r"^\s*(?:at\s|File \"|Traceback \(most recent call last\)|raise\s|During handling of the above"
                      r"|The above exception|goroutine \d+|[\^~\[\]{}(),;\s]*$)")
# A line that names an error: "Error: …", "TypeError: …",
# "django.db.utils.ProgrammingError: …", "error: …", "panic: …", "FATAL:  …".
HEAD_RE = re.compile(r"^\s*(?:[\w.]+\.)?[A-Za-z]*(?:Error|Exception)\b|^\s*(?:error(?:\[\w+\])?|panic|fatal error|fatal):"
                     r"|\b(?:FATAL|PANIC|CRITICAL)\b")
# Otherwise any line that mentions a problem.
ANY_RE = re.compile(r"(?i)\b(?:error|fatal|failed|failure|exception|panic|denied|refused|cannot|could not"
                    r"|unable to|not found|no such)\b")


def reason_line(lines):
    """The most telling line of a failed container's log: the last line that
    names an error, with the next line when it ends in ":" ("Error: Invalid
    environment variables:" + the first variable); otherwise the last line
    that mentions a problem. Stack frames never count. None if nothing fits."""
    clean = [ln for ln in (ANSI_RE.sub("", x).rstrip() for x in lines) if ln.strip()]
    for pat in (HEAD_RE, ANY_RE):
        for i in range(len(clean) - 1, -1, -1):
            if FRAME_RE.match(clean[i]) or not pat.search(clean[i]):
                continue
            text = clean[i].strip()
            if text.endswith(":") and i + 1 < len(clean) and not FRAME_RE.match(clean[i + 1]):
                text += " " + clean[i + 1].strip()
            return text[:REASON_MAX]
    return None


def container_logs(name, n=LOG_TAIL):
    p = subprocess.run(["docker", "logs", "--tail", str(n), name], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, errors="replace", timeout=30)
    return p.stdout.splitlines()


def failure_reason(names, compose_rc=0):
    """Most likely cause of a failed update or rollback, for @@RESULT and the
    history: {"container", "state", "exit_code", "oom", "source", "line"}.
    source is "log", "health" (last output of the health check) or "compose"
    (a failed `docker compose up`, from its output); line is None when nothing
    telling was found. None if there is no container to ask."""
    if compose_rc:
        return {"container": None, "state": None, "exit_code": compose_rc, "oom": False,
                "source": "compose", "line": reason_line(STREAM_TAIL)}
    first = None
    for n in names:
        try:
            st = docker_json("inspect", "--type", "container", n)[0].get("State") or {}
        except (subprocess.CalledProcessError, ValueError, IndexError, OSError):
            continue
        health = st.get("Health") or {}
        hs = health.get("Status")
        r = {"container": n, "state": hs if hs in ("unhealthy", "starting") else st.get("Status"),
             "exit_code": st.get("ExitCode"), "oom": bool(st.get("OOMKilled")), "source": None, "line": None}
        probe = [ln.strip() for ln in (((health.get("Log") or [{}])[-1] or {}).get("Output") or "").splitlines()
                 if ln.strip()]
        if hs == "unhealthy" and probe:
            r.update(source="health", line=probe[-1][:REASON_MAX])
        else:
            try:
                line = reason_line(container_logs(n))
            except (OSError, subprocess.SubprocessError):
                line = None
            if line:
                r.update(source="log", line=line)
        if r["line"]:
            return r
        first = first or r
    return first


def error_reason(e):
    """The cause when the helper itself stopped after the containers were
    recreated (Docker did not answer, a command failed): source "error"."""
    text = (getattr(e, "stderr", None) or str(e) or type(e).__name__).strip()
    line = (text.splitlines() or [type(e).__name__])[-1][:REASON_MAX]
    return {"container": None, "state": None, "exit_code": None, "oom": False, "source": "error",
            "line": pick(f"Prüfung nach dem Neustart abgebrochen: {line}", f"Check after the restart stopped: {line}")}


def short_id(image):
    return (image or "?").split(":")[-1][:12]


def say_reason(reason):
    if reason and reason.get("line"):
        who = reason.get("container") or ("docker compose" if reason.get("source") == "compose" else "diun-update.py")
        say(f"Vermutliche Ursache ({who}): {reason['line']}", f"Most likely cause ({who}): {reason['line']}")


# ------------------------------------------------------------ operations ----
_log_name = None         # logs/<name> that keeps the output of this run
_tee = None              # the Tee of the update or rollback that runs now (keep_output)
_recorded = set()        # operations this process put into the history


def record(entry, log=None):
    """Append to operations.jsonl (flushed to disk: what a rollback needs
    must survive a crash). log: the name under logs/ that keeps the output
    of this run (keep_log)."""
    ensure_base()
    fd = os.open(OPS, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    _recorded.add(entry.get("id"))
    if log:
        keep_log(log)


def keep_log(name):
    """From here on the output of this run goes to logs/<name> as it is
    printed, starting with what was printed before; each call flushes it to
    disk (see Tee)."""
    global _log_name
    _log_name = name
    if _tee:
        _tee.to_file(os.path.join(LOGS, name))
        _tee.sync()


class Tee:
    """sys.stdout during update and rollback: everything goes through to the
    caller as before, and a copy (up to LOG_MAX characters) is kept. Until
    the run is in the history only in memory; from then on (to_file) also in
    its log file, line by line as printed and without the @@RESULT line, so
    a killed run keeps its output up to there. The file reaches the disk
    (fsync) with every history line and at the end: after a power cut the
    lines printed since the last history line can be missing, the history
    line itself not."""
    def __init__(self, real):
        self.real, self.parts, self.size = real, [], 0
        self.fd, self.path, self.rest = None, None, ""

    def write(self, s):
        self.real.write(s)
        if self.size < LOG_MAX:
            self.parts.append(s)
            self.size += len(s)
            if self.fd is not None:
                self.spill(s)
        return len(s)

    def flush(self):
        self.real.flush()

    def __getattr__(self, name):
        return getattr(self.real, name)

    def spill(self, s):
        """Complete lines to the file; an unfinished one waits for its end."""
        text = self.rest + s
        cut = text.rfind("\n") + 1
        self.rest = text[cut:]
        lines = [ln for ln in text[:cut].splitlines(True) if not ln.startswith("@@RESULT ")]
        if lines:
            self.put("".join(lines))

    def put(self, text):
        data = text.encode("utf-8", "replace")
        while data:
            data = data[os.write(self.fd, data):]

    def to_file(self, path):
        """Write to path from now on, what was printed so far first (root
        only). A file that is there already, the output of a rollback that
        failed before, stays: this run's output follows a marker line."""
        if self.fd is not None:
            return
        ensure_base()
        self.fd, self.path = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), path
        if os.fstat(self.fd).st_size:
            with open(path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                ends = f.read(1) == b"\n"
            self.put(("" if ends else "\n") + pick("--- Erneuter Versuch ---\n", "--- Tried again ---\n"))
        self.spill("".join(self.parts))

    def sync(self):
        if self.fd is not None:
            os.fsync(self.fd)

    def close(self, keep=True):
        """End of the run: the unfinished last line and, if the output was
        cut, a note. keep=False removes the file: a run that took back what
        it did keeps no output."""
        if self.fd is None:
            return
        try:
            if keep:
                tail = "" if self.rest.startswith("@@RESULT ") else self.rest
                if self.size >= LOG_MAX:
                    tail += "\n[output cut at %d KiB]\n" % (LOG_MAX // 1024)
                if tail:
                    self.put(tail)
                os.fsync(self.fd)
        finally:
            os.close(self.fd)
            self.fd, self.rest = None, ""
            if not keep:
                try:
                    os.unlink(self.path)
                except OSError:
                    pass


def keep_output(fn, *args, **kw):
    """Run update or rollback and keep its output with the history:
    logs/<op-id>.log and logs/<op-id>.rollback.log, written as printed once
    the run is in the history (Tee). An update that stops before any change
    records nothing, so no output is kept either. A rollback tried again
    after a failure adds its output to the first one's."""
    global _log_name, _tee
    tee = Tee(sys.stdout)
    sys.stdout, _log_name, _tee = tee, None, tee
    try:
        return fn(*args, **kw)
    finally:
        sys.stdout, _tee, _log_name = tee.real, None, None
        try:
            tee.close()
        except OSError:
            pass


def cmd_log(op_id):
    """The kept output of an operation: the update, then its rollback."""
    if not OP_RE.match(op_id):
        raise Abort(64, f"Ungültige Vorgangs-ID: {op_id}", f"Invalid operation id: {op_id}")
    parts = []
    for name, head in ((op_id + ".log", None),
                       (op_id + ".rollback.log", "--- Rollback ---")):
        try:
            with open(os.path.join(LOGS, name), errors="replace") as f:
                text = f.read()
        except FileNotFoundError:
            continue
        if head:
            parts.append(head + "\n")
        parts.append(text if text.endswith("\n") else text + "\n")
    if not parts:
        raise Abort(2, "Für diesen Vorgang ist keine Ausgabe gespeichert.", "No output is kept for this operation.")
    sys.stdout.write("".join(parts))
    return 0


def repo_digests(image_id):
    """Registry digests of a local image (RepoDigests), the same values Diun
    reports for a new build. [] for images built or tagged locally."""
    try:
        img = docker_json("image", "inspect", image_id)[0]
    except (subprocess.CalledProcessError, ValueError, IndexError, OSError):
        return []
    return sorted({d.split("@", 1)[1] for d in img.get("RepoDigests") or [] if "@" in d})


def load_ops():
    """Fold operations.jsonl into {op_id: op}, later lines updating earlier ones.
    Runs that stopped before any container changed and took back what they
    did (status "discarded") are left out."""
    ops = {}
    try:
        with open(OPS) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("id"):
                    ops.setdefault(e["id"], {}).update(e)
    except FileNotFoundError:
        pass
    return {k: o for k, o in ops.items() if o.get("status") != "discarded"}


def result(**kw):
    print("@@RESULT " + json.dumps(kw, ensure_ascii=False), flush=True)


# ---------------------------------------------------------------- update ----
def cmd_update(name, do_dump=True):
    lock()
    plan, ctx = make_plan(name)
    if not plan["updatable"]:
        raise Abort(2, f"Nicht aktualisierbar: {plan['reason']}", f"Not updatable: {plan['reason']}")
    return roll_out(name, plan, ctx, do_dump)


def cmd_retag(name, new_tag, expect, do_dump=True):
    """Change the configured tag (stage 2): the plan of `retag --dry-run`
    once more, and only if it still matches what the page showed."""
    if not FP_RE.match(expect):
        raise Abort(64, f"Ungültiger Fingerabdruck: {expect}", f"Invalid fingerprint: {expect}")
    lock()
    plan, ctx = make_retag(name, new_tag)
    rt = plan["retag"]
    if not rt["possible"]:
        why = rt["reason"] + (f" ({rt['detail']})" if rt.get("detail") else "")
        raise Abort(5, f"Tag-Wechsel nicht möglich: {why}", f"Tag change not possible: {why}")
    if rt["fingerprint"] != expect:
        raise Abort(5, "Die Dateien haben sich seit der Vorschau geändert – nichts geändert, bitte neu planen.",
                    "The files changed since the preview – nothing changed, please plan again.")
    return roll_out(name, plan, ctx, do_dump, retag=rt)


def discard(op):
    """A run that stops before any container changed leaves nothing behind:
    no rollback tag, no dumps."""
    if op.get("rollback"):
        run(["docker", "rmi", op["rollback"]], check=False)
    for rel in op["dumps"]:
        try:
            remove_dump(rel)
        except (Abort, OSError):
            pass


def drop_run(op):
    """Take back what a run did before any container changed: the files of a
    tag change (from their copies), the copies, the rollback tag, the dumps.
    A run that is in the history already gets the status "discarded" there,
    which hides it, and keeps no output. If the files cannot be put back,
    everything stays: the history keeps the run, and its rollback restores
    them."""
    global _log_name
    if op.get("backup_files"):
        try:
            restore_files(op["backup_files"])
        except OSError as e:
            raise Abort(1, f"Wiederherstellen fehlgeschlagen ({e}) – die alten Dateien liegen in {op['backup']}; "
                           f"der Vorgang steht im Verlauf und lässt sich zurückrollen.",
                        f"Restoring failed ({e}) – the old files are in {op['backup']}; the operation is in the "
                        f"history and can be rolled back.")
        drop_backup(op["backup"])
    discard(op)
    if op["id"] in _recorded:
        record({"id": op["id"], "status": "discarded"})
        _log_name = None
        if _tee:
            _tee.close(keep=False)


def change_files(op, ctx, retag, services, st):
    """Step 2b of a tag change: back up the files byte-exact, put the run in
    the history, write the planned lines, check with `docker compose config`
    that the services use the new image. Anything wrong puts the files back
    and stops before a container changes (exit 5)."""
    by_file = {}
    for c in retag["changes"]:
        by_file.setdefault(c["file"], []).append(c)
    new = {f: changed_bytes(f, cs) for f, cs in by_file.items()}
    if any(v is None for v in new.values()):
        drop_run(op)
        raise Abort(5, "Die Dateien haben sich seit der Planung geändert – nichts geändert.",
                    "The files changed since planning – nothing changed.")
    op["backup"], saved = backup_files(op["project"], st, sorted(by_file))
    for x in saved:
        x["new_sha256"] = hashlib.sha256(new[x["path"]]).hexdigest()
    op["backup_files"] = saved
    say(f"Sicherung der Dateien: {op['backup']}", f"Files backed up to {op['backup']}")
    # In the history before the first file changes: should the run end here
    # for good (killed, power cut), the rollback finds the copies.
    record(op, log=op["id"] + ".log")
    try:
        for f in sorted(new):
            write_like(f, new[f])
        for c in retag["changes"]:
            say(f"{c['file']}:{c['line']}: {c['old'].strip()}  →  {c['new'].strip()}",
                f"{c['file']}:{c['line']}: {c['old'].strip()}  →  {c['new'].strip()}")
        cfg = json.loads(run(compose_cmd(ctx) + ["config", "--format", "json"]))
        svc = cfg.get("services") or {}
        wrong = [s for s in services if not same_ref((svc.get(s) or {}).get("image"), retag["image"])]
        if wrong:
            raise ValueError("services on another image: " + ", ".join(wrong))
    except (subprocess.CalledProcessError, OSError, ValueError) as e:
        detail = (getattr(e, "stderr", None) or str(e)).strip()[-300:]
        drop_run(op)
        raise Abort(5, f"Prüfung der geänderten Dateien fehlgeschlagen: {detail} – Dateien wiederhergestellt, "
                       f"nichts geändert.",
                    f"Check of the changed files failed: {detail} – files restored, nothing changed.")
    say("docker compose config: in Ordnung.", "docker compose config: OK.")


def roll_out(name, plan, ctx, do_dump, retag=None):
    """The update itself. With retag (a tag change) the configured tag is
    changed first (change_files) and the new tag is rolled out.
    The run is in the history (status "running") before the first change a
    rollback has to undo: before a tag change writes its files and before the
    containers are recreated. Whatever ends the run from then on, the entry
    stays with its rollback copy and file copies. A run that stops before any
    container changed takes back what it did (drop_run)."""
    st = stamp()
    op_id = f"{st}-{name}"
    ref = plan["image"]
    new_ref = retag["image"] if retag else ref
    repo, _tag = split_ref(ref)
    services, project = plan["services"], plan["project"]
    old_ids = {n: inspect(n)["Image"] for n in plan["containers"]}
    old_id = old_ids[name]
    op = {"id": op_id, "ts": now_iso(), "kind": "retag" if retag else "update", "container": name,
          "project": project, "workdir": ctx["workdir"], "files": ctx["files"],
          "env_files": ctx["env_files"], "services": services,
          "containers": plan["containers"], "image": ref, "old_id": old_id,
          "from_version": version_of(ref, old_id), "to_version": None,
          "rollback": None, "dumps": [], "status": "running", "pid": os.getpid()}

    if retag:
        op.update(to_image=new_ref, from_tag=retag["from_tag"], to_tag=retag["tag"],
                  changes=[{k: c[k] for k in ("file", "line", "old", "new")} for c in retag["changes"]])
        say(f"Tag-Wechsel {ref} → {new_ref} in Projekt {project}: Services {', '.join(services)}",
            f"Changing {ref} to {new_ref} in project {project}: services {', '.join(services)}")
    else:
        say(f"Update {ref} in Projekt {project}: Services {', '.join(services)}",
            f"Updating {ref} in project {project}: services {', '.join(services)}")
    if plan["config_changed"]:
        say("Hinweis: Die Compose-Datei wurde seit dem letzten Start geändert – diese Änderungen werden mit ausgerollt.",
            "Note: the compose file changed since these containers were created – those changes get rolled out too.")

    # 1. Database dumps (before anything changes)
    dumpable = [d for d in plan["dbs"] if d["kind"] in ("postgres", "mysql")]
    if do_dump and dumpable:
        need = sum(d["size"] or 0 for d in dumpable) + FREE_MARGIN
        if plan["free_bytes"] is not None and plan["free_bytes"] < need:
            raise Abort(5, f"Zu wenig Platz in {BASE}: {human(plan['free_bytes'])} frei, {human(need)} nötig.",
                        f"Not enough space in {BASE}: {human(plan['free_bytes'])} free, {human(need)} needed.")
        try:
            for d in dumpable:
                op["dumps"].append(dump_db(project, d, st))
        except Abort:
            # An aborted update leaves nothing behind, not even the dumps
            # of the other databases that worked.
            discard(op)
            raise
    elif do_dump:
        say("Kein unterstützter Datenbank-Container im Projekt – kein Dump.",
            "No supported database container in this project – no dump.")
    else:
        say("DB-Dump auf Wunsch übersprungen.", "Database dump skipped on request.")
    for d in plan["dbs"]:
        if d["kind"] == "unsupported":
            say(f"Kein Dump für {d['name']} (Typ nicht unterstützt).",
                f"No dump for {d['name']} (type not supported).")

    # Up to the recreated containers, anything unexpected takes back what the
    # run did, as a planned stop does.
    try:
        # 2. Rollback tag for the image the container runs on now
        rb = f"{repo}:rollback-{st}"
        run(["docker", "tag", old_id, rb])
        op["rollback"] = rb
        say(f"Rollback-Sicherung: {rb}", f"Rollback image: {rb}")

        # 2b. Tag change: back up the files, write the planned lines, check them
        if retag:
            change_files(op, ctx, retag, services, st)

        # 3. Pull. Without --ignore-pull-failures: compose then exits 0 when the
        # registry fails, and that looked like "no newer image". A failed pull
        # goes on only with the image to roll out on the server already.
        base = compose_cmd(ctx)
        pull_rc = stream(base + ["pull", *services])
        pull_error = (reason_line(STREAM_TAIL) or f"exit {pull_rc}") if pull_rc else None
        new_id = image_id(new_ref)
        if retag and not new_id:
            drop_run(op)
            raise Abort(5, f"Das Image {new_ref} ist nicht verfügbar – Dateien wiederhergestellt, nichts geändert.",
                        f"The image {new_ref} is not available – files restored, nothing changed.")
        unchanged = bool(new_id) and all(v == new_id for v in old_ids.values())
        if pull_rc != 0:
            # A pull that failed and left the old image says nothing about
            # newer ones: not "no newer image".
            if not retag and (unchanged or not new_id):
                drop_run(op)
                raise Abort(5, f"Der Pull ist fehlgeschlagen, und auf dem Server liegt kein neueres Image – nichts "
                               f"geändert. Pull: {pull_error}",
                            f"The pull failed and there is no newer image on the server – nothing changed. "
                            f"Pull: {pull_error}")
            say(f"Warnung: Der Pull ist fehlgeschlagen ({pull_error}). {new_ref} liegt schon auf dem Server "
                f"({short_id(new_id)}) und wird ausgerollt.",
                f"Warning: the pull failed ({pull_error}). {new_ref} is on the server already "
                f"({short_id(new_id)}) and is rolled out.")
        # A tag change always recreates: the containers must carry the new tag.
        if not retag and unchanged:
            run(["docker", "rmi", rb], check=False)
            op.update(status="nochange", rollback=None, new_id=new_id, to_version=op["from_version"])
            record(op, log=op_id + ".log")
            say("Kein neueres Image verfügbar – nichts geändert.", "No newer image available – nothing changed.")
            result(op=op_id, status="nochange", dumps=op["dumps"],
                   from_version=op["from_version"], to_version=op["to_version"])
            return 0
        op["new_id"] = new_id
        op["new_digests"] = repo_digests(new_id)
        op["to_version"] = version_of(new_ref, new_id)
        # In the history before the containers change (see above).
        record(op, log=op_id + ".log")
    except Abort:
        raise
    except BaseException:
        drop_run(op)
        raise
    versions = {"from_version": op["from_version"], "to_version": op["to_version"]}
    if retag:
        versions.update(image=new_ref, backup=op["backup"])

    # 4. Recreate only these services, then watch those that depend on them.
    # Whatever goes wrong from here on fails the update, with its rollback.
    ok = False
    try:
        watched = running_state([c for d in plan["dependents"] for c in d["containers"]])
        rc = stream(base + ["up", "-d", "--no-deps", "--pull", "never", *services])
        ok, failed, _states = (False, [], {}) if rc != 0 else wait_healthy(project, services)
        if ok:
            failed = watch_dependents(watched)
            ok = not failed
        if not ok:
            show_logs(failed or plan["containers"])
            op["reason"] = failure_reason(failed or plan["containers"], compose_rc=rc)
    except Exception as e:
        ok = False
        op["reason"] = error_reason(e)
    if not ok:
        op["status"] = "failed"
        record(op, log=op_id + ".log")
        say_reason(op["reason"])
        if retag:
            say("Tag-Wechsel fehlgeschlagen. Zurückrollen ist möglich (stellt auch die Dateien wieder her).",
                "Tag change failed. A rollback is possible (it restores the files too).")
        else:
            say("Update fehlgeschlagen. Zurückrollen ist möglich.", "Update failed. A rollback is possible.")
        result(op=op_id, status="failed", rollback=rb, dumps=op["dumps"], reason=op["reason"], **versions)
        return 4
    op["status"] = "ok"
    record(op, log=op_id + ".log")
    if retag:
        say(f"Tag-Wechsel erfolgreich: {new_ref}", f"Tag change successful: {new_ref}")
    else:
        say("Update erfolgreich.", "Update successful.")

    # 5. + 6. Never let these turn a successful update into a failure.
    cleanup_after_update(project, repo)
    start_radar()
    result(op=op_id, status="ok", rollback=rb, dumps=op["dumps"], **versions)
    return 0


def compose_images(ctx, services):
    """{service: image} as `docker compose config` gives it now; exit 5 if
    compose cannot read its files."""
    try:
        cfg = json.loads(run(compose_cmd(ctx) + ["config", "--format", "json"]))
    except (subprocess.CalledProcessError, ValueError, OSError) as e:
        detail = (getattr(e, "stderr", None) or str(e)).strip()[-300:]
        raise Abort(5, f"docker compose config schlägt fehl: {detail} – nichts geändert.",
                    f"docker compose config fails: {detail} – nothing changed.")
    svc = cfg.get("services") or {}
    return {s: (svc.get(s) or {}).get("image") for s in services}


def other_image(ctx, op):
    """Exit 5 unless the compose files give every service of the operation its
    image: otherwise compose would start something else than the rollback
    copy, which takes only that tag (an image line changed after the update,
    a .env edited, a service removed)."""
    wrong = [f"{s}: {img or '–'}" for s, img in sorted(compose_images(ctx, op["services"]).items())
             if not same_ref(img, op["image"])]
    if wrong:
        raise Abort(5, f"Die Compose-Dateien geben jetzt {', '.join(wrong)} statt {op['image']} – nichts geändert: "
                       f"Compose würde nicht die Rollback-Kopie starten. Die Dateien zurückändern oder von Hand "
                       f"zurückrollen.",
                    f"The compose files now give {', '.join(wrong)} instead of {op['image']} – nothing changed: "
                    f"compose would not start the rollback copy. Change the files back, or roll back by hand.")


def not_on(project, services, image):
    """Containers of these services (one-off `compose run` ones aside) that do
    not run the image ID `image`: [(name, image ID or None)]."""
    off = []
    for s in services:
        for n in service_containers(project, s):
            try:
                c = docker_json("inspect", "--type", "container", n)[0]
            except (subprocess.CalledProcessError, ValueError, IndexError, OSError):
                off.append((n, None))
                continue
            if (c["Config"].get("Labels") or {}).get("com.docker.compose.oneoff") == "True":
                continue
            if c.get("Image") != image:
                off.append((n, c.get("Image")))
    return off


def cmd_rollback(op_id):
    if not OP_RE.match(op_id):
        raise Abort(64, f"Ungültige Vorgangs-ID: {op_id}", f"Invalid operation id: {op_id}")
    lock()
    op = load_ops().get(op_id)
    if not op or op.get("kind") not in ("update", "retag"):
        raise Abort(2, f"Vorgang {op_id} nicht gefunden.", f"Operation {op_id} not found.")
    # A rollback that went through is not repeated; one that failed may run
    # again (up to 0.18.0 a failed one was marked rolled_back as well).
    if op.get("rolled_back") and op.get("rollback_status") != "failed":
        raise Abort(3, "Dieser Vorgang wurde bereits zurückgerollt.", "This operation was already rolled back.")
    rb = op.get("rollback")
    rb_id = image_id(rb) if rb else ""
    if not rb_id:
        raise Abort(2, "Das Rollback-Image existiert nicht mehr.", "The rollback image no longer exists.")
    ctx = {"project": op["project"], "workdir": op["workdir"], "files": op["files"],
           "env_files": op.get("env_files") or []}
    saved = (op.get("backup_files") or []) if op.get("kind") == "retag" else []
    # A tag change is undone with its files. Check them all before touching one:
    # a file edited since then, or a damaged backup, stops the rollback. A file
    # that is back already (an earlier attempt, a run that ended before writing
    # it) stays as it is.
    pending = []
    for x in saved:
        try:
            now_sum, bk_sum = sha256_of(x["path"]), sha256_of(x["backup"])
        except OSError as e:
            raise Abort(2, f"Datei oder Sicherung nicht lesbar: {e}", f"File or backup not readable: {e}")
        if bk_sum != x["sha256"]:
            raise Abort(2, f"Die Sicherung {x['backup']} ist beschädigt – nichts geändert.",
                        f"The backup {x['backup']} is damaged – nothing changed.")
        if now_sum == x.get("new_sha256"):
            pending.append(x)
        elif now_sum != x["sha256"]:
            raise Abort(5, f"{x['path']} wurde nach dem Tag-Wechsel geändert – nichts geändert. Die alte Datei "
                           f"liegt in {x['backup']}.",
                        f"{x['path']} changed after the tag change – nothing changed. The old file is in {x['backup']}.")
    # The files must give the services the operation's image (other_image):
    # without a tag change checked before anything changes, with one once its
    # files are back, which are written again as they were if not.
    if not saved:
        other_image(ctx, op)
    say(f"Rollback von {op['image']} auf {rb} (Projekt {op['project']}) …",
        f"Rolling {op['image']} back to {rb} (project {op['project']}) …")
    say("Achtung: Datenbanken werden NICHT zurückgesetzt. Bei Migrationsproblemen den Dump einspielen.",
        "Note: databases are NOT restored. If migrations cause trouble, restore the dump.")
    if saved:
        now = {}
        for x in pending:
            with open(os.path.realpath(x["path"]), "rb") as f:
                now[x["path"]] = f.read()
        restore_files(pending)
        try:
            other_image(ctx, op)
        except Abort:
            for path, data in now.items():
                write_like(path, data)
            raise
        for x in pending:
            say(f"Wiederhergestellt: {x['path']}", f"Restored: {x['path']}")
    # The containers change from here on: the output goes to the log as printed.
    keep_log(op_id + ".rollback.log")
    run(["docker", "tag", rb, op["image"]])
    ok, reason = False, None
    try:
        rc = stream(compose_cmd(ctx) + ["up", "-d", "--no-deps", "--pull", "never", *op["services"]])
        ok, failed, _ = (False, [], {}) if rc != 0 else wait_healthy(op["project"], op["services"])
        if ok:
            # What runs must be the rollback copy, not what else a tag names.
            off = not_on(op["project"], op["services"], rb_id)
            if off:
                ok = False
                n, img = off[0]
                reason = {"container": n, "state": None, "exit_code": None, "oom": False, "source": "image",
                          "line": pick(f"läuft auf {short_id(img)} statt auf der Rollback-Kopie {rb} "
                                       f"({short_id(rb_id)})",
                                       f"runs {short_id(img)} instead of the rollback copy {rb} ({short_id(rb_id)})")}
        else:
            show_logs(failed or op["containers"])
            reason = failure_reason(failed or op["containers"], compose_rc=rc)
    except Exception as e:
        ok, reason = False, error_reason(e)
    status = "ok" if ok else "failed"
    entry = {"id": op_id, "rolled_back": ok, "rollback_ts": now_iso(), "rollback_status": status,
             "rollback_reason": reason}
    record(entry, log=op_id + ".rollback.log")
    if not ok:
        say_reason(reason)
        say("Rollback fehlgeschlagen. Er lässt sich wiederholen, sobald die Ursache behoben ist.",
            "Rollback failed. It can be tried again once the cause is fixed.")
        result(op=op_id, status="failed", reason=reason)
        return 4
    say("Rollback erfolgreich.", "Rollback successful.")
    start_radar()
    result(op=op_id, status="ok")
    return 0


# --------------------------------------------------------------- backups ----
def list_dumps():
    """Dump files dumps/<project>/<stamp>_<container>.sql.gz, newest first per project."""
    dumps = []
    if not os.path.isdir(DUMPS):
        return dumps
    for project in sorted(os.listdir(DUMPS)):
        pdir = os.path.join(DUMPS, project)
        if not os.path.isdir(pdir):
            continue
        for f in sorted(os.listdir(pdir), reverse=True):
            if not f.endswith(".sql.gz"):
                continue
            p = os.path.join(pdir, f)
            st, _, rest = f.partition("_")
            dumps.append({"path": f"{project}/{f}", "project": project,
                          "container": rest[:-len(".sql.gz")], "size": os.path.getsize(p),
                          "mtime": int(os.path.getmtime(p))})
    return dumps


CREATED_RE = re.compile(r"(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d) ([+-]\d\d)(\d\d)\b")


def iso_time(created):
    """'2026-10-12 03:15:00 +0200 CEST' (docker image ls) -> '2026-10-12T03:15:00+02:00', else ''."""
    m = CREATED_RE.match(created or "")
    return f"{m.group(1)}T{m.group(2)}{m.group(3)}:{m.group(4)}" if m else ""


def image_sizes(ids):
    """Size in bytes of local images by ID. An image removed in the meantime is
    missing; Docker still prints the others (and exits 1)."""
    if not ids:
        return {}
    try:
        out = run(["docker", "image", "inspect", *sorted(ids)])
    except subprocess.CalledProcessError as e:
        out = e.stdout or ""
    try:
        return {i["Id"]: i["Size"] for i in json.loads(out or "[]") if isinstance(i.get("Size"), int)}
    except (ValueError, TypeError, KeyError, AttributeError):
        return {}


def rollback_images():
    """Local <repo>:rollback-<stamp> tags, newest first. in_use = a container
    (running or stopped) still uses the image; size in bytes (None if Docker
    does not say), created as ISO 8601."""
    in_use = set(run(["docker", "ps", "-a", "--format", "{{.ID}}"]).split())
    used_ids = set()
    if in_use:
        used_ids = {c["Image"] for c in docker_json("inspect", *in_use)}
    images = []
    for line in run(["docker", "image", "ls", "--no-trunc", "--format", "{{json .}}"]).splitlines():
        try:
            i = json.loads(line)
        except ValueError:
            continue
        if ROLLBACK_TAG_RE.match(i.get("Tag", "")):
            images.append({"ref": f"{i['Repository']}:{i['Tag']}", "id": i["ID"],
                           "created": iso_time(i.get("CreatedAt", "")), "in_use": i["ID"] in used_ids})
    sizes = image_sizes({i["id"] for i in images})
    for i in images:
        i["size"] = sizes.get(i["id"])
    images.sort(key=lambda x: x["ref"].rsplit(":", 1)[1], reverse=True)
    return images


def helper_alive(pid):
    """True if the process `pid` is this helper (an update still running)."""
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            return b"diun-update" in f.read()
    except (OSError, TypeError, ValueError):
        return False


def cmd_backups():
    ensure_base()
    dumps = list_dumps()
    images = rollback_images()
    rb_refs = {i["ref"] for i in images}
    all_ops = load_ops()
    known = radar_versions()

    ops = sorted((dict(o) for o in all_ops.values()), key=lambda o: o.get("id", ""), reverse=True)[:30]
    for o in ops:
        # "running": a run that ended without its last line (killed, power
        # cut) unless its process still runs. A failed rollback may be tried again.
        o["active"] = o.get("status") == "running" and helper_alive(o.get("pid"))
        o["can_rollback"] = (o.get("kind") in ("update", "retag") and o.get("status") in ("ok", "failed", "running")
                             and not o["active"]
                             and not (o.get("rolled_back") and o.get("rollback_status") != "failed")
                             and norm_ref(o.get("rollback") or "") in rb_refs)
        for k, iid in (("from_version", o.get("old_id")), ("to_version", o.get("new_id"))):
            if refines(known.get(iid), o.get(k)):
                o[k] = known[iid]
        for k in ("workdir", "files", "env_files", "old_id", "new_id", "backup_files", "pid"):
            o.pop(k, None)
        o["log"] = os.path.exists(os.path.join(LOGS, o["id"] + ".log"))
        o["rollback_log"] = os.path.exists(os.path.join(LOGS, o["id"] + ".rollback.log"))

    # What the cleanup rules would delete right now (preview for the tab).
    cleanup = {"keep_dumps": None, "image_days": None, "file": None, "error": None,
               "dumps": [], "images": [], "bytes": 0}
    try:
        rules = load_rules()
        del_dumps, del_images = cleanup_plan(rules, dumps, images, all_ops)
        cleanup.update(keep_dumps=rules["keep_dumps"], image_days=rules["image_days"], file=rules["file"],
                       dumps=[d["path"] for d in del_dumps], images=[i["ref"] for i in del_images],
                       bytes=sum(d["size"] for d in del_dumps))
    except Abort as e:
        cleanup["error"] = str(e)

    du = shutil.disk_usage(BASE)
    print(json.dumps({"dumps": dumps, "images": images, "ops": ops, "cleanup": cleanup,
                      "dump_bytes": sum(d["size"] for d in dumps),
                      "free_bytes": du.free, "dir": BASE}, ensure_ascii=False))
    return 0


def remove_dump(rel):
    """Delete one dump; rel must be <project>/<stamp>_<container>.sql.gz below DUMPS."""
    if not DUMP_RE.match(rel):
        raise Abort(64, f"Ungültiger Dump-Name: {rel}", f"Invalid dump name: {rel}")
    path = os.path.realpath(os.path.join(DUMPS, rel))
    if not path.startswith(os.path.realpath(DUMPS) + os.sep) or not os.path.isfile(path):
        raise Abort(2, f"Dump {rel} nicht gefunden.", f"Dump {rel} not found.")
    os.unlink(path)
    pdir = os.path.dirname(path)
    if not os.listdir(pdir):
        os.rmdir(pdir)


def cmd_delete_dump(rel):
    remove_dump(rel)
    say(f"Gelöscht: {rel}", f"Deleted: {rel}")
    return 0


def cmd_delete_image(ref):
    repo, tag = split_ref(ref)
    if not ROLLBACK_TAG_RE.match(tag) or not repo or "@" in ref or ref != f"{repo}:{tag}":
        raise Abort(64, f"Kein Rollback-Image: {ref}", f"Not a rollback image: {ref}")
    if not image_id(ref):
        raise Abort(2, f"{ref} nicht gefunden.", f"{ref} not found.")
    # Without -f: Docker refuses if a container still uses the image.
    return stream(["docker", "rmi", ref])


# --------------------------------------------------------------- cleanup ----
# KEY in /etc/image-updates.conf -> (rule, default). 0 switches a rule off.
RULES = {"KEEP_DUMPS": ("keep_dumps", 3), "ROLLBACK_IMAGE_DAYS": ("image_days", 14)}
RULE_VALUE_RE = re.compile(r"^\d{1,4}$")


def load_rules():
    """Cleanup rules: the defaults, overridden by /etc/image-updates.conf
    (KEY=VALUE lines, # comments). The file must belong to root and be
    writable by root only. Anything not understood stops the cleanup instead
    of guessing - a typo must never delete more than intended."""
    rules = {name: default for name, default in RULES.values()}
    rules["file"] = None
    try:
        st = os.stat(CONF)
    except FileNotFoundError:
        return rules
    if st.st_uid not in (0, os.geteuid()) or st.st_mode & 0o022:
        raise Abort(2, f"{CONF} muss root gehören und darf nur für root schreibbar sein – nichts gelöscht.",
                    f"{CONF} must belong to root and be writable by root only – nothing deleted.")
    with open(CONF) as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = (x.strip() for x in line.partition("="))
            value = value.strip("\"'")
            if not sep or key not in RULES or not RULE_VALUE_RE.match(value):
                raise Abort(2, f"{CONF}, Zeile {n} nicht verstanden: {line[:60]} – nichts gelöscht.",
                            f"{CONF}, line {n} not understood: {line[:60]} – nothing deleted.")
            rules[RULES[key][0]] = int(value)
    rules["file"] = CONF
    return rules


def cleanup_plan(rules, dumps, images, ops, project=None, repo=None, now=None):
    """What the rules delete: (dumps, images), entries of list_dumps() and
    rollback_images(). Never selected:
      - the newest keep_dumps dumps of every database container
      - the dumps of each project's last update
      - files not named like the add-on's dumps
      - rollback images a container uses, or younger than image_days
    project / repo narrow it down to one update's project and image."""
    del_dumps, del_images = [], []
    if rules["keep_dumps"] > 0:
        last = {}
        for o in ops.values():
            p = o.get("project")
            if o.get("kind") in ("update", "retag") and p and o.get("id", "") > last.get(p, {}).get("id", ""):
                last[p] = o
        protected = {d for o in last.values() for d in o.get("dumps") or []}
        groups = {}
        for d in dumps:
            if DUMP_RE.match(d["path"]) and project in (None, d["project"]):
                groups.setdefault((d["project"], d["container"]), []).append(d)
        for group in groups.values():
            group.sort(key=lambda d: d["path"], reverse=True)
            del_dumps += [d for d in group[rules["keep_dumps"]:] if d["path"] not in protected]
    if rules["image_days"] > 0:
        limit = (now or datetime.now()) - timedelta(days=rules["image_days"])
        for i in images:
            i_repo, tag = split_ref(i["ref"])
            if i["in_use"] or (repo is not None and i_repo != norm_ref(repo)):
                continue
            try:
                made = datetime.strptime(tag[len("rollback-"):], "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            if made < limit:
                del_images.append(i)
    return del_dumps, del_images


def rules_text(rules):
    """The rules in one line, as (de, en) for say()."""
    keep, days, src = rules["keep_dumps"], rules["image_days"], rules["file"]
    de = [f"die letzten {keep} Dumps je Datenbank-Container behalten" if keep else "Dumps nie löschen",
          f"ungenutzte Rollback-Images nach {days} Tagen entfernen" if days else "Rollback-Images nie entfernen"]
    en = [f"keep the last {keep} dumps per database container" if keep else "never delete dumps",
          f"remove unused rollback images after {days} days" if days else "never remove rollback images"]
    return (f"Regeln ({src or 'Standard'}): " + ", ".join(de) + ".",
            f"Rules ({src or 'defaults'}): " + ", ".join(en) + ".")


def apply_cleanup(dumps, images):
    """Delete the selected dumps and rollback images; one line per item.
    Returns (deleted dump paths, removed image refs, freed dump bytes, failures)."""
    gone_dumps, gone_images, freed, failed = [], [], 0, []
    for d in dumps:
        try:
            remove_dump(d["path"])
        except (Abort, OSError) as e:
            failed.append(d["path"])
            say(f"Nicht gelöscht: {d['path']} ({e})", f"Not deleted: {d['path']} ({e})")
            continue
        gone_dumps.append(d["path"])
        freed += d["size"]
        say(f"Dump gelöscht: {d['path']} ({human(d['size'])})", f"Deleted dump {d['path']} ({human(d['size'])})")
    for i in images:
        # Without -f: Docker refuses if a container started using it meanwhile.
        try:
            run(["docker", "rmi", i["ref"]], timeout=120)
        except subprocess.CalledProcessError as e:
            failed.append(i["ref"])
            err = (e.stderr or "").strip()[-200:]
            say(f"Nicht entfernt: {i['ref']} ({err})", f"Not removed: {i['ref']} ({err})")
            continue
        gone_images.append(i["ref"])
        say(f"Rollback-Image entfernt: {i['ref']}", f"Removed rollback image {i['ref']}")
    return gone_dumps, gone_images, freed, failed


def cmd_cleanup(dry=False):
    rules = load_rules()
    if not dry:
        lock()
    say(*rules_text(rules))
    dumps, images = cleanup_plan(rules, list_dumps(), rollback_images(), load_ops())
    if not dumps and not images:
        say("Nichts aufzuräumen.", "Nothing to clean up.")
        if not dry:
            result(status="nochange", dumps=[], images=[], bytes=0, failed=[])
        return 0
    if dry:
        for d in dumps:
            say(f"Würde löschen: {d['path']} ({human(d['size'])})", f"Would delete {d['path']} ({human(d['size'])})")
        for i in images:
            say(f"Würde entfernen: {i['ref']}", f"Would remove {i['ref']}")
        return 0
    gone_dumps, gone_images, freed, failed = apply_cleanup(dumps, images)
    say(f"Fertig: {len(gone_dumps)} Dumps ({human(freed)}), {len(gone_images)} Rollback-Images entfernt.",
        f"Done: {len(gone_dumps)} dumps ({human(freed)}), {len(gone_images)} rollback images removed.")
    result(status="ok", dumps=gone_dumps, images=gone_images, bytes=freed, failed=failed)
    return 0


def cleanup_after_update(project, repo):
    """The same rules after a successful update, limited to its project's dumps
    and its image's rollback copies. A problem here is reported, never fatal."""
    try:
        rules = load_rules()
        dumps, images = cleanup_plan(rules, list_dumps(), rollback_images(), load_ops(),
                                     project=project, repo=repo)
        if dumps or images:
            say("Aufräumen nach den Regeln …", "Cleaning up by the rules …")
            apply_cleanup(dumps, images)
    except Abort as e:
        say(f"Aufräumen übersprungen: {e}", f"Cleanup skipped: {e}")
    except Exception as e:  # never turn a successful update into a failure
        say(f"Aufräumen übersprungen: {e}", f"Cleanup skipped: {e}")


# ----------------------------------------------------------------- radar ----
def cmd_radar():
    """Run the version radar now and wait for it. systemd runs the oneshot
    service once at a time: a second start joins the run in progress."""
    if not radar_installed():
        raise Abort(2, "Der Versions-Radar ist nicht eingerichtet.", "The version radar is not set up.")
    say("Versions-Radar läuft …", "Version radar running …")
    try:
        p = systemctl("start", RADAR_UNIT, timeout=20 * 60)
    except subprocess.TimeoutExpired:
        raise Abort(1, "Der Versions-Radar antwortet nicht (journalctl -u diun-radar.service).",
                    "The version radar does not finish (journalctl -u diun-radar.service).")
    if p.returncode != 0:
        print((p.stderr or "").strip(), flush=True)
        raise Abort(1, "Der Radar-Lauf ist fehlgeschlagen (journalctl -u diun-radar.service).",
                    "The radar run failed (journalctl -u diun-radar.service).")
    say("Versions-Radar fertig.", "Version radar finished.")
    return 0


# ------------------------------------------------------------------ main ----
def main(argv):
    if os.geteuid() != 0:
        raise Abort(77, "Dafür werden Administratorrechte benötigt.", "This requires administrator privileges.")
    if len(argv) == 2 and argv[0] == "plan":
        plan, _ = make_plan(argv[1])
        print(json.dumps(plan, ensure_ascii=False))
        return 0
    if len(argv) in (2, 3) and argv[0] == "update" and (len(argv) == 2 or argv[2] == "--no-dump"):
        return keep_output(cmd_update, argv[1], do_dump=len(argv) == 2)
    if len(argv) == 2 and argv[0] == "rollback":
        return keep_output(cmd_rollback, argv[1])
    if len(argv) == 2 and argv[0] == "log":
        return cmd_log(argv[1])
    if argv == ["backups"]:
        return cmd_backups()
    if len(argv) == 2 and argv[0] == "delete-dump":
        return cmd_delete_dump(argv[1])
    if len(argv) == 2 and argv[0] == "delete-image":
        return cmd_delete_image(argv[1])
    if argv in (["cleanup"], ["cleanup", "--dry-run"]):
        return cmd_cleanup(dry=len(argv) == 2)
    if argv == ["radar"]:
        return cmd_radar()
    if len(argv) == 4 and argv[0] == "retag" and argv[3] == "--dry-run":
        print(json.dumps(make_retag(argv[1], argv[2])[0], ensure_ascii=False))
        return 0
    if len(argv) in (5, 6) and argv[0] == "retag" and argv[3] == "--expect" and argv[5:] in ([], ["--no-dump"]):
        return keep_output(cmd_retag, argv[1], argv[2], argv[4], do_dump=len(argv) == 5)
    raise Abort(64, "Unbekannter Befehl: " + " ".join(argv), "Unknown command: " + " ".join(argv))


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Abort as e:
        print(str(e), flush=True)
        sys.exit(e.code)
    except subprocess.CalledProcessError as e:
        print((e.stderr or str(e)).strip(), flush=True)
        sys.exit(1)

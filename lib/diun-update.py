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
                                recreate the same services (data is NOT restored,
                                except a key-value store's copy of a tag change)
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
  status [--lang en|de]         read-only, also without root: the add-on's
                                status for the Status Board (schema 1, see
                                diunstatus), from /var/lib/diun-radar/status.json
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
                                A newer major of a key-value store (Redis,
                                Valkey, KeyDB) is a tag change as well: before
                                the restart the store is stopped and its data
                                directory packed into data/<project>/; the
                                rollback puts that copy back.

  dbmajor <container> <tag> --dry-run
                                read-only: the plan of a database major
                                (stage 5, PostgreSQL): the tag change to the
                                new major, where the data lies, the services
                                that wait for the database, the extensions in
                                use, and a fingerprint; or why not
  dbmajor <container> <tag> --expect <fingerprint>
                                the major itself (dump and restore): the new
                                image, the rollback tag, the waiting services
                                stopped, a dump, every table counted, the tag
                                line written, the database stopped and its data
                                moved aside (kept until released), the new
                                major started on the empty data directory, the
                                dump restored, the counts compared, the waiting
                                services started. A rollback moves the data
                                back (and keeps the new major's).
  rebuild <container> --dry-run read-only: the plan of a rebuild of an image built
                                on this server (compose `build:`)
  rebuild <container> [--no-dump]
                                an update for such an image: dumps, rollback tag,
                                `docker compose build --pull` instead of the pull
                                (a failed build stops here, exit 5), then the
                                update steps 4 to 6
  delete-olddata <op-id>        release the data a database major kept: the
                                old major's after a good run, the new major's
                                after a rollback

Only retag and dbmajor change the compose or .env files, and only the lines
they showed. Images built locally or pinned by digest are not updated.

Everything the addon writes lives in /var/backups/image-updates (root only):
  dumps/<project>/<stamp>_<container>.sql.gz
  compose/<project>/<stamp>/    the files a tag change wrote, as they were
  data/<project>/<stamp>_<container>.tar
                                a key-value store's data before its new major,
                                removed together with the rollback image
  operations.jsonl              one JSON line per update / tag change / rollback
  logs/<op-id>.log              output of the update (.rollback.log: of its rollback)

The machine-readable result of update/rollback/cleanup is the last line:
  @@RESULT {"op": "...", "status": "ok|nochange|failed", ...}
After them (also after a failure) status.json is written anew, silently.

SIGTERM, SIGHUP and SIGINT end a run through its cleanup (entry, diunproc):
the docker command running is stopped, a run that changed no container yet
takes back what it did (exit 5), one that did is recorded as failed with its
rollback (exit 4), and only then is the lock free. SIGKILL cannot be caught:
see diunproc and docs/INTERNALS.md.

Lives in /usr/lib/cockpit-image-updates (root-owned, not mounted into any
container). DIUN_LANG=de|en selects the language of the log messages.
"""

import fcntl
import gzip
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from diunlib import CI_JOB_RE, CI_RUNNER_RE, core, is_ci_job, local_version, nums_of, tag_digits  # noqa: E402
from diunproc import Interrupted, Timeout, copy as copy_out, held, on_signals, stream as stream_out  # noqa: E402
import diunstatus  # noqa: E402

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
LANG_ARG_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})?$")   # status --lang: en, de, de-DE, pt_BR
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
def run(cmd, check=True, capture=True, timeout=600, data=None):
    p = subprocess.run(cmd, input=data, capture_output=capture, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, cmd, p.stdout, p.stderr)
    return p.stdout if capture else ""


STREAM_TAIL = []          # last lines of the last stream() call, for the failure reason
# Time limits in seconds (DOCKER_TIMEOUT, DUMP_TIMEOUT in /etc/image-updates.conf, load_limits):
# one docker / docker compose command that streams (pull, up, rmi), one database dump.
LIMITS = {"docker": 1800, "dump": 14400}


def stream(cmd):
    """Run a docker command, pass its output through to our stdout line by
    line and keep the last lines in STREAM_TAIL. Ends after LIMITS["docker"]
    seconds with diunproc.Timeout (the command stopped, STREAM_TAIL filled)."""
    print("+ " + " ".join(cmd), flush=True)
    try:
        rc, tail = stream_out(cmd, LIMITS["docker"], sys.stdout, keep_fds=lock_fds())
    except Timeout as e:
        STREAM_TAIL[:] = e.tail
        raise
    STREAM_TAIL[:] = tail
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


def lock_fds():
    """The lock for the docker commands a run starts (diunproc keep_fds):
    should the helper be killed with SIGKILL, the lock stays held until they
    have ended as well."""
    return (_lock_fh.fileno(),) if _lock_fh else ()


def ensure_base():
    for d in (BASE, DUMPS, LOGS):
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)


def free_space(path):
    """Free bytes where path is, or will be once ensure_base makes it: on the
    nearest folder of it that exists. Debian has /var/backups, the RHEL
    rebuilds do not, so there neither BASE nor its parent exists before the
    first update."""
    path = os.path.abspath(path)
    while not os.path.isdir(path):
        path = os.path.dirname(path)
    return shutil.disk_usage(path).free


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


def built_version(ref, image_id):
    """For an image built on this server: the version its final base brings,
    from the build's variables (caddy:2 -> CADDY_VERSION=2.11.4), the base
    named by the radar (versions.json "base"). None if unknown."""
    for b in (radar_record(ref) or {}).get("base") or []:
        if b.get("final") and b.get("image") and b.get("tag") and image_id:
            try:
                return local_version(b["image"], b["tag"], docker_json("image", "inspect", image_id)[0])[0]
            except Exception:
                return None
    return None


def built_target(ref):
    """What a rebuild brings, as far as the radar knows: the version the final
    base's tag points to now. None if unknown."""
    for b in (radar_record(ref) or {}).get("base") or []:
        if b.get("final"):
            return b.get("now") or None
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


def compose_cmd(ctx, files=None):
    """files: other compose files than the project's ("-": standard input)."""
    cmd = ["docker", "compose", "-p", ctx["project"], "--project-directory", ctx["workdir"]]
    for f in ctx["files"] if files is None else files:
        cmd += ["-f", f]
    for e in ctx["env_files"]:
        if os.path.exists(e):
            cmd += ["--env-file", e]
    return cmd


def config_changed(ctx, services, same, resolved):
    """Has the configuration of these containers changed since compose created
    them? `docker compose config --hash` against their L_HASH. Before compose
    5.5.0 that command hashed a service without its env_file merged into the
    environment, unlike `up` (docker/compose#14001), and every service with an
    env_file looked changed. A service whose hash differs is hashed once more
    from `resolved`, the output of `config` (the environment merged, every `$`
    escaped by compose) given as the compose file; it has changed only if that
    hash differs as well."""
    def differ(out, among):
        current = dict(line.split()[:2] for line in out.splitlines() if len(line.split()) >= 2)
        labels = [m["Config"]["Labels"] or {} for m in same]
        return sorted({lbl.get(L_SERVICE) for lbl in labels if lbl.get(L_SERVICE) in among
                       and current.get(lbl.get(L_SERVICE)) not in (None, lbl.get(L_HASH))})

    changed = differ(run(compose_cmd(ctx) + ["config", "--hash", ",".join(services)]), services)
    if changed:
        try:
            changed = differ(run(compose_cmd(ctx, files=["-"]) + ["config", "--hash", ",".join(changed)],
                                 data=resolved), changed)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            pass        # the first answer stands
    return bool(changed)


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
def make_plan(name, build=False):
    """build: the plan of a rebuild (`rebuild`): only for services built on
    this server, which an update refuses as "built-locally"."""
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
        resolved = run(compose_cmd(ctx) + ["config", "--format", "json"])
        cfg = json.loads(resolved)
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
    built = [s for s in services if ((cfg.get("services") or {}).get(s) or {}).get("build") is not None]
    plan["build"] = bool(build)
    if built and not build:
        plan["reason"] = "built-locally"
        return plan, ctx
    if build and len(built) != len(services):
        plan["reason"] = "not-built"
        return plan, ctx
    relations(plan, cfg, members, same, services, ctx["project"])

    # Has the compose file changed since these containers were created?
    try:
        plan["config_changed"] = config_changed(ctx, services, same, resolved)
    except Exception:
        pass

    tagid = image_id(ref)
    plan["pulled"] = bool(tagid) and tagid != c["Image"]

    # A tag that fixes no version number (latest, stable …) follows every
    # release. If the running version is unknown too, nobody can tell how big
    # the jump is: the plugin warns and asks for an extra confirmation.
    plan["installed_version"] = version_of(ref, c["Image"]) or (built_version(ref, c["Image"]) if build else None)
    plan["tag_digits"] = tag_digits(split_ref(ref)[1])
    # A rebuild builds the same Dockerfile again: no jump of a tag to warn of.
    plan["jump_unknown"] = plan["installed_version"] is None and plan["tag_digits"] == 0 and not build

    # What the update brings: the image pulled already, otherwise what the
    # tag points to in the registry right now (radar, "tag_now") - if that
    # counts in the same numbering as the installed version.
    if build:
        target = built_target(ref)
    elif plan["pulled"]:
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

    plan["free_bytes"] = free_space(BASE)
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


def make_retag(name, new_tag, db_major=False):
    """Read-only plan of a tag change (`retag … --dry-run`): the plan of an
    update plus what the radar offers the new tag as, the version it brings,
    where the tag is written and every line before and after; or why it
    cannot be done here. Returns (plan, compose context). db_major: the tag
    change of a database major (stage 5, make_major), which retag refuses."""
    if not TAG_ARG_RE.match(new_tag):
        raise Abort(64, f"Ungültiger Tag: {new_tag}", f"Invalid tag: {new_tag}")
    plan, ctx = make_plan(name)
    repo, old_tag = split_ref(plan["image"])
    rt = plan["retag"] = {"tag": new_tag, "from_tag": old_tag, "image": repo + ":" + new_tag,
                          "choice": None, "level": None, "newest": None,
                          "possible": False, "reason": None, "detail": None,
                          "variable": None, "changes": [], "git": None, "fingerprint": None,
                          "store": None}

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
    # A key-value store reads an older major's files when it starts: a newer
    # major is a tag change that saves its data for the rollback (save_store).
    if rec.get("db") and (target is None or major(target) != major(installed)) and not db_major:
        store = store_of(repo)
        if not (store and target is not None and major(target) > major(installed)):
            return refuse("db-major")
        rt["store"] = store_plan(plan, store)
        # Data the new major would not get (left_behind); the run checks the same after the pull.
        gone = left_behind(rt["store"], rt["image"], compose_cmd(ctx))
        if gone:
            return refuse("store-volume", ", ".join(f"{i['container']} {i['destination']}" for i in gone))
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
# The root password goes through MYSQL_PWD, as in MY_SH: on the dump's command line it would
# show in the host's process list.
MY_DUMP = ('export MYSQL_PWD="${MARIADB_ROOT_PASSWORD:-$MYSQL_ROOT_PASSWORD}"; '
           'if command -v mariadb-dump >/dev/null 2>&1; then D=mariadb-dump; else D=mysqldump; fi; '
           'exec "$D" --all-databases --single-transaction --routines --events -uroot')
DUMP_DONE = {"postgres": b"database cluster dump complete", "mysql": b"-- Dump completed"}


def dump_db(project, db, st, script=None):
    say(f"Datenbank-Dump von {db['name']} ({db['kind']}) …",
        f"Dumping database {db['name']} ({db['kind']}) …")
    if not db["running"]:
        raise Abort(5, f"{db['name']} läuft nicht – kein Dump möglich. Update abgebrochen.",
                    f"{db['name']} is not running – cannot dump. Update aborted.")
    pdir = os.path.join(DUMPS, project)
    os.makedirs(pdir, mode=0o700, exist_ok=True)
    final = os.path.join(pdir, f"{st}_{db['name']}.sql.gz")
    tmp = final + ".part"
    script = script or (PG_DUMP if db["kind"] == "postgres" else MY_DUMP)
    seen = {"tail": b""}
    t0 = time.time()

    def keep(gz, chunk):
        gz.write(chunk)
        seen["tail"] = (seen["tail"] + chunk)[-4096:]

    # The dump has DUMP_TIMEOUT seconds; then docker exec is stopped and the
    # partial file goes. The dump inside the container ends when it finds
    # its output closed; the update stops before anything changed.
    with tempfile.TemporaryFile() as errf:
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6) as gz:
                rc = copy_out(["docker", "exec", db["name"], "sh", "-c", script],
                              lambda chunk: keep(gz, chunk), LIMITS["dump"], errf, keep_fds=lock_fds())
        except BaseException as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            if isinstance(e, Timeout):
                raise Abort(5, f"Dump von {db['name']} nach {e.seconds} s abgebrochen (DUMP_TIMEOUT in {CONF}). "
                               f"Update abgebrochen – nichts geändert.",
                            f"Dump of {db['name']} stopped after {e.seconds} s (DUMP_TIMEOUT in {CONF}). "
                            f"Update aborted – nothing changed.")
            raise
        errf.seek(0)
        err = errf.read().decode(errors="replace").strip()
    tail = seen["tail"]
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
    recreated (Docker did not answer, a command failed): source "error".
    A command that did not end within its limit (diunproc.Timeout) gets
    source "timeout", a signal to the helper (diunproc.Interrupted) source
    "signal": the command was stopped, but what it asked the Docker daemon to
    do may go on or have stopped half way, so nobody can say the containers
    are as before."""
    if isinstance(e, Interrupted):
        return {"container": None, "state": None, "exit_code": None, "oom": False, "source": "signal",
                "line": pick(f"diun-update.py hat {e.name} bekommen und den Neustart oder die Prüfung danach "
                             f"abgebrochen; Docker kann die Container noch ändern – mit docker ps prüfen.",
                             f"diun-update.py got {e.name} and stopped the restart or the check after it; Docker "
                             f"may still be changing the containers – check with docker ps.")}
    if isinstance(e, Timeout):
        return {"container": None, "state": None, "exit_code": None, "oom": False, "source": "timeout",
                "line": pick(f"{' '.join(e.cmd[-6:])} hat nach {e.seconds} s nicht geendet und wurde abgebrochen "
                             f"(DOCKER_TIMEOUT); die Container können sich noch ändern – mit docker ps prüfen.",
                             f"{' '.join(e.cmd[-6:])} did not end within {e.seconds} s and was stopped "
                             f"(DOCKER_TIMEOUT); the containers may still be changing – check with docker ps.")}
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
    with held():        # a signal waits until the line is on disk (diunproc)
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
@contextmanager
def before_change():
    """A signal (diunproc.Interrupted) while nothing has changed yet: exit 5."""
    try:
        yield
    except Interrupted as e:
        raise stopped(e, "nichts geändert.", "nothing changed.")


def stopped(e, de, en):
    """Abort(5) for a run that a signal stopped before any container changed."""
    return Abort(5, f"Abgebrochen durch {e.name}, bevor ein Container geändert wurde – {de}",
                 f"Stopped by {e.name} before any container changed – {en}")


def cmd_update(name, do_dump=True):
    lock()
    with before_change():
        plan, ctx = make_plan(name)
    if not plan["updatable"]:
        raise Abort(2, f"Nicht aktualisierbar: {plan['reason']}", f"Not updatable: {plan['reason']}")
    return roll_out(name, plan, ctx, do_dump)


def cmd_rebuild(name, do_dump=True):
    """An update of an image built on this server: the image is built again
    from its Dockerfile with fresh bases (`docker compose build --pull`)."""
    lock()
    with before_change():
        plan, ctx = make_plan(name, build=True)
    if not plan["updatable"]:
        raise Abort(2, f"Nicht neu baubar: {plan['reason']}", f"Cannot be rebuilt: {plan['reason']}")
    return roll_out(name, plan, ctx, do_dump, build=True)


def tag_back(ref, image):
    """After a failed rebuild: ref names the image it named before."""
    if image and image_id(ref) != image:
        run(["docker", "tag", image, ref], check=False)


def cmd_pull(name):
    """Step one of an update in two steps: fetch the image of the plan's
    services and change nothing that runs. The row then shows "Roll-out
    pending", and a later update rolls the image out (e.g. in a maintenance
    window). No dump, no rollback copy and no history entry: nothing that runs
    changed, so there is nothing to take back. Every failure is exit 5."""
    lock()
    with before_change():
        plan, ctx = make_plan(name)
    if not plan["updatable"]:
        raise Abort(2, f"Nicht aktualisierbar: {plan['reason']}", f"Not updatable: {plan['reason']}")
    ref, services, running = plan["image"], plan["services"], plan["old_image"]
    say(f"Nur herunterladen: {ref} für {', '.join(services)} – die laufenden Container bleiben unverändert.",
        f"Download only: {ref} for {', '.join(services)} – the running containers stay unchanged.")
    with before_change():
        try:
            rc = stream(compose_cmd(ctx) + ["pull", *services])
        except Timeout as e:
            raise Abort(5, f"Der Pull hat nach {e.seconds} s nicht geendet und wurde abgebrochen (DOCKER_TIMEOUT in "
                           f"{CONF}) – nichts geändert. Docker kann ein begonnenes Herunterladen noch beenden.",
                        f"The pull did not end within {e.seconds} s and was stopped (DOCKER_TIMEOUT in {CONF}) – "
                        f"nothing changed. Docker may still finish a download it had started.")
    if rc != 0:
        error = reason_line(STREAM_TAIL) or f"exit {rc}"
        raise Abort(5, f"Der Pull ist fehlgeschlagen – nichts geändert. Pull: {error}",
                    f"The pull failed – nothing changed. Pull: {error}")
    new_id = image_id(ref)
    if not new_id or new_id == running:
        say("Kein neueres Image verfügbar – nichts geändert.", "No newer image available – nothing changed.")
        result(status="nochange", image=ref, from_version=plan["installed_version"],
               to_version=plan["installed_version"])
        return 0
    to_version = version_of(ref, new_id)
    say(f"Heruntergeladen: {ref} ({short_id(new_id)}{', ' + to_version if to_version else ''}). Ausrollen mit "
        f"„Image ausrollen“ oder `diun-update.py update {name}`.",
        f"Downloaded: {ref} ({short_id(new_id)}{', ' + to_version if to_version else ''}). Roll it out with "
        f"“Roll out image” or `diun-update.py update {name}`.")
    result(status="pulled", image=ref, from_version=plan["installed_version"], to_version=to_version)
    return 0


def cmd_retag(name, new_tag, expect, do_dump=True):
    """Change the configured tag (stage 2): the plan of `retag --dry-run`
    once more, and only if it still matches what the page showed."""
    if not FP_RE.match(expect):
        raise Abort(64, f"Ungültiger Fingerabdruck: {expect}", f"Invalid fingerprint: {expect}")
    lock()
    with before_change():
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
    no rollback tag, no dumps, no data copies. A signal meanwhile waits until
    it is done."""
    with held():
        if op.get("rollback"):
            run(["docker", "rmi", op["rollback"]], check=False)
        for rel in op["dumps"]:
            try:
                remove_dump(rel)
            except (Abort, OSError):
                pass
        for x in op.get("data_copies") or []:
            try:
                drop_copy(x.get("copy"))
            except OSError:
                pass


def drop_run(op):
    """Take back what a run did before any container changed: the files of a
    tag change (from their copies), the copies, the rollback tag, the dumps.
    A run that is in the history already gets the status "discarded" there,
    which hides it, and keeps no output. If the files cannot be put back,
    everything stays: the history keeps the run, and its rollback restores
    them. Runs once per run, and a signal meanwhile waits until it is done:
    the cleanup is never cut in two."""
    global _log_name
    if op.get("dropped"):
        return
    with held():
        if op.get("backup_files"):
            try:
                restore_files(op["backup_files"])
            except OSError as e:
                raise Abort(1, f"Wiederherstellen fehlgeschlagen ({e}) – die alten Dateien liegen in "
                               f"{op['backup']}; der Vorgang steht im Verlauf und lässt sich zurückrollen.",
                            f"Restoring failed ({e}) – the old files are in {op['backup']}; the operation is in "
                            f"the history and can be rolled back.")
            drop_backup(op["backup"])
        discard(op)
        if op["id"] in _recorded:
            record({"id": op["id"], "status": "discarded"})
            _log_name = None
            if _tee:
                _tee.close(keep=False)
        op["dropped"] = True


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


def roll_out(name, plan, ctx, do_dump, retag=None, build=False):
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
    with before_change():
        old_ids = {n: inspect(n)["Image"] for n in plan["containers"]}
        from_version = version_of(ref, old_ids[name]) or (built_version(ref, old_ids[name]) if build else None)
    old_id = old_ids[name]
    op = {"id": op_id, "ts": now_iso(), "kind": "retag" if retag else "rebuild" if build else "update", "container": name,
          "project": project, "workdir": ctx["workdir"], "files": ctx["files"],
          "env_files": ctx["env_files"], "services": services,
          "containers": plan["containers"], "image": ref, "old_id": old_id,
          "from_version": from_version, "to_version": None,
          "rollback": None, "dumps": [], "status": "running", "pid": os.getpid()}

    if retag:
        op.update(to_image=new_ref, from_tag=retag["from_tag"], to_tag=retag["tag"],
                  changes=[{k: c[k] for k in ("file", "line", "old", "new")} for c in retag["changes"]])
        say(f"Tag-Wechsel {ref} → {new_ref} in Projekt {project}: Services {', '.join(services)}",
            f"Changing {ref} to {new_ref} in project {project}: services {', '.join(services)}")
    elif build:
        say(f"Neubau {ref} in Projekt {project}: Services {', '.join(services)}",
            f"Rebuilding {ref} in project {project}: services {', '.join(services)}")
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
        except Interrupted as e:
            discard(op)
            raise stopped(e, "die angefangenen Dumps sind gelöscht, nichts geändert. Ein Dump im Container endet, "
                             "sobald er merkt, dass seine Ausgabe fehlt.",
                          "the dumps begun are deleted, nothing changed. A dump in the container ends once it "
                          "notices that its output is gone.")
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
        if build:
            # 3'. A rebuild: the Dockerfile again, with fresh bases. A build
            # that fails changed nothing that runs; the tag goes back to the
            # image it named, should a build of several services have moved it.
            before = image_id(ref)
            try:
                rc = stream(base + ["build", "--pull", *services])
            except Interrupted:
                tag_back(ref, before)
                raise
            except Timeout as e:
                tag_back(ref, before)
                drop_run(op)
                raise Abort(5, f"Der Neubau hat nach {e.seconds} s nicht geendet und wurde abgebrochen (DOCKER_TIMEOUT "
                               f"in {CONF}) – nichts geändert.",
                            f"The rebuild did not end within {e.seconds} s and was stopped (DOCKER_TIMEOUT in {CONF}) – "
                            f"nothing changed.")
            if rc != 0:
                error = reason_line(STREAM_TAIL) or f"exit {rc}"
                tag_back(ref, before)
                drop_run(op)
                raise Abort(5, f"Der Neubau ist fehlgeschlagen – nichts geändert. Build: {error}",
                            f"The rebuild failed – nothing changed. Build: {error}")
        try:
            pull_rc = 0 if build else stream(base + ["pull", *services])
        except Timeout as e:
            drop_run(op)
            raise Abort(5, f"Der Pull hat nach {e.seconds} s nicht geendet und wurde abgebrochen (DOCKER_TIMEOUT in "
                           f"{CONF}) – nichts geändert. Docker kann ein begonnenes Herunterladen noch beenden.",
                        f"The pull did not end within {e.seconds} s and was stopped (DOCKER_TIMEOUT in {CONF}) – "
                        f"nothing changed. Docker may still finish a download it had started.")
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
            say("Der Neubau ergab dasselbe Image – nichts geändert." if build else "Kein neueres Image verfügbar – nichts geändert.",
                "The rebuild gave the same image – nothing changed." if build else "No newer image available – nothing changed.")
            result(op=op_id, status="nochange", dumps=op["dumps"],
                   from_version=op["from_version"], to_version=op["to_version"])
            return 0
        op["new_id"] = new_id
        op["new_digests"] = repo_digests(new_id)
        op["to_version"] = version_of(new_ref, new_id) or (built_version(ref, new_id) if build else None)
        # In the history before the containers change (see above).
        record(op, log=op_id + ".log")
        # 3b. A key-value store's new major: stop it and pack its files.
        if retag and retag.get("store"):
            save_store(op, base, retag["store"], st)
    except Abort:
        raise
    except Interrupted as e:
        drop_run(op)
        raise stopped(e, "Dumps, Rollback-Kopie und geänderte Dateien zurückgenommen, nichts geändert. Ein "
                         "begonnener Pull kann im Docker-Daemon noch zu Ende laufen.",
                      "dumps, rollback copy and changed files taken back, nothing changed. A pull that had "
                      "begun may still finish in the Docker daemon.")
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
    except (Exception, Interrupted) as e:
        # A signal stops the command running (diunproc) and lands here: the
        # update fails with its history, output and rollback; further signals
        # wait until that is recorded.
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
        say("Neubau erfolgreich." if build else "Update erfolgreich.",
            "Rebuild successful." if build else "Update successful.")

    # 5. + 6. Never let these turn a successful update into a failure.
    try:
        cleanup_after_update(project, repo)
        start_radar()
    except Interrupted as e:
        say(f"{e.name}: Aufräumen und Radar-Start übersprungen.", f"{e.name}: cleanup and radar start skipped.")
    result(op=op_id, status="ok", rollback=rb, dumps=op["dumps"], **versions)
    return 0


# ------------------------------------------------- key-value store majors ----
# Redis, Valkey and KeyDB read the files of an older major when they start: a
# newer major needs no migration and is a tag change (stage 2). An older major
# may not read what a newer one wrote, though. So the change stops the store,
# packs its data directory into data/<project>/<stamp>_<container>.tar, and the
# rollback puts that copy back before the old image starts. The copy goes with
# the rollback image: without that image there is no rollback to use it for.
STORE_RE = re.compile(r"(^|/)(redis|valkey|keydb)$")
STORE_NAMES = {"redis": "Redis", "valkey": "Valkey", "keydb": "KeyDB"}
STORE_DIRS = ("/data", "/bitnami/redis/data", "/bitnami/valkey/data")    # the official images, Bitnami's
COPY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/\d{8}-\d{6}_[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.tar$")


def store_of(repo):
    """'redis', 'valkey' or 'keydb' for an image of such a store, else None."""
    m = STORE_RE.search(norm_ref(repo))
    return m.group(2) if m else None


def store_mount(c):
    """The volume or bind mount of container c at one of STORE_DIRS:
    {"type", "source", "destination", "anonymous"}, or None (nothing kept,
    e.g. tmpfs). anonymous: a volume without a name, which the image's VOLUME
    made (since 0.27.2)."""
    for m in c.get("Mounts") or []:
        if m.get("Destination") in STORE_DIRS and m.get("Type") in ("volume", "bind") and m.get("Source"):
            return {"type": m["Type"], "source": m["Source"], "destination": m["Destination"],
                    "anonymous": m["Type"] == "volume" and bool(ANON_VOLUME_RE.match(m.get("Name") or ""))}
    return None


def image_volumes(ref):
    """The paths image ref declares as volumes (VOLUME); None if it is not on the server."""
    try:
        img = docker_json("image", "inspect", ref)[0]
    except (subprocess.CalledProcessError, ValueError, IndexError, OSError):
        return None
    return set((img.get("Config") or {}).get("Volumes") or {})


def compose_targets(base, service):
    """The paths the compose files mount into service, as `docker compose config` gives them."""
    try:
        cfg = json.loads(run(base + ["config", "--format", "json"]))
    except (subprocess.CalledProcessError, ValueError, OSError):
        return set()
    vols = ((cfg.get("services") or {}).get(service) or {}).get("volumes") or []
    return {v.get("target") for v in vols if isinstance(v, dict)}


def left_behind(store, ref, base):
    """The items of a store whose data lies in an anonymous volume that
    neither the image ref (VOLUME) nor the compose files keep: compose would
    recreate the container without it, and the new major would start empty
    while the data stays in a volume no container uses (Redis 8's image no
    longer declares /data). None while ref is not on the server."""
    vols = image_volumes(ref)
    if vols is None:
        return None
    return [i for i in store["items"] if i.get("anonymous") and i["destination"] not in vols
            and i["destination"] not in compose_targets(base, i.get("service"))]


def tree_bytes(path):
    """Bytes of the files below path (links not followed), None if it is no directory."""
    if not os.path.isdir(path):
        return None
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def store_plan(plan, store):
    """retag.store: the store and, per container of the plan, the directory
    the tag change packs ("source" None: nothing mounted, nothing to keep)."""
    items = []
    for n in plan["containers"]:
        c = inspect(n)
        m = store_mount(c) or {"type": None, "source": None, "destination": None, "anonymous": False}
        items.append({"container": n, "service": (c["Config"].get("Labels") or {}).get(L_SERVICE), **m,
                      "bytes": tree_bytes(m["source"]) if m["source"] else None})
    return {"engine": store, "name": STORE_NAMES[store], "items": items,
            "bytes": sum(i["bytes"] or 0 for i in items)}


def copy_path(rel):
    """The file of a data copy, rel = <project>/<stamp>_<container>.tar below
    data/; None if rel is not one."""
    if not isinstance(rel, str) or not COPY_RE.match(rel):
        return None
    top = os.path.realpath(os.path.join(BASE, "data"))
    path = os.path.realpath(os.path.join(top, rel))
    return path if path.startswith(top + os.sep) else None


def pack_dir(src, project, name):
    """A tar of the directory src with owners, modes and links, written as
    data/<project>/<name>.tar (root only). Returns <project>/<name>.tar."""
    rel = f"{project}/{name}.tar"
    if not NAME_RE.match(project) or not copy_path(rel):
        raise Abort(64, f"Ungültiger Name der Datenkopie: {rel}", f"Invalid name of the data copy: {rel}")
    top = os.path.join(BASE, "data")
    d = os.path.join(top, project)
    os.makedirs(d, mode=0o700, exist_ok=True)
    for x in (top, d):
        os.chmod(x, 0o700)
    path = os.path.join(d, name + ".tar")
    part = path + ".part"
    try:
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            with tarfile.open(fileobj=f, mode="w") as tar:
                tar.add(src, arcname=".")
            f.flush()
            os.fsync(f.fileno())
        os.replace(part, path)
    except BaseException:
        try:
            os.unlink(part)
        except OSError:
            pass
        raise
    return rel


def unpack_dir(path, dst):
    """Empty the directory dst and unpack a copy of pack_dir into it, owners
    and modes as they were. The members are checked first: nothing outside
    dst, only files, directories and links."""
    with tarfile.open(path) as tar:
        members = tar.getmembers()
        for m in members:
            n = os.path.normpath(m.name)
            if os.path.isabs(m.name) or n == ".." or n.startswith("../") or \
                    not (m.isreg() or m.isdir() or m.issym() or m.islnk()):
                raise ValueError(f"{os.path.basename(path)}: {m.name}")
        for e in os.listdir(dst):
            p = os.path.join(dst, e)
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.unlink(p)
        # The "tar" filter where Python has it (3.12, backports): it keeps owners and modes,
        # which the default from 3.14 on ("data") would not.
        kw = {"filter": "tar"} if hasattr(tarfile, "tar_filter") else {}
        tar.extractall(dst, members=members, numeric_owner=True, **kw)


def drop_copy(rel):
    """Delete one data copy (and what is left of writing it), and its project
    folder once empty. True if the copy was there."""
    path = copy_path(rel)
    if not path:
        return False
    there = os.path.isfile(path)
    for p in (path, path + ".part"):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
    for d in (os.path.dirname(path), os.path.dirname(os.path.dirname(path))):
        try:
            os.rmdir(d)
        except OSError:
            break
    return there


def drop_copies_of(ref):
    """After the rollback image ref is gone: the data copies of its runs."""
    for o in load_ops().values():
        if o.get("rollback") and norm_ref(o["rollback"]) == norm_ref(ref):
            for x in o.get("data_copies") or []:
                if drop_copy(x.get("copy")):
                    say(f"Datenkopie gelöscht: {x['copy']}", f"Deleted data copy {x['copy']}")


def save_store(op, base, store, st):
    """Step 3b of a key-value store's new major (in the history already):
    stop its services, so that the store writes its files and nothing changes
    them, then pack each container's data directory. Anything wrong starts the
    services again and takes back what the run did (exit 5)."""
    name = store["name"]
    op["store"] = name
    items = [i for i in store["items"] if i["source"]]
    # The new image is here now: data it would leave behind stops the run before the store stops.
    gone = left_behind(store, op.get("to_image") or op["image"], base)
    if gone:
        drop_run(op)
        where = ", ".join(f"{i['container']} {i['destination']}" for i in gone)
        raise Abort(5, f"Das neue Image erklärt {where} nicht mehr als Volume, und die Compose-Datei bindet dort "
                       f"nichts ein: Die Daten im anonymen Volume blieben zurück, und {name} startete leer. Erst ein "
                       f"benanntes Volume für diesen Pfad in die Compose-Datei eintragen – nichts geändert.",
                    f"The new image no longer declares {where} as a volume, and the compose file mounts nothing "
                    f"there: the data in the anonymous volume would stay behind and {name} would start empty. Add a "
                    f"named volume for that path to the compose file first – nothing changed.")
    if not items:
        say(f"{name}: kein Datenverzeichnis eingehängt – nichts zu sichern; nach einem Rollback startet {name} leer.",
            f"{name}: no data directory mounted – nothing to save; after a rollback {name} starts empty.")
        return
    need = sum(i["bytes"] or 0 for i in items) + FREE_MARGIN
    free = free_space(BASE)
    if free < need:
        drop_run(op)
        raise Abort(5, f"Zu wenig Platz in {BASE} für die Daten von {name}: {human(free)} frei, {human(need)} nötig "
                       f"– nichts geändert.",
                    f"Not enough space in {BASE} for the data of {name}: {human(free)} free, {human(need)} needed "
                    f"– nothing changed.")
    op["data_copies"] = []
    try:
        say(f"{name} wird gestoppt, damit es seine Dateien schreibt und nichts sie mehr ändert: "
            f"{', '.join(op['services'])}",
            f"Stopping {name}, so that it writes its files and nothing changes them any more: "
            f"{', '.join(op['services'])}")
        if stream(base + ["stop", *op["services"]]) != 0:
            raise RuntimeError(pick("docker compose stop schlug fehl", "docker compose stop failed"))
        for i in items:
            # Named in op before it is written: taking back the run removes a half-written copy too.
            x = {"container": i["container"], "source": i["source"], "destination": i["destination"],
                 "copy": f"{op['project']}/{st}_{i['container']}.tar", "bytes": None}
            op["data_copies"].append(x)
            pack_dir(i["source"], op["project"], f"{st}_{i['container']}")
            x["bytes"] = os.path.getsize(copy_path(x["copy"]))
            say(f"Daten gesichert: {i['source']} → {os.path.join(BASE, 'data', x['copy'])} ({human(x['bytes'])})",
                f"Data saved: {i['source']} → {os.path.join(BASE, 'data', x['copy'])} ({human(x['bytes'])})")
        # With its copies in the history: a run that ends for good from here on
        # is rolled back with them.
        record(op, log=op["id"] + ".log")
    except (Exception, Interrupted) as e:
        with held():
            try:
                stream(base + ["start", *op["services"]])
            except Exception:
                pass
            drop_run(op)
        if isinstance(e, Interrupted):
            raise stopped(e, f"{name} läuft wieder, nichts geändert.", f"{name} runs again, nothing changed.")
        detail = str(e).strip()[-300:]
        raise Abort(5, f"Sichern der Daten von {name} fehlgeschlagen: {detail} – {name} läuft wieder, Dateien "
                       f"wiederhergestellt, nichts geändert.",
                    f"Saving the data of {name} failed: {detail} – {name} runs again, files restored, nothing "
                    f"changed.")


# -------------------------------------------------------- database majors ----
# Stage 5: a PostgreSQL major (16 -> 17) by dump and restore. The data of the
# old major is moved aside, not copied: into a volume of its own next to a
# named volume, or into a sibling directory of a bind mount (renames on the
# same filesystem: no second copy, no time). It stays until it is released
# (delete-olddata); a rollback moves it back. The compose file changes only in
# its tag line (stage 2).
PGDATA_DEFAULT = "/var/lib/postgresql/data"
PG_NEW_LAYOUT = 18          # from 18 on the image keeps its data in /var/lib/postgresql/<major>/docker
READY_TIMEOUT = 300         # seconds for the new major to accept connections over TCP
ANON_VOLUME_RE = re.compile(r"^[0-9a-f]{64}$")
KEPT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,250}[.-](pg|mariadb)\d{1,3}-\d{8}-\d{6}$")   # make_place's names
# Every database, every table (also partitioned ones), counted exactly:
# "database|schema.table|rows" per line.
PG_COUNTS = r"""U="${POSTGRES_USER:-postgres}"
dbs=$(psql -X -U "$U" -d postgres -At -c "select datname from pg_database where datallowconn and datname <> 'template1' order by 1") || exit 1
printf '%s\n' "$dbs" | while IFS= read -r d; do
  printf '%s\n' "select format('select %L, %L, count(*) from %I.%I', current_database(), n.nspname || '.' || c.relname, n.nspname, c.relname) from pg_class c join pg_namespace n on n.oid = c.relnamespace where c.relkind in ('r', 'p') and n.nspname <> 'information_schema' and n.nspname !~ '^pg_' order by 1 \gexec" |
    psql -X -U "$U" -d "$d" -At -F '|' -v ON_ERROR_STOP=1 || exit 1
done"""
PG_EXTENSIONS = r"""U="${POSTGRES_USER:-postgres}"
dbs=$(psql -X -U "$U" -d postgres -At -c "select datname from pg_database where datallowconn order by 1") || exit 1
printf '%s\n' "$dbs" | while IFS= read -r d; do psql -X -U "$U" -d "$d" -At -c "select extname from pg_extension" || exit 1; done"""
PG_READY = 'pg_isready -q -h 127.0.0.1 -p "${PGPORT:-5432}" -U "${POSTGRES_USER:-postgres}"'
# The database the image creates on its first start is empty; the dump creates it again with its data.
PG_DROP_FIRST = r"""U="${POSTGRES_USER:-postgres}"; D="${POSTGRES_DB:-$U}"
[ "$D" = postgres ] || [ "$D" = template1 ] || printf 'DROP DATABASE IF EXISTS :"d";\n' |
  psql -X -q -v ON_ERROR_STOP=1 -v d="$D" -U "$U" -d postgres"""
PG_RESTORE = 'exec psql -X -q -v ON_ERROR_STOP=0 -U "${POSTGRES_USER:-postgres}" -d postgres -o /dev/null'
PG_ANALYZE = 'exec vacuumdb -U "${POSTGRES_USER:-postgres}" --all --analyze-only --quiet'
# Roles that log in with an MD5 password hash: the images of PostgreSQL 14 and
# newer let clients in over the network with scram-sha-256 only.
PG_MD5_ROLES = ('psql -X -U "${POSTGRES_USER:-postgres}" -d postgres -At -c '
                '"select rolname from pg_authid where rolcanlogin and rolpassword like \'md5%\' order by 1"')
# After the restore: the user of the environment logs in over TCP with its
# password, as the services that wait for the database do.
PG_LOGIN = ('[ -n "$POSTGRES_PASSWORD" ] || { echo skipped; exit 0; }; PGPASSWORD="$POSTGRES_PASSWORD" exec psql -X '
            '-h 127.0.0.1 -p "${PGPORT:-5432}" -U "${POSTGRES_USER:-postgres}" -d postgres -Atc "select 1"')
PROBE_LABEL = "image-updates.probe"     # the throwaway containers of a major's trial run

# Since 0.25.0 a major runs for MariaDB too, the same way: what differs per
# engine is where the data lies, how the server says it is ready, how rows are
# counted, how a dump is restored and what follows the restore.
ENGINES = {"postgres": "PostgreSQL", "mariadb": "MariaDB"}
PLACE_PREFIX = {"postgres": "pg", "mariadb": "mariadb"}     # <volume>-pg16-<stamp>, <volume>-mariadb11-<stamp>
MARIADB_RE = re.compile(r"(^|/)mariadb$")
MARIADB_ENV = ("MARIADB_VERSION=", "MARIADB_MAJOR=")
MY_DATADIR = "/var/lib/mysql"
MY_VERSION_FILE = "mysql_upgrade_info"      # the version mariadb-upgrade last ran for, in the data directory
LEAD_MAJOR_RE = re.compile(r"^\s*(\d+)")
# The root password from the container's environment goes through MYSQL_PWD,
# never onto a command line.
MY_SH = ('export MYSQL_PWD="${MARIADB_ROOT_PASSWORD:-$MYSQL_ROOT_PASSWORD}"; '
         'if command -v mariadb >/dev/null 2>&1; then C=mariadb A=mariadb-admin U=mariadb-upgrade; '
         'else C=mysql A=mysqladmin U=mysql_upgrade; fi; ')
# Every table of every database but the system ones, counted exactly: "db<TAB>table<TAB>rows".
MY_COUNTS = MY_SH + r"""q=$("$C" -uroot -N -B -e "select concat('select ', quote(table_schema), ', ', quote(table_name), ', count(*) from \`', replace(table_schema, '\`', '\`\`'), '\`.\`', replace(table_name, '\`', '\`\`'), '\`;') from information_schema.tables where table_type = 'BASE TABLE' and table_schema not in ('mysql', 'information_schema', 'performance_schema', 'sys') order by table_schema, table_name") || exit 1
[ -z "$q" ] || printf '%s\n' "$q" | "$C" -uroot -N -B"""
MY_SERVER_VERSION = MY_SH + '"$C" -uroot -N -B -e "select version()"'
# The dump of a MariaDB major: the system tables of the old major (the mysql
# database) do not load into a newer one (mysql.proc has another column count),
# so the dump holds the users' databases and then the accounts as statements
# the new major runs, in an order it takes: every user (CREATE OR REPLACE USER,
# from SHOW CREATE USER), every role (CREATE OR REPLACE ROLE, also one nobody
# has), then the grants of each (SHOW GRANTS: privileges, the roles granted to
# users and roles, the grant and admin options), then the default roles, then
# FLUSH PRIVILEGES. The roles are created with a role of the restore's own as
# their admin (IMPORT_ROLE, dropped at the end), as mariadb-dump --system=users
# does: CREATE ROLE grants that role to the restoring user, and MariaDB lets a
# user grant every role it reaches with the admin option through the roles
# granted to it, so the restore may grant every role, and root ends up with
# the roles it had and no more. No SET ROLE: it would compute the session's
# privileges anew from root's account, which the restore has just replaced
# and not granted anything yet (CREATE ROLE then fails for want of CREATE
# USER, as the end-to-end test found). The restoring account, root@localhost
# (the restore logs in as root over the socket), is altered, not replaced:
# replacing it drops its PROXY grant, and with that the right to grant PROXY
# (acl_check_proxy_grant_access), so its own GRANT PROXY was refused (found by
# the end-to-end test too); mariadb-dump never drops the current user either.
# PUBLIC is not created (every
# server has it), only its grants come along. Left out: mariadb.sys and
# healthcheck, which the image sets up itself. An error of any query ends the
# dump (each answer is read whole before a pipe uses it), and the end marker
# comes last; names reach the server only as SQL literals QUOTE() made.
IMPORT_ROLE = "image_updates_import_role"
MY_MAJOR_DUMP = MY_SH + r"""dbs=$("$C" -uroot -N -B -e "select schema_name from information_schema.schemata where schema_name not in ('mysql', 'information_schema', 'performance_schema', 'sys') order by 1") || exit 1
if command -v mariadb-dump >/dev/null 2>&1; then D=mariadb-dump; else D=mysqldump; fi
if [ -n "$dbs" ]; then "$D" -uroot --single-transaction --routines --events --triggers --databases $dbs || exit 1; fi
x="user not in ('mariadb.sys', 'healthcheck', 'image_updates_import_role')"
users=$("$C" -uroot -N -B -r -e "select concat(quote(user), '@', quote(host)) from mysql.user where is_role = 'N' and $x order by user, host") || exit 1
roles=$("$C" -uroot -N -B -r -e "select quote(user) from mysql.user where is_role = 'Y' and user <> 'PUBLIC' and $x order by user") || exit 1
public=$("$C" -uroot -N -B -r -e "select count(*) from mysql.user where is_role = 'Y' and user = 'PUBLIC'") || exit 1
defaults=$("$C" -uroot -N -B -r -e "select concat('SET DEFAULT ROLE ', quote(default_role), ' FOR ', quote(user), '@', quote(host), ';') from mysql.user where is_role = 'N' and default_role <> '' and $x order by user, host") || exit 1
echo "-- Users and roles"
printf '%s\n' "$users" | while IFS= read -r a; do
  [ -n "$a" ] || continue
  s=$("$C" -uroot -N -B -r -e "show create user $a") || exit 1
  if [ "$a" = "'root'@'localhost'" ]; then c="ALTER USER "; else c="CREATE OR REPLACE USER "; fi
  printf '%s;\n' "$s" | sed "s/^CREATE USER /$c/"
done || exit 1
echo "CREATE ROLE image_updates_import_role;"
printf '%s\n' "$roles" | while IFS= read -r r; do
  [ -z "$r" ] || printf 'CREATE OR REPLACE ROLE %s WITH ADMIN image_updates_import_role;\n' "$r"
done
echo "-- Grants"
{ printf '%s\n' "$users" "$roles"; [ "$public" = 0 ] || echo PUBLIC; } | while IFS= read -r a; do
  [ -n "$a" ] || continue
  g=$("$C" -uroot -N -B -r -e "show grants for $a") || exit 1
  [ -z "$g" ] || printf '%s\n' "$g" | sed '/^SET DEFAULT ROLE /d; s/$/;/'
done || exit 1
[ -z "$defaults" ] || printf '%s\n' "$defaults"
echo "DROP ROLE image_updates_import_role;"
echo "FLUSH PRIVILEGES;"
echo "-- Dump completed"
"""
# Every account the dump takes along (users, roles, PUBLIC) with its grants,
# one "account<TAB>line" per line: read before the dump, in the trial run and
# after the restore, and compared (compare_grants).
MY_GRANTS = MY_SH + r"""x="user not in ('mariadb.sys', 'healthcheck', 'image_updates_import_role')"
a=$("$C" -uroot -N -B -r -e "select if(is_role = 'Y', if(user = 'PUBLIC', 'PUBLIC', quote(user)), concat(quote(user), '@', quote(host))) from mysql.user where $x order by is_role, user, host") || exit 1
printf '%s\n' "$a" | while IFS= read -r u; do
  [ -n "$u" ] || continue
  g=$("$C" -uroot -N -B -r -e "show grants for $u") || exit 1
  printf '%s\n' "$g" | while IFS= read -r l; do printf '%s\t%s\n' "$u" "$l"; done
done || exit 1"""
# Whether a role (or user) has the name the restore uses for its own role.
MY_ROLE_NAME = MY_SH + "\"$C\" -uroot -N -B -e \"select count(*) from mysql.user where user = 'image_updates_import_role'\""
MY_READY = MY_SH + '"$A" ping --protocol=tcp -h 127.0.0.1 -uroot --silent'
MY_RESTORE = MY_SH + 'exec "$C" -uroot'
# A restore leaves the system tables of the dump's version while the data
# directory already says the new one: mariadb-upgrade --force brings them up.
MY_AFTER = MY_SH + '"$C" -uroot -e "flush privileges" && "$U" --force -uroot'
MY_LOGIN = MY_SH + ('u="${MARIADB_USER:-$MYSQL_USER}"; p="${MARIADB_PASSWORD:-$MYSQL_PASSWORD}"; '
                    '[ -n "$u" ] || { u=root; p="$MYSQL_PWD"; }; '
                    'MYSQL_PWD="$p" exec "$C" --protocol=tcp -h 127.0.0.1 -u"$u" -N -B -e "select 1"')


def engine_of(x):
    """The engine of a plan's "major" or an operation; before 0.25.0 always PostgreSQL."""
    return (x or {}).get("engine") or "postgres"


def ename(x):
    return ENGINES.get(engine_of(x), engine_of(x))


def lead_major(text):
    """11 of "11.8.9-MariaDB-ubu2404", 16 of "16"; None if it names no number."""
    m = LEAD_MAJOR_RE.match(text or "")
    return int(m.group(1)) if m else None


def run_env(cmd, env, timeout=300):
    """run() with variables put into the environment of the command: values a
    command line must not show (`docker run -e NAME` takes them from there)."""
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env={**os.environ, **env})
    if p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, cmd, p.stdout, p.stderr)
    return p.stdout


def my_datadir(c):
    """The data directory of a MariaDB container: --datadir in its command, else /var/lib/mysql."""
    for a in (c["Config"].get("Cmd") or []):
        if str(a).startswith("--datadir="):
            return str(a).split("=", 1)[1] or MY_DATADIR
    return MY_DATADIR


def major_of(version):
    try:
        return nums_of(version)[0] if version else None
    except ValueError:
        return None


def inside(path, mount):
    """path is mount itself or lies below it."""
    path, mount = os.path.normpath(path), os.path.normpath(mount)
    return path == mount or path.startswith(mount.rstrip("/") + "/")


def env_of(c):
    out = {}
    for e in c["Config"].get("Env") or []:
        k, _, v = e.partition("=")
        out[k] = v
    return out


def data_mount(c, pgdata):
    """The mount that holds PGDATA (the longest destination that contains it):
    {"type": "volume|bind", "name", "driver", "source", "destination"} or None."""
    best = None
    for m in c.get("Mounts") or []:
        dest = m.get("Destination") or ""
        if dest and inside(pgdata, dest) and (best is None or len(dest) > len(best["destination"])):
            best = {"type": m.get("Type"), "name": m.get("Name"), "driver": m.get("Driver"),
                    "source": m.get("Source"), "destination": dest}
    return best


def pg_lines(name, script, timeout=None):
    """Lines a shell script prints in the database container."""
    out = run(["docker", "exec", name, "sh", "-c", script], timeout=timeout or LIMITS["dump"])
    return [ln for ln in out.splitlines() if ln.strip()]


def table_counts(name, engine="postgres"):
    """{database: {schema.table: rows}} of every table, counted exactly
    (MariaDB: {database: {table: rows}}, the system databases left out)."""
    counts = {}
    if engine == "mariadb":
        for ln in pg_lines(name, MY_COUNTS):
            parts = ln.split("\t")
            if len(parts) == 3 and parts[2].strip().isdigit():
                counts.setdefault(parts[0], {})[parts[1]] = int(parts[2])
        return counts
    for ln in pg_lines(name, PG_COUNTS):
        db, _, rest = ln.partition("|")
        table, _, n = rest.rpartition("|")
        if table and n.strip().isdigit():
            counts.setdefault(db, {})[table] = int(n)
    return counts


def compare_counts(before, after):
    """What differs: ["db: schema.table 120 -> 119", …] (missing tables as -)."""
    out = []
    for db in sorted(set(before) | set(after)):
        a, b = before.get(db) or {}, after.get(db) or {}
        for t in sorted(set(a) | set(b)):
            if a.get(t) != b.get(t):
                out.append(f"{db}: {t} {a.get(t, '-')} -> {b.get(t, '-')}")
    return out


def account_grants(name):
    """MariaDB: {account: its SHOW GRANTS lines, sorted} of every user, role
    and PUBLIC the major dump takes along (MY_GRANTS)."""
    out = {}
    for ln in pg_lines(name, MY_GRANTS, timeout=120):
        acct, sep, line = ln.partition("\t")
        if sep:
            out.setdefault(acct, []).append(line)
    return {k: sorted(v) for k, v in out.items()}


def compare_grants(before, after):
    """What a restore lost or added: ["'viewer': missing", "'app'@'%': lacks
    GRANT …", "'root'@'localhost': has GRANT … too"]. An account only the new
    server has (one its image set up from the environment) does not count."""
    out = []
    for acct in sorted(before):
        if acct not in after:
            out.append(f"{acct}: missing")
            continue
        b, a = set(before[acct]), set(after[acct])
        out += [f"{acct}: lacks {ln}" for ln in sorted(b - a)] + [f"{acct}: has {ln} too" for ln in sorted(a - b)]
    return out


def count_summary(counts):
    return pick(f"{len(counts)} Datenbanken, {sum(len(t) for t in counts.values())} Tabellen, "
                f"{sum(n for t in counts.values() for n in t.values())} Zeilen",
                f"{len(counts)} databases, {sum(len(t) for t in counts.values())} tables, "
                f"{sum(n for t in counts.values() for n in t.values())} rows")


def missing_extensions(ref, used):
    """The extensions in use that the image `ref` does not ship."""
    if not used:
        return []
    out = run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "sh", ref, "-c",
               'ls "$(pg_config --sharedir)/extension"'], timeout=LIMITS["docker"])
    have = {f[:-len(".control")] for f in out.split() if f.endswith(".control")}
    return sorted(e for e in used if e not in have)


RESTORE_OK_RE = re.compile(r'ERROR:\s+role "[^"]+" already exists')


def restore_errors(stderr):
    """The error lines of a restore with psql, except the role the new
    database was created with, which the dump creates again."""
    return [ln.strip() for ln in stderr.splitlines() if "ERROR:" in ln and not RESTORE_OK_RE.search(ln)]


def make_major(name, new_tag):
    """Read-only plan of a database major (`dbmajor … --dry-run`): the plan of
    the tag change to the new major (make_retag) plus "major": the majors,
    where the data lies and where the new major will keep it, the services
    stopped meanwhile, the extensions in use, the space; or why not."""
    plan, ctx = make_retag(name, new_tag, db_major=True)
    rt = plan["retag"]
    mj = plan["major"] = {"possible": False, "reason": None, "detail": None, "engine": None, "from_major": None,
                          "to_major": None, "data": None, "pgdata": None, "datadir": None, "stop": [],
                          "extensions": [], "data_bytes": None, "data_free": None, "fingerprint": None}

    def refuse(reason, detail=None):
        mj["reason"], mj["detail"] = reason, detail
        return plan, ctx

    if not rt["possible"]:
        return refuse(rt["reason"], rt.get("detail"))
    c = inspect(name)
    kind = kind_of(c)
    image_env = c["Config"].get("Env") or []
    if kind == "postgres":
        engine = "postgres"
    elif kind == "mysql" and (MARIADB_RE.search(split_ref(c["Config"]["Image"])[0].lower())
                              or any(e.startswith(MARIADB_ENV) for e in image_env)):
        engine = "mariadb"
    else:
        return refuse("major-not-supported", c["Config"]["Image"])
    mj["engine"] = engine
    if len(plan["services"]) != 1 or len(plan["containers"]) != 1:
        return refuse("major-several", ", ".join(plan["containers"]))
    if not c["State"].get("Running"):
        return refuse("major-stopped")
    old, new = major_of(plan["installed_version"]), major_of(plan["target_version"])
    if old is None or new is None or new <= old:
        return refuse("major-not-newer", plan["target_version"])
    mj["from_major"], mj["to_major"] = old, new
    datadir = (env_of(c).get("PGDATA") or PGDATA_DEFAULT) if engine == "postgres" else my_datadir(c)
    mount = data_mount(c, datadir)
    if not mount or mount["type"] not in ("volume", "bind") or not mount["source"]:
        return refuse("major-no-volume", datadir)
    if mount["type"] == "volume" and (mount["driver"] or "local") != "local":
        return refuse("major-volume-driver", mount["driver"])
    if mount["type"] == "volume" and ANON_VOLUME_RE.match(mount["name"] or ""):
        return refuse("major-anonymous-volume", mount["destination"])
    src = mount["source"]
    if not os.path.isdir(src):
        return refuse("major-no-volume", src)
    if mount["type"] == "bind" and (os.path.ismount(src) or os.stat(src).st_dev != os.stat(os.path.dirname(
            os.path.normpath(src))).st_dev):
        return refuse("major-bind-mountpoint", src)
    mj["data"] = mount
    service = plan["services"][0]
    env = (((ctx.get("cfg") or {}).get("services") or {}).get(service) or {}).get("environment") or {}
    pgdata = None
    if engine == "postgres":
        # Where the new major keeps its data must lie in that mount: from 18 on the
        # image uses /var/lib/postgresql/18/docker unless the compose file sets PGDATA.
        pgdata = (env.get("PGDATA") if isinstance(env, dict) else None) or \
            (f"/var/lib/postgresql/{new}/docker" if new >= PG_NEW_LAYOUT else PGDATA_DEFAULT)
        if not inside(pgdata, mount["destination"]):
            return refuse("major-pgdata", f"{pgdata} | {mount['destination']}")
        mj["pgdata"] = pgdata
    else:
        mj["datadir"] = datadir
        # Dump, count and restore log in as root with the password of the environment.
        if not (env_of(c).get("MARIADB_ROOT_PASSWORD") or env_of(c).get("MYSQL_ROOT_PASSWORD")):
            return refuse("major-root-password")
        # The users and roles with their grants, as the dump and the comparisons will read them;
        # the restore gives its own role a name no role of the database may have.
        try:
            account_grants(name)
            taken = pg_lines(name, MY_ROLE_NAME, timeout=60)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            return refuse("major-unreadable", (getattr(e, "stderr", None) or str(e)).strip()[-300:])
        if taken and taken[0].strip() != "0":
            return refuse("major-role-name", IMPORT_ROLE)
    mj["stop"] = [d["service"] for d in plan["dependents"]
                  if any((inspect(n)["State"] or {}).get("Running") for n in d["containers"])]
    if engine == "postgres":
        try:
            mj["extensions"] = sorted(set(pg_lines(name, PG_EXTENSIONS, timeout=120)) - {"plpgsql"})
            md5 = pg_lines(name, PG_MD5_ROLES, timeout=120)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            return refuse("major-unreadable", (getattr(e, "stderr", None) or str(e)).strip()[-300:])
        # The dump keeps the password hashes: an MD5 one would not let its role in
        # any more, unless the compose file keeps MD5 for the network.
        auth = str((env.get("POSTGRES_HOST_AUTH_METHOD") if isinstance(env, dict) else None) or "").lower()
        if md5 and auth not in ("md5", "password", "trust"):
            return refuse("major-md5", ", ".join(md5[:10]))
        mj["data_bytes"] = next((d["size"] for d in plan["dbs"] if d["name"] == name), None)
    else:
        mj["data_bytes"] = kept_bytes({"path": src})
    mj["data_free"] = shutil.disk_usage(src).free
    if mj["data_bytes"] is not None and mj["data_free"] < mj["data_bytes"] + FREE_MARGIN:
        return refuse("major-space", f"{human(mj['data_free'])} | {human(mj['data_bytes'] + FREE_MARGIN)}")
    # The trial run (probe_restore) keeps a copy in Docker's directory for a while.
    root = docker_root()
    if root and mj["data_bytes"] is not None and not same_fs(root, src):
        free = shutil.disk_usage(root).free
        if free < mj["data_bytes"] + FREE_MARGIN:
            return refuse("major-probe-space", f"{human(free)} | {human(mj['data_bytes'] + FREE_MARGIN)} | {root}")
    mj["fingerprint"] = hashlib.sha256(json.dumps([rt["fingerprint"], mount, pgdata or datadir, mj["stop"]]
                                                  + ([engine] if engine != "postgres" else []),
                                                  sort_keys=True).encode()).hexdigest()
    mj["possible"] = True
    return plan, ctx


def same_fs(a, b):
    return os.stat(a).st_dev == os.stat(b).st_dev


def docker_root():
    """Docker's own directory (anonymous volumes live there), or None."""
    try:
        root = run(["docker", "info", "-f", "{{.DockerRootDir}}"], timeout=60).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    return root if root.startswith("/") and os.path.isdir(root) else None


def drop_probes():
    """Containers of a trial run that a killed run left behind (PROBE_LABEL),
    with their anonymous volumes."""
    try:
        ids = run(["docker", "ps", "-aq", "--filter", "label=" + PROBE_LABEL], timeout=60).split()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return
    for i in ids:
        run(["docker", "rm", "-f", "-v", i], check=False, timeout=120)


def probe_restore(name, plan, op, before, grants=None):
    """Before anything changes (the old database runs on its data): the dump
    restored into a throwaway container of the new major, without network, on
    an anonymous volume, removed afterwards, and counted as the run counts it;
    MariaDB: its users and roles with their grants compared too (grants, as
    account_grants read them on the old server). A dump the new major does not
    take, other counts or other grants stop the major here (exit 5)."""
    mj, new_ref, new = plan["major"], plan["retag"]["image"], plan["major"]["to_major"]
    engine, label = engine_of(mj), ename(mj)
    env = env_of(inspect(name))
    probe = f"{name}-probe-{op['id'][:15]}"
    drop_probes()
    cmd = ["docker", "run", "-d", "--rm", "--name", probe, "--network", "none",
           "--label", f"{PROBE_LABEL}={op['id']}"]
    secret = {}
    if engine == "mariadb":
        # The dump brings the old users with it: the probe gets the root password
        # of the real one, through the environment of docker run, not its arguments.
        secret = {"MARIADB_ROOT_PASSWORD": env.get("MARIADB_ROOT_PASSWORD") or env.get("MYSQL_ROOT_PASSWORD") or ""}
        cmd += ["-e", "MARIADB_ROOT_PASSWORD"]
    else:
        cmd += ["-e", "POSTGRES_PASSWORD=" + secrets.token_hex(16)]
        for k in ("POSTGRES_USER", "POSTGRES_DB"):
            if env.get(k):
                cmd += ["-e", f"{k}={env[k]}"]
        if mj.get("pgdata"):
            cmd += ["-e", "PGDATA=" + mj["pgdata"]]
    say(f"Probelauf: der Dump in einen Wegwerf-Container von {label} {new}, ohne Netz …",
        f"Trial run: the dump into a throwaway container of {label} {new}, without network …")
    why = None
    try:
        run_env(cmd + [new_ref], secret, timeout=300)
        try:
            ready = wait_ready(probe, engine)
        except Abort:
            ready = False           # it ended and --rm removed it
        if not ready:
            why = pick(f"{label} startete nicht", f"{label} did not start")
        else:
            errors = restore_into(probe, op["dumps"][0], engine)
            if errors:
                for ln in errors[:20]:
                    print(ln)
                why = pick(f"Einspielen: {errors[0]}", f"restore: {errors[0]}")
            else:
                if engine == "mariadb":
                    after_restore(probe, engine)
                diff = compare_counts(before, table_counts(probe, engine))
                if diff:
                    why = pick(f"Zeilen weichen ab: {diff[0]}", f"rows differ: {diff[0]}")
                elif grants is not None:
                    diff = compare_grants(grants, account_grants(probe))
                    for ln in diff[:20]:
                        print(ln)
                    if diff:
                        why = pick(f"Rechte weichen ab: {diff[0]}", f"privileges differ: {diff[0]}")
    except subprocess.CalledProcessError as e:
        why = ((e.stderr or "") if isinstance(e.stderr, str) else str(e)).strip()[-300:] or str(e)
    except subprocess.TimeoutExpired:
        why = pick("Zeitlimit überschritten", "time limit exceeded")
    finally:
        run(["docker", "rm", "-f", "-v", probe], check=False, timeout=120)
    if why:
        raise Abort(5, f"Probelauf in {label} {new} gescheitert ({why}) – nichts geändert, die Datenbank läuft "
                       f"mit ihren Daten weiter.",
                    f"Trial run in {label} {new} failed ({why}) – nothing changed, the database runs on with "
                    f"its data.")
    say(f"Probelauf: in {label} {new} eingespielt und gezählt – gleich.",
        f"Trial run: restored into {label} {new} and counted – the same.")


def cmd_major(name, new_tag, expect):
    """A database major: the plan of `dbmajor --dry-run` once more, and only
    if it still matches what the page showed."""
    if not FP_RE.match(expect):
        raise Abort(64, f"Ungültiger Fingerabdruck: {expect}", f"Invalid fingerprint: {expect}")
    lock()
    with before_change():
        plan, ctx = make_major(name, new_tag)
    mj = plan["major"]
    if not mj["possible"]:
        why = (mj["reason"] or "?") + (f" ({mj['detail']})" if mj.get("detail") else "")
        raise Abort(5, f"Datenbank-Major nicht möglich: {why}", f"Database major not possible: {why}")
    if mj["fingerprint"] != expect:
        raise Abort(5, "Dateien oder Daten haben sich seit der Vorschau geändert – nichts geändert, bitte neu planen.",
                    "The files or the data changed since the preview – nothing changed, please plan again.")
    return migrate(name, plan, ctx)


# Moving the data of a major: every entry of a directory into another on the
# same filesystem, by renames (nothing is copied). Each move is in the history
# before its first entry moves and marked done after its last ("move":
# {"step", "from", "to"}), with the signals held in between (held), so that
# the move, the places and the history always agree. Whatever ends the helper
# then, SIGKILL too, every entry is in one of the two places and the history
# says which way it was going: a rollback finishes or reverses an open move
# (swap_back). Steps: "aside" (the major: the old data into its place),
# "new-aside" and "old-back" (the rollback: the new major's data into a place
# of its own, then the old data back). Nothing here deletes data: a place is
# removed only while it is empty (drop_empty), and only once the history says
# its data is back where it belongs ("emptied", see drop_emptied).
MOVE_STEPS = ("aside", "new-aside", "old-back")


def move_entries(src, dst):
    """Move every entry of directory src into directory dst (renames). Stops
    before the first rename when dst has a name src has too (a rename would
    replace it). Raises OSError at the first entry that does not move; the
    ones before it have moved."""
    names = sorted(os.listdir(src))
    clash = sorted(set(names) & set(os.listdir(dst)))
    if clash:
        raise FileExistsError(f"{dst} has {', '.join(clash[:5])} as {src} has")
    for e in names:
        os.rename(os.path.join(src, e), os.path.join(dst, e))


def make_place(data, suffix, op_id):
    """An empty place for the data of a mount (data_mount), on its filesystem:
    {"type": "volume", "name", "path"} for a volume (labelled with the
    operation), {"type": "bind", "path"} for a directory next to it. Raises
    OSError (the place removed again) unless it is on the data's filesystem."""
    src = data["source"]
    if data["type"] == "volume":
        name = f"{data['name']}-{suffix}"
        run(["docker", "volume", "create", "--label", f"image-updates.op={op_id}", name])
        dst = run(["docker", "volume", "inspect", "-f", "{{.Mountpoint}}", name]).strip()
        kept = {"type": "volume", "name": name, "path": dst}
    else:
        dst = os.path.normpath(src) + "." + suffix
        st = os.stat(src)
        os.mkdir(dst, st.st_mode & 0o7777)
        os.chown(dst, st.st_uid, st.st_gid)
        kept = {"type": "bind", "path": dst}
    if not same_fs(src, dst):
        drop_empty(kept)
        raise OSError(f"{dst} is not on the filesystem of {src}")
    return kept


def drop_empty(kept):
    """Remove a place make_place made, only while it is empty: one with data
    in it stays (False)."""
    try:
        if os.listdir(kept["path"]):
            return False
    except FileNotFoundError:
        pass
    if kept["type"] == "volume":
        run(["docker", "volume", "rm", kept["name"]])
    elif os.path.isdir(kept["path"]):
        os.rmdir(kept["path"])
    return True


def drop_quietly(kept):
    """drop_empty where an empty place left behind is only untidy."""
    try:
        drop_empty(kept)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass


def drop_emptied(op):
    """Remove the place a move emptied, then let the history forget it. The
    history names it ("emptied") in the same line that says its move is over
    (old_data or new_data null, move null), before it goes: a kill before or
    after the removal leaves a state the next rollback finishes here. Up to
    0.25.0 the place went first, and a kill right after that left the history
    naming a place that was gone (old_data_home). Only a place make_place
    made, only while empty: one with data in it stays, and so does its record."""
    place = op.get("emptied")
    if not place:
        return
    try:
        gone = not kept_ok(place) or not kept_exists(place) or drop_empty(place)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        gone = False
    if gone:
        op["emptied"] = None
        record({"id": op["id"], "emptied": None})


def drop_kept(kept):
    """Release a place with its data (delete-olddata, on request only)."""
    if kept["type"] == "volume":
        run(["docker", "volume", "rm", kept["name"]])
    elif os.path.isdir(kept["path"]):
        shutil.rmtree(kept["path"])


def split_error(e, e2, src, dst):
    return RuntimeError(pick(
        f"Die Daten ließen sich nicht verschieben ({e}) und nicht zurücklegen ({e2}). Sie liegen teils in {src}, "
        f"teils in {dst}, wie der Verlauf es sagt; nichts ist gelöscht. Der Rollback legt sie zusammen.",
        f"The data could not be moved ({e}) nor put back ({e2}). It lies partly in {src}, partly in {dst}, as "
        f"the history says; nothing is deleted. The rollback puts it together."))


def set_aside(op, suffix):
    """Step 6 of a major, with the database stopped: its data (the old
    major's) into a place of its own. The place, the entries and the move are
    in the history before the first entry moves, the end of the move once the
    last has; signals wait until both are done. A rename that fails puts back
    what moved, records that, then removes the emptied place (drop_emptied);
    if putting back fails too, everything stays where it is and in the
    history (the rollback puts the data together)."""
    data = op["data"]
    src = data["source"]
    with held():
        kept = make_place(data, suffix, op["id"])
        op.update(old_data=kept, data_entries=sorted(os.listdir(src)),
                  move={"step": "aside", "from": src, "to": kept["path"]})
        record(op, log=op["id"] + ".log")
        try:
            move_entries(src, kept["path"])
        except OSError as e:
            try:
                move_entries(kept["path"], src)
            except OSError as e2:
                raise split_error(e, e2, src, kept["path"]) from e
            op.update(old_data=None, move=None, emptied=kept)
            record(op, log=op["id"] + ".log")
            drop_emptied(op)
            raise
        op["move"] = None
        record(op, log=op["id"] + ".log")
    return kept


def version_file_of(op):
    """The file in the data that names its major: PG_VERSION (pg_version_file),
    MariaDB's mysql_upgrade_info (version_file, if it was there), or None."""
    return op.get("pg_version_file") or op.get("version_file") or \
        ("PG_VERSION" if engine_of(op) == "postgres" else None)


def pg_version_of(op):
    """What the version file of the database's data says now (None if unreadable)."""
    if not version_file_of(op):
        return None
    path = os.path.join(op["data"]["source"], version_file_of(op))
    try:
        with open(path) as f:
            return f.read().strip().strip("\x00").strip()
    except OSError:
        return None


def data_restored(op):
    """None when the data of the database is the old major's again: the
    entries it had when the major set it aside, and PG_VERSION of the old
    major. Otherwise why not; a rollback starts no database on anything else
    (on an empty directory it would set up a new, empty cluster)."""
    src = op["data"]["source"]
    try:
        names = set(os.listdir(src))
    except OSError as e:
        return pick(f"{src} ist nicht lesbar ({e})", f"{src} cannot be read ({e})")
    want = op.get("data_entries")
    if want is not None and names != set(want):
        lack, extra = ", ".join(sorted(set(want) - names)) or "–", ", ".join(sorted(names - set(want))) or "–"
        return pick(f"In {src} fehlt {lack}, dazu kam {extra}: nicht die Daten, die der Major beiseitegelegt hat",
                    f"{src} lacks {lack} and has {extra} too: not the data the major set aside")
    if not names:
        return pick(f"{src} ist leer (die Datenbank legte darin einen neuen, leeren Cluster an)",
                    f"{src} is empty (the database would set up a new, empty cluster in it)")
    vf = version_file_of(op)
    path = os.path.join(src, vf) if vf else None
    if path and (op.get("pg_version_file") or op.get("version_file") or os.path.isfile(path)):
        v = pg_version_of(op)
        if lead_major(v) != op.get("from_major"):
            return pick(f"{path} nennt {v or 'nichts'} statt {ename(op)} {op.get('from_major')}",
                        f"{path} says {v or 'nothing'} instead of {ename(op)} {op.get('from_major')}")
    return None


def old_data_home(op):
    """True when the old data whose place (old_data) is gone lies whole in
    the data directory again. Up to 0.25.0 the end of a move back, and of a
    move aside taken back, removed the emptied place before the history said
    the move was over; a kill in between left the history naming that move
    and a place that is gone. Only such a move counts (old-back into the
    data directory, or aside out of it), never data whose place went in
    another way, and only when the data directory holds exactly the entries
    set aside and the old major's version file (data_restored): every entry
    moves whole by one rename, so with all of them there nothing is missing."""
    kept, mv, src = op.get("old_data"), op.get("move") or {}, (op.get("data") or {}).get("source")
    if not (src and kept_ok(kept)) or op.get("data_released") or op.get("data_entries") is None:
        return False
    if (mv.get("step"), mv.get("from"), mv.get("to")) not in (("old-back", kept["path"], src),
                                                                ("aside", src, kept["path"])):
        return False
    try:
        if kept_exists(kept):
            return False
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return data_restored(op) is None


def unrecorded_place(op):
    """Where a major of 0.23.0 to 0.24.0 may have moved the old data without
    the history knowing (it recorded the place only after the move, so a
    signal or a kill in between lost it): the place named after the
    operation, as that version named it, with data in it. None if there is
    none, and for a run since 0.24.1, which records every move before it
    begins (data_entries) and a place it emptied (emptied)."""
    data = op.get("data") or {}
    if op.get("old_data") or op.get("move") or not op.get("from_major") or data.get("type") not in ("volume", "bind") \
            or engine_of(op) != "postgres" or op.get("data_entries") is not None or op.get("emptied"):
        return None
    suffix = f"pg{op['from_major']}-{op['id'][:15]}"
    if data["type"] == "volume":
        name = f"{data.get('name')}-{suffix}"
        try:
            label = run(["docker", "volume", "inspect", "-f", '{{index .Labels "image-updates.op"}}', name],
                        timeout=60).strip()
            path = run(["docker", "volume", "inspect", "-f", "{{.Mountpoint}}", name], timeout=60).strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return None
        if label != op["id"]:
            return None
        place = {"type": "volume", "name": name, "path": path}
    else:
        place = {"type": "bind", "path": os.path.normpath(data["source"]) + "." + suffix}
    try:
        return place if kept_ok(place) and os.listdir(place["path"]) else None
    except OSError:
        return None


def unclear(de, en):
    return Abort(5, f"Unklarer Stand der Daten: {de} – nichts verschoben, die Datenbank startet nicht.",
                 f"Unclear state of the data: {en} – nothing moved, the database does not start.")


def kept_where(kept):
    return kept and (pick(f"Volume {kept['name']}", f"volume {kept['name']}") if kept["type"] == "volume"
                     else kept["path"])


def wait_ready(name, engine="postgres"):
    """The new major accepts connections over TCP: the server the image runs
    while it sets up a new cluster listens on its socket only (PostgreSQL) or
    on no network (MariaDB). False when the container stops or READY_TIMEOUT
    passes."""
    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        state = inspect(name)["State"] or {}
        if state.get("Status") in ("exited", "dead") or state.get("Restarting"):
            return False
        try:
            if subprocess.run(["docker", "exec", name, "sh", "-c", MY_READY if engine == "mariadb" else PG_READY],
                              capture_output=True, timeout=30).returncode == 0:
                return True
        except subprocess.TimeoutExpired:
            pass
        time.sleep(2)
    return False


def restore_into(name, rel, engine="postgres"):
    """Restore a dump (pg_dumpall / mariadb-dump, gzip) into the new major:
    its error lines (restore_errors). PostgreSQL: the database the image
    created first goes before. MariaDB: the client stops at the first error."""
    if engine == "mariadb":
        p = subprocess.run(["sh", "-c", 'gunzip -c "$1" | docker exec -i "$2" sh -c "$3"', "sh",
                            os.path.join(DUMPS, rel), name, MY_RESTORE],
                           capture_output=True, text=True, timeout=LIMITS["dump"])
        if p.returncode == 0:
            return []
        # The client frames the statement that failed with dashes; the ERROR line says why.
        lines = [ln for ln in (p.stderr or "").splitlines() if ln.strip() and set(ln.strip()) != {"-"}]
        return ([ln for ln in lines if ln.startswith("ERROR")] or lines)[-5:] or [f"exit {p.returncode}"]
    run(["docker", "exec", name, "sh", "-c", PG_DROP_FIRST], timeout=120)
    p = subprocess.run(["sh", "-c", 'gunzip -c "$1" | docker exec -i "$2" sh -c "$3"', "sh",
                        os.path.join(DUMPS, rel), name, PG_RESTORE],
                       capture_output=True, text=True, timeout=LIMITS["dump"])
    errors = restore_errors(p.stderr or "")
    if p.returncode != 0 and not errors:
        errors = [(p.stderr or "").strip()[-300:] or f"exit {p.returncode}"]
    return errors


def after_restore(name, engine):
    """What follows a restore. MariaDB: the privileges read again and
    mariadb-upgrade --force (raises when that fails). PostgreSQL: the
    statistics renewed (a failure is a note)."""
    if engine == "mariadb":
        run(["docker", "exec", name, "sh", "-c", MY_AFTER], timeout=LIMITS["dump"])
        return
    try:
        run(["docker", "exec", name, "sh", "-c", PG_ANALYZE], timeout=LIMITS["dump"])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        say("Hinweis: Die Statistiken ließen sich nicht erneuern (vacuumdb --analyze-only).",
            "Note: the statistics could not be renewed (vacuumdb --analyze-only).")


def login_failure(name, engine):
    """None when the user of the environment logs in over TCP with its
    password, as the waiting services do (MariaDB without an app user: root),
    else the last line the client said."""
    try:
        out = run(["docker", "exec", name, "sh", "-c", MY_LOGIN if engine == "mariadb" else PG_LOGIN],
                  timeout=60).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        lines = (getattr(e, "stderr", None) or str(e) or "").strip().splitlines()
        return lines[-1] if lines else "?"
    if out != "skipped":
        say("Anmeldung über TCP mit dem Passwort aus der Umgebung: in Ordnung.",
            "Login over TCP with the password from the environment: OK.")
    return None


def start_stopped(op, base):
    """Start the services a major stopped (best effort, for the cases that end
    before the database changed)."""
    if op.get("stopped"):
        stream(base + ["start", *op["stopped"]])


def undo_early(op, base):
    """A major that ends before the database changed: what it did goes back
    (drop_run), the services it stopped start again."""
    with held():
        drop_run(op)
        try:
            start_stopped(op, base)
        except (OSError, subprocess.SubprocessError, Timeout):
            say("Die gestoppten Dienste ließen sich nicht starten: " + ", ".join(op["stopped"]),
                "The services stopped could not be started: " + ", ".join(op["stopped"]))


def major_reason(name, source, line):
    return {"container": name, "state": None, "exit_code": None, "oom": False, "source": source, "line": line}


def migrate(name, plan, ctx):
    """The database major. In the history (kind "dbmajor") before the tag line
    is written; until the database is stopped anything that ends the run takes
    back what it did and starts the services it stopped (exit 5). From then on
    a failure is a failed run with its rollback (exit 4); the services that wait
    for the database stay stopped until it is rolled back or fixed."""
    rt, mj = plan["retag"], plan["major"]
    st = stamp()
    op_id = f"{st}-{name}"
    ref, new_ref = plan["image"], rt["image"]
    repo = split_ref(ref)[0]
    service, project = plan["services"][0], plan["project"]
    base = compose_cmd(ctx)
    old, new = mj["from_major"], mj["to_major"]
    engine, label = engine_of(mj), ename(mj)
    with before_change():
        c = inspect(name)
        old_id = c["Image"]
    # The file that names the old major in the mount: what a rollback checks
    # before it starts the old major again (data_restored). PostgreSQL:
    # PG_VERSION below PGDATA; MariaDB: mysql_upgrade_info, if there is one.
    pg_version_file = version_file = None
    if engine == "postgres":
        pg_version_file = os.path.normpath(os.path.join(
            os.path.relpath(env_of(c).get("PGDATA") or PGDATA_DEFAULT, mj["data"]["destination"]), "PG_VERSION"))
    else:
        rel = os.path.normpath(os.path.join(os.path.relpath(mj.get("datadir") or MY_DATADIR, mj["data"]["destination"]),
                                            MY_VERSION_FILE))
        if os.path.isfile(os.path.join(mj["data"]["source"], rel)):
            version_file = rel
    op = {"id": op_id, "ts": now_iso(), "kind": "dbmajor", "container": name, "project": project,
          "workdir": ctx["workdir"], "files": ctx["files"], "env_files": ctx["env_files"],
          "services": [service], "containers": [name], "image": ref, "old_id": old_id,
          "from_version": plan["installed_version"], "to_version": None,
          "to_image": new_ref, "from_tag": rt["from_tag"], "to_tag": rt["tag"],
          "changes": [{k: c[k] for k in ("file", "line", "old", "new")} for c in rt["changes"]],
          "from_major": old, "to_major": new, "data": mj["data"], "old_data": None, "stopped": [],
          "engine": engine, "pg_version_file": pg_version_file, "version_file": version_file,
          "data_entries": None, "move": None, "emptied": None,
          "rollback": None, "dumps": [], "status": "running", "pid": os.getpid()}
    say(f"Datenbank-Major {ref} → {new_ref} ({label} {old} → {new}) in Projekt {project}.",
        f"Database major {ref} to {new_ref} ({label} {old} to {new}) in project {project}.")
    say("Die Datenbank und die Dienste, die auf sie warten, sind bis zum Ende nicht erreichbar.",
        "The database and the services that wait for it are unavailable until the end.")

    # 1. The new image, and the extensions the databases use in it.
    with before_change():
        try:
            pull_rc = stream(["docker", "pull", new_ref])
        except Timeout as e:
            raise Abort(5, f"Der Pull hat nach {e.seconds} s nicht geendet – nichts geändert.",
                        f"The pull did not end within {e.seconds} s – nothing changed.")
        new_id = image_id(new_ref)
        if not new_id:
            raise Abort(5, f"Das Image {new_ref} ist nicht verfügbar – nichts geändert. Pull: "
                           f"{reason_line(STREAM_TAIL) or pull_rc}",
                        f"The image {new_ref} is not available – nothing changed. Pull: "
                        f"{reason_line(STREAM_TAIL) or pull_rc}")
        if pull_rc != 0:
            say(f"Warnung: Der Pull ist fehlgeschlagen; {new_ref} liegt schon auf dem Server und wird verwendet.",
                f"Warning: the pull failed; {new_ref} is on the server already and is used.")
        missing = missing_extensions(new_ref, mj["extensions"]) if engine == "postgres" else []
        if missing:
            raise Abort(5, f"{new_ref} hat die Erweiterungen {', '.join(missing)} nicht, die die Datenbanken nutzen "
                           f"– nichts geändert. Ein Image mit ihnen wählen (z. B. pgvector/pgvector).",
                        f"{new_ref} lacks the extensions {', '.join(missing)} the databases use – nothing changed. "
                        f"Choose an image that has them (e.g. pgvector/pgvector).")
        if mj["extensions"]:
            say(f"Erweiterungen im neuen Image vorhanden: {', '.join(mj['extensions'])}",
                f"Extensions present in the new image: {', '.join(mj['extensions'])}")
    need = (mj["data_bytes"] or 0) + FREE_MARGIN
    if plan["free_bytes"] is not None and plan["free_bytes"] < need:
        raise Abort(5, f"Zu wenig Platz in {BASE}: {human(plan['free_bytes'])} frei, {human(need)} nötig.",
                    f"Not enough space in {BASE}: {human(plan['free_bytes'])} free, {human(need)} needed.")

    # 2. - 5. Until the database is stopped: undone completely when anything ends the run.
    try:
        rb = f"{repo}:rollback-{st}"
        run(["docker", "tag", old_id, rb])
        op["rollback"] = rb
        say(f"Rollback-Sicherung: {rb}", f"Rollback image: {rb}")
        if mj["stop"]:
            op["stopped"] = list(mj["stop"])
            say(f"Gestoppt, damit nichts mehr schreibt: {', '.join(mj['stop'])}",
                f"Stopped, so that nothing writes any more: {', '.join(mj['stop'])}")
            if stream(base + ["stop", *mj["stop"]]) != 0:
                raise Abort(5, "Die wartenden Dienste ließen sich nicht stoppen – nichts geändert.",
                            "The waiting services could not be stopped – nothing changed.")
        db = next(d for d in plan["dbs"] if d["name"] == name)
        op["dumps"].append(dump_db(project, db, st, MY_MAJOR_DUMP if engine == "mariadb" else None))
        before = table_counts(name, engine)
        say(f"Gezählt: {count_summary(before)}", f"Counted: {count_summary(before)}")
        grants = None
        if engine == "mariadb":
            try:
                grants = account_grants(name)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                detail = (getattr(e, "stderr", None) or str(e)).strip()[-300:]
                raise Abort(5, f"Benutzer, Rollen und Rechte ließen sich nicht lesen ({detail}) – nichts geändert.",
                            f"The users, roles and grants could not be read ({detail}) – nothing changed.")
            say(f"Benutzer und Rollen: {len(grants)}, mit {sum(len(v) for v in grants.values())} Rechtezeilen",
                f"Users and roles: {len(grants)}, with {sum(len(v) for v in grants.values())} grant lines")
        # The data the major moves must be this server's: a rollback checks it by the same file.
        running = old
        if engine == "mariadb":
            try:
                running = lead_major((pg_lines(name, MY_SERVER_VERSION, timeout=60) or [""])[0])
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                running = None
        if running != old or (version_file_of(op) and lead_major(pg_version_of(op)) != old):
            where = os.path.join(mj["data"]["source"], version_file_of(op) or "")
            raise Abort(5, f"Server oder {where} nennen nicht {label} {old} – nichts geändert.",
                        f"The server or {where} do not say {label} {old} – nothing changed.")
        # 4b. The trial run: does the new major take the dump, are the counts the same?
        probe_restore(name, plan, op, before, grants)
        change_files(op, ctx, rt, [service], st)
    except Interrupted as e:
        undo_early(op, base)
        raise stopped(e, "Dump, Rollback-Kopie und Dateien zurückgenommen, die gestoppten Dienste wieder gestartet.",
                      "dump, rollback copy and files taken back, the stopped services started again.")
    except BaseException:
        undo_early(op, base)
        raise

    # 6. - 8. The database changes from here on.
    ok, reason = False, None
    try:
        if stream(base + ["stop", service]) != 0:
            raise RuntimeError(pick("docker compose stop schlug fehl", "docker compose stop failed"))
        set_aside(op, f"{PLACE_PREFIX[engine]}{old}-{st}")
        say(f"Die Daten von {label} {old} liegen jetzt in {kept_where(op['old_data'])}; sie bleiben, bis sie "
            f"freigegeben werden.",
            f"The data of {label} {old} is now in {kept_where(op['old_data'])}; it stays until released.")
        rc = stream(base + ["up", "-d", "--no-deps", "--pull", "never", service])
        if rc != 0 or not wait_ready(name, engine):
            show_logs([name])
            reason = failure_reason([name], compose_rc=rc)
            raise RuntimeError("not ready")
        op["new_id"] = new_id
        op["to_version"] = version_of(new_ref, new_id)
        say(f"{label} {new} läuft. Spiele den Dump ein …", f"{label} {new} runs. Restoring the dump …")
        errors = restore_into(name, op["dumps"][0], engine)
        if errors:
            for ln in errors[:20]:
                print(ln)
            reason = major_reason(name, "restore", errors[0][-REASON_MAX:])
            raise RuntimeError("restore")
        if engine == "mariadb":
            # The system tables of the dump's version brought up, then a restart on them.
            try:
                after_restore(name, engine)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                lines = (getattr(e, "stderr", None) or getattr(e, "output", None) or str(e) or "").strip().splitlines()
                reason = major_reason(name, "upgrade", "mariadb-upgrade: " + (lines[-1] if lines else "?")[:REASON_MAX - 20])
                raise RuntimeError("upgrade")
            say("Rechte neu gelesen, mariadb-upgrade gelaufen; MariaDB startet neu …",
                "Privileges read again, mariadb-upgrade done; MariaDB restarts …")
            rc = stream(base + ["restart", service])
            if rc != 0 or not wait_ready(name, engine):
                show_logs([name])
                reason = failure_reason([name], compose_rc=rc)
                raise RuntimeError("restart")
        after = table_counts(name, engine)
        diff = compare_counts(before, after)
        if diff:
            for ln in diff[:20]:
                print(ln)
            reason = major_reason(name, "counts", pick(f"Zeilen weichen ab: {diff[0]}", f"rows differ: {diff[0]}"))
            raise RuntimeError("counts")
        say(f"Gezählt nach dem Einspielen: {count_summary(after)} – gleich.",
            f"Counted after the restore: {count_summary(after)} – the same.")
        if grants is not None:
            diff = compare_grants(grants, account_grants(name))
            if diff:
                for ln in diff[:20]:
                    print(ln)
                reason = major_reason(name, "grants", pick(f"Rechte weichen ab: {diff[0]}",
                                                           f"privileges differ: {diff[0]}")[:REASON_MAX])
                raise RuntimeError("grants")
            say("Benutzer, Rollen und Rechte nach dem Einspielen: gleich.",
                "Users, roles and grants after the restore: the same.")
        # The services that wait for the database log in over the network with a password.
        refused = login_failure(name, engine)
        if refused is not None:
            reason = major_reason(name, "login", pick("Anmeldung über TCP gescheitert: ", "Login over TCP failed: ")
                                  + refused[:REASON_MAX - 40])
            raise RuntimeError("login")
        if engine == "postgres":
            after_restore(name, engine)
        # 9. The services that wait for the database
        if op["stopped"]:
            rc = stream(base + ["start", *op["stopped"]])
            started, failed, _ = (False, [], {}) if rc != 0 else wait_healthy(project, op["stopped"])
            if not started:
                names = failed or [n for d in plan["dependents"] for n in d["containers"]]
                show_logs(names)
                reason = failure_reason(names, compose_rc=rc)
                raise RuntimeError("dependents")
        ok = True
    except (Exception, Interrupted) as e:
        if reason is None:
            reason = error_reason(e)
    versions = {"from_version": op["from_version"], "to_version": op["to_version"], "image": new_ref,
                "backup": op.get("backup"), "old_data": kept_where(op["old_data"])}
    if not ok:
        op["status"], op["reason"] = "failed", reason
        record(op, log=op_id + ".log")
        say_reason(reason)
        say("Datenbank-Major fehlgeschlagen. Zurückrollen ist möglich: Es legt die alten Daten zurück und stellt "
            "die Dateien wieder her. Die wartenden Dienste bleiben bis dahin gestoppt.",
            "Database major failed. A rollback is possible: it moves the old data back and restores the files. "
            "The waiting services stay stopped until then.")
        result(op=op_id, status="failed", rollback=op["rollback"], dumps=op["dumps"], reason=reason, **versions)
        return 4
    op["status"] = "ok"
    record(op, log=op_id + ".log")
    say(f"Datenbank-Major erfolgreich: {label} {new}.", f"Database major successful: {label} {new}.")
    try:
        start_radar()
    except Interrupted as e:
        say(f"{e.name}: Radar-Start übersprungen.", f"{e.name}: radar start skipped.")
    result(op=op_id, status="ok", rollback=op["rollback"], dumps=op["dumps"], **versions)
    return 0


def swap_back(op, base):
    """The rollback of a major, once the files are back: the data of the
    database the old major's again, before anything starts it. With the old
    data set aside (old_data) it stops the database and the services that wait
    for it, then, each move in the history before it begins (see set_aside):
      - a move aside the major left open (a signal, a kill, a failed undo):
        the rest of the old data goes back; the new major never ran;
      - otherwise the new major's data, if any, into a place of its own (kept
        until released), then the old data back;
      - a move an earlier rollback left open goes on where it stopped.
    Then, and also when nothing had to move, the data must be the old
    major's (data_restored), or the rollback fails with everything where it
    is. A move that is over is in the history before its emptied place goes
    (drop_emptied, which also removes one an earlier run left). A state that
    does not fit stops it before anything moves (exit 5). Returns where the
    new major's data is kept, or None."""
    src = op["data"]["source"]
    drop_emptied(op)
    found = unrecorded_place(op)
    if found:
        op.update(old_data=found, move={"step": "aside", "from": src, "to": found["path"]})
        record({"id": op["id"], "old_data": found, "move": op["move"]})
        say(f"Die alten Daten liegen in {kept_where(found)}, ohne dass der Verlauf es wusste (ein Lauf vor 0.24.1, "
            f"beim Verschieben beendet); sie kommen zurück.",
            f"The old data is in {kept_where(found)} without the history knowing (a run before 0.24.1 that ended "
            f"while it moved the data); it goes back.")
    kept, new_kept, mv = op.get("old_data"), op.get("new_data"), op.get("move")
    step = (mv or {}).get("step")
    if kept and step == "new-aside" and kept_ok(new_kept) and (mv.get("from"), mv.get("to")) == (src, new_kept["path"]) \
            and not kept_exists(new_kept) and os.path.isdir(src) and os.listdir(src):
        # Up to 0.25.0 a move of the new major's data that failed and was taken
        # back removed its emptied place before the history said so: the data
        # is whole in the data directory again (a place goes only while empty).
        op.update(new_data=None, move=None)
        record({"id": op["id"], "new_data": None, "move": None})
        new_kept, mv, step = None, None, None
        say("Die Daten des neuen Majors liegen wieder im Datenverzeichnis; ein früherer Rollback endete, nachdem er "
            "ihren leeren Ort entfernt hatte. Sie bekommen einen neuen.",
            "The new major's data is back in the data directory; an earlier rollback ended after it removed their "
            "empty place. They get a new one.")
    if kept:
        if mv and (step not in MOVE_STEPS or src not in (mv.get("from"), mv.get("to"))):
            raise unclear(f"der Verlauf nennt eine Verschiebung, die nicht passt ({mv})",
                          f"the history names a move that does not fit ({mv})")
        if not os.path.isdir(kept["path"]) or not os.path.isdir(src):
            raise unclear(f"{kept['path']} oder {src} fehlt", f"{kept['path']} or {src} is missing")
        if step == "new-aside" and not (new_kept and os.path.isdir(new_kept.get("path") or "")):
            raise unclear("der Ort für die Daten des neuen Majors fehlt", "the place for the new major's data is missing")
        if step == "aside":
            clash = sorted(set(os.listdir(kept["path"])) & set(os.listdir(src)))
            if clash:
                raise unclear(f"{src} und {kept['path']} haben beide {', '.join(clash[:5])}",
                              f"{src} and {kept['path']} both have {', '.join(clash[:5])}")
        # The services that wait for the database stop as well (the rollback starts them again).
        if stream(base + ["stop", *(op.get("stopped") or []), *op["services"]]) != 0:
            raise RuntimeError(pick("docker compose stop schlug fehl", "docker compose stop failed"))
    with held():
        if kept and step != "aside":
            if step == "new-aside" or (step is None and os.listdir(src)):
                if step != "new-aside":
                    new_kept = make_place(op["data"], f"{PLACE_PREFIX[engine_of(op)]}{op['to_major']}-{stamp()}",
                                          op["id"])
                    op.update(new_data=new_kept, move={"step": "new-aside", "from": src, "to": new_kept["path"]})
                    record({"id": op["id"], "new_data": new_kept, "move": op["move"]})
                try:
                    move_entries(src, new_kept["path"])
                except OSError as e:
                    # As it was before this rollback, if it can be: the new major's data in its place.
                    try:
                        move_entries(new_kept["path"], src)
                    except OSError as e2:
                        raise split_error(e, e2, src, new_kept["path"]) from e
                    op.update(new_data=None, move=None, emptied=new_kept)
                    record({"id": op["id"], "new_data": None, "move": None, "emptied": new_kept})
                    drop_emptied(op)
                    raise
            op["move"] = {"step": "old-back", "from": kept["path"], "to": src}
            record({"id": op["id"], "move": op["move"]})
        if kept:
            move_entries(kept["path"], src)
        why = data_restored(op)
        if why:
            raise RuntimeError(pick(f"Die Daten sind nicht die alten: {why}. Die Datenbank startet nicht; alles bleibt, "
                                    f"wo es ist.",
                                    f"The data is not the old data: {why}. The database does not start; everything "
                                    f"stays where it is."))
        if kept:
            op.update(old_data=None, move=None, emptied=kept)
            record({"id": op["id"], "old_data": None, "move": None, "emptied": kept})
            drop_emptied(op)
    if kept:
        say(f"Die Daten von {ename(op)} {op['from_major']} liegen wieder an ihrem Ort"
            + (f"; die von {ename(op)} {op['to_major']} bleiben in {kept_where(op.get('new_data'))}, bis sie "
               f"freigegeben werden." if op.get("new_data") else "."),
            f"The data of {ename(op)} {op['from_major']} is back in its place"
            + (f"; that of {ename(op)} {op['to_major']} stays in {kept_where(op.get('new_data'))} until released."
               if op.get("new_data") else "."))
    return op.get("new_data")


def kept_of(op):
    """The data a major keeps that is not in use: the old major's after a run
    that was not rolled back, the new major's after a rollback."""
    return op.get("new_data") if op.get("rolled_back") else op.get("old_data")


def kept_ok(kept):
    """A place make_place made, by its name: nothing else is ever deleted."""
    if not isinstance(kept, dict):
        return False
    if kept.get("type") == "volume":
        return bool(KEPT_RE.match(kept.get("name") or ""))
    path = kept.get("path") or ""
    return kept.get("type") == "bind" and bool(KEPT_RE.match(os.path.basename(path))) and os.path.isabs(path) \
        and path == os.path.normpath(path) and not os.path.islink(path)


def kept_bytes(kept):
    try:
        out = run(["du", "-sb", kept["path"]], timeout=60)
        return int(out.split()[0])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError, IndexError):
        return None


def kept_exists(kept):
    if kept["type"] == "volume":
        return run(["docker", "volume", "inspect", kept["name"]], check=False, timeout=60).strip() not in ("", "[]")
    return os.path.isdir(kept["path"])


def cmd_delete_olddata(op_id):
    if not OP_RE.match(op_id):
        raise Abort(64, f"Ungültige Vorgangs-ID: {op_id}", f"Invalid operation id: {op_id}")
    lock()
    op = load_ops().get(op_id)
    if not op or op.get("kind") != "dbmajor":
        raise Abort(2, f"Vorgang {op_id} nicht gefunden.", f"Operation {op_id} not found.")
    if op.get("status") != "ok" and not op.get("rolled_back"):
        raise Abort(3, "Die alten Daten sind der Weg zurück, solange der Major nicht gelungen oder zurückgerollt ist.",
                    "The old data is the way back as long as the major has not succeeded or been rolled back.")
    if op.get("move"):
        raise Abort(3, "Eine Verschiebung dieser Daten ist nicht zu Ende; erst zurückrollen.",
                    "A move of this data is not finished; roll back first.")
    kept = kept_of(op)
    if not kept_ok(kept) or not kept_exists(kept):
        raise Abort(2, "Keine aufbewahrten Daten zu diesem Vorgang.", "No kept data for this operation.")
    say(f"Gebe {kept_where(kept)} frei …", f"Releasing {kept_where(kept)} …")
    drop_kept(kept)
    record({"id": op_id, "data_released": now_iso()})
    say("Freigegeben.", "Released.")
    return 0


def compose_images(ctx, services):
    """{service: image} as `docker compose config` gives it now: its image:
    line, or for a service with `build:` and none the name Compose v2 gives
    the image it builds, <project>-<service>, in the project compose runs it
    in (the config's "name", from -p); None for a service the files do not
    define. Exit 5 if compose cannot read its files."""
    try:
        cfg = json.loads(run(compose_cmd(ctx) + ["config", "--format", "json"]))
    except (subprocess.CalledProcessError, ValueError, OSError) as e:
        detail = (getattr(e, "stderr", None) or str(e)).strip()[-300:]
        raise Abort(5, f"docker compose config schlägt fehl: {detail} – nichts geändert.",
                    f"docker compose config fails: {detail} – nothing changed.")
    svc = cfg.get("services") or {}
    project = cfg.get("name") or ctx["project"]
    out = {}
    for s in services:
        c = svc.get(s)
        if not isinstance(c, dict):
            out[s] = None
        elif c.get("image"):
            out[s] = c["image"]
        else:
            out[s] = f"{project}-{s}" if c.get("build") is not None else None
    return out


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
    if not op or op.get("kind") not in ("update", "retag", "dbmajor", "rebuild"):
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
    saved = (op.get("backup_files") or []) if op.get("kind") in ("retag", "dbmajor") else []
    major = op.get("kind") == "dbmajor"
    # A major that moved the data aside moves it back: the old data must still be
    # there, or whole in its place already (a run up to 0.25.0 that was killed
    # right after it removed the emptied place, old_data_home).
    if major and op.get("old_data") and not (kept_ok(op["old_data"]) and kept_exists(op["old_data"])):
        if not old_data_home(op):
            raise Abort(2, f"Die alten Daten ({kept_where(op['old_data'])}) sind nicht mehr da – nichts geändert.",
                        f"The old data ({kept_where(op['old_data'])}) is gone – nothing changed.")
        where = kept_where(op["old_data"])
        op.update(old_data=None, move=None)
        record({"id": op_id, "old_data": None, "move": None})
        say(f"Die alten Daten liegen schon wieder vollständig an ihrem Ort; {where} hat ein früherer Lauf leer "
            f"entfernt, bevor er das festhalten konnte.",
            f"The old data is already back in its place, whole; an earlier run removed {where} empty before it "
            f"could record that.")
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
    # A key-value store's new major goes back with the data saved before it
    # (save_store): every copy must be there and readable, and its container
    # must still keep its files where the copy came from.
    copies = (op.get("data_copies") or []) if op.get("kind") == "retag" else []
    for x in copies:
        path = copy_path(x.get("copy"))
        if not path or not os.path.isfile(path) or not tarfile.is_tarfile(path):
            raise Abort(2, f"Die Datenkopie {x.get('copy')} fehlt oder ist beschädigt – nichts geändert.",
                        f"The data copy {x.get('copy')} is missing or damaged – nothing changed.")
        try:
            m = store_mount(inspect(x["container"]))
        except Abort:
            m = None
        if not m or m["source"] != x["source"]:
            raise Abort(5, f"{x['container']} hält seine Daten nicht mehr in {x['source']} – nichts geändert. Die "
                           f"Kopie liegt in {path}.",
                        f"{x['container']} no longer keeps its data in {x['source']} – nothing changed. The copy "
                        f"is in {path}.")
    # The files must give the services the operation's image (other_image):
    # without a tag change checked before anything changes, with one once its
    # files are back, which are written again as they were if not.
    now = {}            # the files as they were before this rollback put any back
    try:
        if not saved:
            other_image(ctx, op)
        say(f"Rollback von {op['image']} auf {rb} (Projekt {op['project']}) …",
            f"Rolling {op['image']} back to {rb} (project {op['project']}) …")
        if major:
            say(f"Die Datenbank kehrt mit ihren alten Daten zu {ename(op)} {op.get('from_major')} zurück.",
                f"The database returns to {ename(op)} {op.get('from_major')} with its old data.")
        elif copies:
            store = op.get("store") or "Store"
            say(f"{store} bekommt seine Daten von vor dem Tag-Wechsel zurück; was es seitdem geschrieben hat, geht "
                f"verloren.",
                f"{store} gets back its data from before the tag change; what it wrote since then is lost.")
        else:
            say("Achtung: Datenbanken werden NICHT zurückgesetzt. Bei Migrationsproblemen den Dump einspielen.",
                "Note: databases are NOT restored. If migrations cause trouble, restore the dump.")
        if saved:
            for x in pending:
                with open(os.path.realpath(x["path"]), "rb") as f:
                    now[x["path"]] = f.read()
            restore_files(pending)
            other_image(ctx, op)
            for x in pending:
                say(f"Wiederhergestellt: {x['path']}", f"Restored: {x['path']}")
    except (Abort, Interrupted) as e:
        with held():
            for path, data in now.items():
                write_like(path, data)
        if isinstance(e, Interrupted):
            raise Abort(5, f"Rollback durch {e.name} abgebrochen, bevor ein Container geändert wurde – nichts "
                           f"geändert.",
                        f"Rollback stopped by {e.name} before any container changed – nothing changed.")
        raise
    # The containers change from here on: the output goes to the log as printed.
    keep_log(op_id + ".rollback.log")
    ok, reason = False, None
    try:
        if major:
            try:
                swap_back(op, compose_cmd(ctx))
            except Exception as e:      # the data is not back: say so (a signal is error_reason's)
                reason = major_reason(op["container"], "data", str(e)[:REASON_MAX])
                raise
        if copies:
            # Stopped, the store's directory takes the copy; unpacking is never cut in two.
            try:
                if stream(compose_cmd(ctx) + ["stop", *op["services"]]) != 0:
                    raise RuntimeError(pick("docker compose stop schlug fehl", "docker compose stop failed"))
                with held():
                    for x in copies:
                        unpack_dir(copy_path(x["copy"]), x["source"])
                        say(f"Daten zurückgelegt: {x['source']} (Stand vor dem Tag-Wechsel)",
                            f"Data put back: {x['source']} (as before the tag change)")
            except Exception as e:
                reason = major_reason(op["container"], "data", str(e)[:REASON_MAX])
                raise
        run(["docker", "tag", rb, op["image"]])
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
        # A major stopped the services that wait for the database: they start again.
        if ok and major and op.get("stopped"):
            rc = stream(compose_cmd(ctx) + ["start", *op["stopped"]])
            ok, failed, _ = (False, [], {}) if rc != 0 else wait_healthy(op["project"], op["stopped"])
            if not ok:
                show_logs(failed or op["stopped"])
                reason = failure_reason(failed or op["stopped"], compose_rc=rc)
    except (Exception, Interrupted) as e:
        ok, reason = False, reason or error_reason(e)
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
        could = (o.get("kind") in ("update", "retag", "dbmajor", "rebuild") and o.get("status") in ("ok", "failed", "running")
                 and not o["active"] and not (o.get("rolled_back") and o.get("rollback_status") != "failed"))
        o["can_rollback"] = could and norm_ref(o.get("rollback") or "") in rb_refs
        if could and o.get("rollback") and not o["can_rollback"]:
            # The rollback copy is gone: deleted by the cleanup, in the backups tab or outside the
            # add-on (docker image prune -a). The page says so instead of only leaving the button out.
            o["rollback_gone"] = True
        for k, iid in (("from_version", o.get("old_id")), ("to_version", o.get("new_id"))):
            if refines(known.get(iid), o.get(k)):
                o[k] = known[iid]
        # A key-value store's data saved before its new major: its size, and
        # without the copies no rollback (it would start the old major on newer files).
        copies = o.pop("data_copies", None) or []
        if copies:
            there = all(copy_path(x.get("copy")) and os.path.isfile(copy_path(x["copy"])) for x in copies)
            o["data_copy"] = {"bytes": sum(x.get("bytes") or 0 for x in copies), "there": there}
            if o.get("can_rollback") and not there:
                o["can_rollback"] = False
                o["data_gone"] = True
        for k in ("workdir", "files", "env_files", "old_id", "new_id", "backup_files", "pid"):
            o.pop(k, None)
        # The data a database major keeps (kept_of), while it is there.
        if o.get("kind") == "dbmajor":
            kept = None if o.get("data_released") else kept_of(o)
            if kept_ok(kept) and kept_exists(kept):
                o["kept"] = {"where": kept_where(kept), "bytes": kept_bytes(kept),
                             "major": o.get("to_major") if o.get("rolled_back") else o.get("from_major"),
                             "can_release": (o.get("status") == "ok" or bool(o.get("rolled_back")))
                             and not o.get("move")}
            # Without the old data a rollback has nothing to move back, unless it is back already.
            if o.get("can_rollback") and not o.get("rolled_back") and o.get("old_data") and "kept" not in o \
                    and not old_data_home(o):
                o["can_rollback"] = False
            for k in ("data", "old_data", "new_data", "move", "emptied", "data_entries", "pg_version_file",
                      "version_file"):
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
    # Without -f: Docker refuses if a container still uses the image. Its runs'
    # data copies go with it.
    try:
        rc = stream(["docker", "rmi", ref])
        if rc == 0:
            drop_copies_of(ref)
        return rc
    except Timeout as e:
        raise Abort(1, f"docker rmi hat nach {e.seconds} s nicht geendet und wurde abgebrochen; ob {ref} noch da "
                       f"ist, zeigt die Übersicht nach dem Neuladen.",
                    f"docker rmi did not end within {e.seconds} s and was stopped; whether {ref} is still there "
                    f"shows after a reload.")
    except Interrupted as e:
        raise Abort(1, f"docker rmi durch {e.name} abgebrochen; ob {ref} noch da ist, zeigt die Übersicht nach "
                       f"dem Neuladen.",
                    f"docker rmi stopped by {e.name}; whether {ref} is still there shows after a reload.")


# --------------------------------------------------------------- cleanup ----
# KEY in /etc/image-updates.conf -> (rule, default). 0 switches a rule off.
RULES = {"KEEP_DUMPS": ("keep_dumps", 3), "ROLLBACK_IMAGE_DAYS": ("image_days", 14)}
RULE_VALUE_RE = re.compile(r"^\d{1,4}$")
# KEY -> (LIMITS key, lowest, highest), in seconds (see LIMITS).
LIMIT_KEYS = {"DOCKER_TIMEOUT": ("docker", 60, 86400), "DUMP_TIMEOUT": ("dump", 60, 172800)}
LIMIT_VALUE_RE = re.compile(r"^\d{1,6}$")


def read_conf(nothing_de, nothing_en):
    """{KEY: int} from /etc/image-updates.conf, {} without the file. The file
    must belong to root and be writable by root only; a line not understood,
    an unknown key or a value out of range raises Abort(2) with nothing_* as
    the consequence ("nothing deleted", "nothing changed")."""
    try:
        st = os.stat(CONF)
    except FileNotFoundError:
        return {}
    if st.st_uid not in (0, os.geteuid()) or st.st_mode & 0o022:
        raise Abort(2, f"{CONF} muss root gehören und darf nur für root schreibbar sein – {nothing_de}.",
                    f"{CONF} must belong to root and be writable by root only – {nothing_en}.")
    found = {}
    with open(CONF) as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = (x.strip() for x in line.partition("="))
            value = value.strip("\"'")
            ok = sep and ((key in RULES and RULE_VALUE_RE.match(value))
                          or (key in LIMIT_KEYS and LIMIT_VALUE_RE.match(value)
                              and LIMIT_KEYS[key][1] <= int(value) <= LIMIT_KEYS[key][2]))
            if not ok:
                hint = (f" ({LIMIT_KEYS[key][1]}–{LIMIT_KEYS[key][2]} s)" if key in LIMIT_KEYS else "")
                raise Abort(2, f"{CONF}, Zeile {n} nicht verstanden: {line[:60]}{hint} – {nothing_de}.",
                            f"{CONF}, line {n} not understood: {line[:60]}{hint} – {nothing_en}.")
            found[key] = int(value)
    return found


def load_limits():
    """LIMITS from /etc/image-updates.conf, before an update, a rollback or
    a delete changes anything; a file not understood stops it (exit 2)."""
    found = read_conf("nichts geändert", "nothing changed")
    for key, (name, _, _) in LIMIT_KEYS.items():
        if key in found:
            LIMITS[name] = found[key]


def load_rules():
    """Cleanup rules: the defaults, overridden by /etc/image-updates.conf
    (KEY=VALUE lines, # comments). The file must belong to root and be
    writable by root only. Anything not understood stops the cleanup instead
    of guessing - a typo must never delete more than intended."""
    rules = {name: default for name, default in RULES.values()}
    rules["file"] = None
    if not os.path.exists(CONF):
        return rules
    for key, value in read_conf("nichts gelöscht", "nothing deleted").items():
        if key in RULES:
            rules[RULES[key][0]] = value
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
        drop_copies_of(i["ref"])
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


# ---------------------------------------------------------------- status ----
def cmd_status(args):
    """The status contract (diunstatus): read-only, without a lock and also
    without root. German for de, de-DE, …; English otherwise."""
    if args and not (len(args) == 2 and args[0] == "--lang" and LANG_ARG_RE.match(args[1])):
        raise Abort(64, "Aufruf: status --lang en|de", "Usage: status --lang en|de")
    lang = "de" if args and args[1].lower().startswith("de") else "en"
    print(json.dumps(diunstatus.show(lang), ensure_ascii=False))
    return 0


def refresh_status():
    """status.json anew after an update, a tag change, a rollback or a
    cleanup, so the Status Board sees the result before the next radar run.
    Silent, so @@RESULT stays the last line; a problem keeps the last file."""
    try:
        diunstatus.refresh()
    except Exception:
        pass


# ------------------------------------------------------------------ main ----
def main(argv):
    if argv[:1] == ["status"]:
        return cmd_status(argv[1:])
    if os.geteuid() != 0:
        raise Abort(77, "Dafür werden Administratorrechte benötigt.", "This requires administrator privileges.")
    if len(argv) == 2 and argv[0] == "plan":
        plan, _ = make_plan(argv[1])
        print(json.dumps(plan, ensure_ascii=False))
        return 0
    if argv[:1] in (["update"], ["pull"], ["rollback"], ["delete-image"]) or argv[:1] == ["rebuild"] and \
            "--dry-run" not in argv or \
            argv[:1] in (["retag"], ["dbmajor"]) and "--expect" in argv:
        load_limits()
    if len(argv) in (2, 3) and argv[0] == "update" and (len(argv) == 2 or argv[2] == "--no-dump"):
        return keep_output(cmd_update, argv[1], do_dump=len(argv) == 2)
    if len(argv) == 2 and argv[0] == "pull":
        return cmd_pull(argv[1])
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
    if len(argv) == 4 and argv[0] == "dbmajor" and argv[3] == "--dry-run":
        print(json.dumps(make_major(argv[1], argv[2])[0], ensure_ascii=False))
        return 0
    if len(argv) == 5 and argv[0] == "dbmajor" and argv[3] == "--expect":
        return keep_output(cmd_major, argv[1], argv[2], argv[4])
    if len(argv) == 3 and argv[0] == "rebuild" and argv[2] == "--dry-run":
        print(json.dumps(make_plan(argv[1], build=True)[0], ensure_ascii=False))
        return 0
    if len(argv) in (2, 3) and argv[0] == "rebuild" and argv[2:] in ([], ["--no-dump"]):
        return keep_output(cmd_rebuild, argv[1], do_dump=len(argv) == 2)
    if len(argv) == 2 and argv[0] == "delete-olddata":
        return cmd_delete_olddata(argv[1])
    raise Abort(64, "Unbekannter Befehl: " + " ".join(argv), "Unknown command: " + " ".join(argv))


def entry(argv):
    """main() as the command line runs it: a message for an error, and the
    exit code. SIGTERM, SIGHUP and SIGINT end a run through its cleanup
    (diunproc.on_signals): the docker command running is stopped, the run
    takes back what it did or records what is left, and only then does the
    process end and free the lock. The code is the run's own (5: stopped
    before any change, 4: failed afterwards, with its rollback), else 128 +
    the signal's number."""
    guard = None
    try:
        with on_signals() as guard:
            code = main(argv)
    except Abort as e:
        print(str(e), flush=True)
        code = e.code
    except subprocess.CalledProcessError as e:
        print((e.stderr or str(e)).strip(), flush=True)
        code = 1
    except (Timeout, subprocess.TimeoutExpired) as e:
        print(pick(f"Abgebrochen, Zeitgrenze überschritten: {e}", f"Stopped, time limit passed: {e}"), flush=True)
        code = 1
    except Interrupted as e:
        print(pick(f"Abgebrochen durch {e.name}.", f"Stopped by {e.name}."), flush=True)
        code = 128 + e.signum
    if guard is not None and guard.repeats:
        say(f"{guard.repeats} weitere(s) Signal(e) während des Aufräumens nicht beachtet.",
            f"{guard.repeats} more signal(s) ignored while cleaning up.")
    if argv[:1] in (["update"], ["pull"], ["retag"], ["dbmajor"], ["rebuild"], ["rollback"], ["cleanup"]) and "--dry-run" not in argv \
            and code not in (64, 75, 77):
        refresh_status()
    return code


if __name__ == "__main__":
    sys.exit(entry(sys.argv[1:]))

#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Update / rollback / backup helper for the Cockpit Image-Updates plugin.

Called by the plugin via cockpit.spawn with a FIXED set of commands only:

  plan <container>              read-only: what an update would touch (JSON),
                                including whether the size of the jump is unknown
  update <container> [--no-dump]
                                1. dump every supported database container of
                                   the compose project (pg_dumpall / mariadb-dump)
                                2. tag the running image as <repo>:rollback-<stamp>
                                3. docker compose pull + up -d --no-deps for every
                                   service of the project that uses the same image
                                4. wait until the new containers run (healthy);
                                   if they do not, name the most likely cause
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

Same tag only ("stage 1"): the image reference in the compose file is never
changed. Images built locally or pinned by digest are not updated.

Everything the addon writes lives in /var/backups/image-updates (root only):
  dumps/<project>/<stamp>_<container>.sql.gz
  operations.jsonl              one JSON line per update / rollback
  logs/<op-id>.log              output of the update (.rollback.log: of its rollback)

The machine-readable result of update/rollback/cleanup is the last line:
  @@RESULT {"op": "...", "status": "ok|nochange|failed", ...}

Lives in /usr/local/lib/diun-radar (root-owned, not mounted into any
container). DIUN_LANG=de|en selects the language of the log messages.
"""

import fcntl
import gzip
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
from diunlib import local_version, tag_digits  # noqa: E402

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
FREE_MARGIN = 1 << 30    # keep at least 1 GiB free beyond the dump estimate

LANG = "de" if os.environ.get("DIUN_LANG", "en") == "de" else "en"


def say(de, en):
    print(de if LANG == "de" else en, flush=True)


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


def version_of(ref, image_id):
    """Version of a local image for the history: the tag or the app's version
    variable (diunlib), otherwise what the radar found for this image ID
    (label, digest). None if unknown. Never fails - the history is only info."""
    if not image_id:
        return None
    repo, tag = split_ref(ref)
    try:
        v = local_version(repo, tag, docker_json("image", "inspect", image_id)[0])[0]
        if v:
            return v
    except Exception:
        pass
    try:
        with open(VERSIONS) as f:
            for r in json.load(f).get("images") or []:
                if r.get("image") == repo and r.get("tag") == tag:
                    return ((r.get("installed") or {}).get(image_id) or {}).get("version")
    except Exception:
        pass
    return None


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


def kind_of(c):
    """Database type of a container (inspect dict). The image is recognised by
    name or by the version variables the official postgres / mariadb / mysql
    images set; it only counts as a database if its command actually starts
    the server - a helper container that merely reuses the image (sh -c …,
    migrations, backups) is not dumped."""
    repo = split_ref(c["Config"]["Image"])[0].lower()
    env = c["Config"].get("Env") or []
    words = command_words(c)
    if PG_RE.search(repo) or any(e.startswith(PG_ENV) for e in env):
        return "postgres" if words & PG_SERVER else None
    if MY_RE.search(repo) or any(e.startswith(MY_ENV) for e in env):
        return "mysql" if words & MY_SERVER else None
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
            "installed_version": None, "tag_digits": None, "jump_unknown": False}

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
    same = [m for m in members if m["Config"]["Image"] == ref]
    services = sorted({(m["Config"]["Labels"] or {}).get(L_SERVICE) for m in same} - {None})
    plan["services"] = services
    plan["containers"] = sorted(m["Name"].lstrip("/") for m in same)

    try:
        cfg = json.loads(run(compose_cmd(ctx) + ["config", "--format", "json"]))
    except subprocess.CalledProcessError as e:
        plan["reason"] = "compose-config-error"
        plan["detail"] = (e.stderr or "").strip()[-400:]
        return plan, ctx
    for s in services:
        svc = (cfg.get("services") or {}).get(s) or {}
        if svc.get("build") is not None:
            plan["reason"] = "built-locally"
            return plan, ctx

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

    plan["free_bytes"] = shutil.disk_usage(BASE if os.path.isdir(BASE) else os.path.dirname(BASE)).free
    plan["updatable"] = True
    return plan, ctx


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
            if status in ("exited", "dead") or health == "unhealthy" or (
                    status == "restarting" and st.get("RestartCount", 0) > 0):
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


def say_reason(reason):
    if reason and reason.get("line"):
        who = reason.get("container") or "docker compose"
        say(f"Vermutliche Ursache ({who}): {reason['line']}", f"Most likely cause ({who}): {reason['line']}")


# ------------------------------------------------------------ operations ----
_log_name = None         # logs/<name> to keep once the operation is recorded


def record(entry, log=None):
    """Append to operations.jsonl. log: the name under logs/ that keeps the
    output of this run (see keep_output)."""
    global _log_name
    ensure_base()
    fd = os.open(OPS, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if log:
        _log_name = log


class Tee:
    """sys.stdout during update and rollback: everything goes through to the
    caller as before, and a copy (up to LOG_MAX characters) is kept."""
    def __init__(self, real):
        self.real, self.parts, self.size = real, [], 0

    def write(self, s):
        self.real.write(s)
        if self.size < LOG_MAX:
            self.parts.append(s)
            self.size += len(s)
        return len(s)

    def flush(self):
        self.real.flush()

    def __getattr__(self, name):
        return getattr(self.real, name)


def save_log(name, text):
    """logs/<name>, root only, written atomically, without the @@RESULT line."""
    ensure_base()
    path = os.path.join(LOGS, name)
    lines = [ln for ln in text.splitlines(True) if not ln.startswith("@@RESULT ")]
    fd = os.open(path + ".part", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.writelines(lines)
    os.replace(path + ".part", path)


def keep_output(fn, *args, **kw):
    """Run update or rollback and keep its output with the history:
    logs/<op-id>.log and logs/<op-id>.rollback.log. An update that stops
    before any change records nothing, so no output is kept either."""
    global _log_name
    tee = Tee(sys.stdout)
    sys.stdout, _log_name = tee, None
    try:
        return fn(*args, **kw)
    finally:
        sys.stdout = tee.real
        if _log_name:
            text = "".join(tee.parts)
            if tee.size >= LOG_MAX:
                text += "\n[output cut at %d KiB]\n" % (LOG_MAX // 1024)
            try:
                save_log(_log_name, text)
            except OSError:
                pass
            _log_name = None


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
    """Fold operations.jsonl into {op_id: op}, later lines updating earlier ones."""
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
    return ops


def result(**kw):
    print("@@RESULT " + json.dumps(kw, ensure_ascii=False), flush=True)


# ---------------------------------------------------------------- update ----
def cmd_update(name, do_dump=True):
    lock()
    plan, ctx = make_plan(name)
    if not plan["updatable"]:
        raise Abort(2, f"Nicht aktualisierbar: {plan['reason']}", f"Not updatable: {plan['reason']}")

    st = stamp()
    op_id = f"{st}-{name}"
    ref = plan["image"]
    repo, _tag = split_ref(ref)
    services, project = plan["services"], plan["project"]
    old_ids = {n: inspect(n)["Image"] for n in plan["containers"]}
    old_id = old_ids[name]
    op = {"id": op_id, "ts": now_iso(), "kind": "update", "container": name,
          "project": project, "workdir": ctx["workdir"], "files": ctx["files"],
          "env_files": ctx["env_files"], "services": services,
          "containers": plan["containers"], "image": ref, "old_id": old_id,
          "from_version": version_of(ref, old_id), "to_version": None,
          "rollback": None, "dumps": [], "status": "running"}

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
            for rel in op["dumps"]:
                try:
                    remove_dump(rel)
                except (Abort, OSError):
                    pass
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

    # 2. Rollback tag for the image the container runs on now
    rb = f"{repo}:rollback-{st}"
    run(["docker", "tag", old_id, rb])
    op["rollback"] = rb
    say(f"Rollback-Sicherung: {rb}", f"Rollback image: {rb}")

    # 3. Pull (registry failures are reported, an already pulled image still counts)
    base = compose_cmd(ctx)
    if stream(base + ["pull", "--ignore-pull-failures", *services]) != 0:
        say("Warnung: Pull meldete Fehler – es wird mit dem lokal vorhandenen Image weitergemacht.",
            "Warning: pull reported errors – continuing with the image available locally.")
    new_id = image_id(ref)
    if new_id and all(v == new_id for v in old_ids.values()):
        run(["docker", "rmi", rb], check=False)
        op.update(status="nochange", rollback=None, new_id=new_id, to_version=op["from_version"])
        record(op, log=op_id + ".log")
        say("Kein neueres Image verfügbar – nichts geändert.", "No newer image available – nothing changed.")
        result(op=op_id, status="nochange", dumps=op["dumps"],
               from_version=op["from_version"], to_version=op["to_version"])
        return 0
    op["new_id"] = new_id
    op["new_digests"] = repo_digests(new_id)
    op["to_version"] = version_of(ref, new_id)
    versions = {"from_version": op["from_version"], "to_version": op["to_version"]}

    # 4. Recreate only these services
    rc = stream(base + ["up", "-d", "--no-deps", "--pull", "never", *services])
    ok, failed, _states = (False, [], {}) if rc != 0 else wait_healthy(project, services)
    if rc != 0 or not ok:
        show_logs(failed or plan["containers"])
        op["reason"] = failure_reason(failed or plan["containers"], compose_rc=rc)
        op["status"] = "failed"
        record(op, log=op_id + ".log")
        say_reason(op["reason"])
        say("Update fehlgeschlagen. Zurückrollen ist möglich.", "Update failed. A rollback is possible.")
        result(op=op_id, status="failed", rollback=rb, dumps=op["dumps"], reason=op["reason"], **versions)
        return 4
    op["status"] = "ok"
    record(op, log=op_id + ".log")
    say("Update erfolgreich.", "Update successful.")

    # 5. + 6. Never let these turn a successful update into a failure.
    cleanup_after_update(project, repo)
    start_radar()
    result(op=op_id, status="ok", rollback=rb, dumps=op["dumps"], **versions)
    return 0


def cmd_rollback(op_id):
    if not OP_RE.match(op_id):
        raise Abort(64, f"Ungültige Vorgangs-ID: {op_id}", f"Invalid operation id: {op_id}")
    lock()
    op = load_ops().get(op_id)
    if not op or op.get("kind") != "update":
        raise Abort(2, f"Vorgang {op_id} nicht gefunden.", f"Operation {op_id} not found.")
    if op.get("rolled_back"):
        raise Abort(3, "Dieser Vorgang wurde bereits zurückgerollt.", "This operation was already rolled back.")
    rb = op.get("rollback")
    if not rb or not image_id(rb):
        raise Abort(2, "Das Rollback-Image existiert nicht mehr.", "The rollback image no longer exists.")
    ctx = {"project": op["project"], "workdir": op["workdir"], "files": op["files"],
           "env_files": op.get("env_files") or []}
    say(f"Rollback von {op['image']} auf {rb} (Projekt {op['project']}) …",
        f"Rolling {op['image']} back to {rb} (project {op['project']}) …")
    say("Achtung: Datenbanken werden NICHT zurückgesetzt. Bei Migrationsproblemen den Dump einspielen.",
        "Note: databases are NOT restored. If migrations cause trouble, restore the dump.")
    run(["docker", "tag", rb, op["image"]])
    rc = stream(compose_cmd(ctx) + ["up", "-d", "--no-deps", "--pull", "never", *op["services"]])
    ok, failed, _ = (False, [], {}) if rc != 0 else wait_healthy(op["project"], op["services"])
    status = "ok" if ok else "failed"
    entry = {"id": op_id, "rolled_back": True, "rollback_ts": now_iso(), "rollback_status": status}
    if not ok:
        show_logs(failed or op["containers"])
        entry["rollback_reason"] = failure_reason(failed or op["containers"], compose_rc=rc)
    record(entry, log=op_id + ".rollback.log")
    if not ok:
        say_reason(entry["rollback_reason"])
        say("Rollback fehlgeschlagen.", "Rollback failed.")
        result(op=op_id, status="failed", reason=entry["rollback_reason"])
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


def rollback_images():
    """Local <repo>:rollback-<stamp> tags, newest first. in_use = a container
    (running or stopped) still uses the image."""
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
                           "size": i.get("Size", ""), "created": i.get("CreatedAt", ""),
                           "in_use": i["ID"] in used_ids})
    images.sort(key=lambda x: x["ref"].rsplit(":", 1)[1], reverse=True)
    return images


def cmd_backups():
    ensure_base()
    dumps = list_dumps()
    images = rollback_images()
    rb_refs = {i["ref"] for i in images}
    all_ops = load_ops()

    ops = sorted((dict(o) for o in all_ops.values()), key=lambda o: o.get("id", ""), reverse=True)[:30]
    for o in ops:
        o["can_rollback"] = (o.get("kind") == "update" and o.get("status") in ("ok", "failed")
                             and not o.get("rolled_back") and norm_ref(o.get("rollback") or "") in rb_refs)
        for k in ("workdir", "files", "env_files", "old_id", "new_id"):
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
            if o.get("kind") == "update" and p and o.get("id", "") > last.get(p, {}).get("id", ""):
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

#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Update / rollback / backup helper for the Cockpit Image-Updates plugin.

Called by the plugin via cockpit.spawn with a FIXED set of commands only:

  plan <container>              read-only: what an update would touch (JSON)
  update <container> [--no-dump]
                                1. dump every supported database container of
                                   the compose project (pg_dumpall / mariadb-dump)
                                2. tag the running image as <repo>:rollback-<stamp>
                                3. docker compose pull + up -d --no-deps for every
                                   service of the project that uses the same image
                                4. wait until the new containers run (healthy)
  rollback <op-id>              point the tag back at the rollback image and
                                recreate the same services (data is NOT restored)
  backups                       read-only: dumps, rollback images, history (JSON)
  delete-dump <project/file>    delete one dump file
  delete-image <repo:rollback-…> remove one rollback image tag

Same tag only ("stage 1"): the image reference in the compose file is never
changed. Images built locally or pinned by digest are not updated.

Everything the addon writes lives in /var/backups/image-updates (root only):
  dumps/<project>/<stamp>_<container>.sql.gz
  operations.jsonl              one JSON line per update / rollback

The machine-readable result of update/rollback is the last line:
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
from datetime import datetime, timezone

BASE = "/var/backups/image-updates"
DUMPS = os.path.join(BASE, "dumps")
OPS = os.path.join(BASE, "operations.jsonl")
LOCK = "/run/lock/diun-update.lock"

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


def stream(cmd):
    """Run a command, pass its output straight through to our stdout."""
    print("+ " + " ".join(cmd), flush=True)
    p = subprocess.run(cmd, stdout=sys.stdout, stderr=subprocess.STDOUT)
    return p.returncode


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


def lock():
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    fh = open(LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise Abort(75, "Es läuft bereits ein Update oder Rollback.",
                    "An update or rollback is already running.")
    return fh


def ensure_base():
    for d in (BASE, DUMPS):
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)


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
            "pulled": False, "old_image": c["Image"]}

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
        subprocess.run(["docker", "logs", "--tail", "30", n], stdout=sys.stdout, stderr=subprocess.STDOUT)
        sys.stdout.flush()


# ------------------------------------------------------------ operations ----
def record(entry):
    ensure_base()
    fd = os.open(OPS, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


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
    _lk = lock()  # noqa: F841 - keep the lock file open until we exit
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
        for d in dumpable:
            op["dumps"].append(dump_db(project, d, st))
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
        op.update(status="nochange", rollback=None, new_id=new_id)
        record(op)
        say("Kein neueres Image verfügbar – nichts geändert.", "No newer image available – nothing changed.")
        result(op=op_id, status="nochange", dumps=op["dumps"])
        return 0
    op["new_id"] = new_id

    # 4. Recreate only these services
    rc = stream(base + ["up", "-d", "--no-deps", "--pull", "never", *services])
    ok, failed, _states = (False, [], {}) if rc != 0 else wait_healthy(project, services)
    if rc != 0 or not ok:
        show_logs(failed or plan["containers"])
        op["status"] = "failed"
        record(op)
        say("Update fehlgeschlagen. Zurückrollen ist möglich.", "Update failed. A rollback is possible.")
        result(op=op_id, status="failed", rollback=rb, dumps=op["dumps"])
        return 4
    op["status"] = "ok"
    record(op)
    say("Update erfolgreich.", "Update successful.")
    result(op=op_id, status="ok", rollback=rb, dumps=op["dumps"])
    return 0


def cmd_rollback(op_id):
    if not OP_RE.match(op_id):
        raise Abort(64, f"Ungültige Vorgangs-ID: {op_id}", f"Invalid operation id: {op_id}")
    _lk = lock()  # noqa: F841 - keep the lock file open until we exit
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
    record({"id": op_id, "rolled_back": True, "rollback_ts": now_iso(), "rollback_status": status})
    if not ok:
        show_logs(failed or op["containers"])
        say("Rollback fehlgeschlagen.", "Rollback failed.")
        result(op=op_id, status="failed")
        return 4
    say("Rollback erfolgreich.", "Rollback successful.")
    result(op=op_id, status="ok")
    return 0


# --------------------------------------------------------------- backups ----
def cmd_backups():
    ensure_base()
    dumps = []
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
    rb_refs = {i["ref"] for i in images}

    ops = sorted(load_ops().values(), key=lambda o: o.get("id", ""), reverse=True)[:30]
    for o in ops:
        o["can_rollback"] = (o.get("kind") == "update" and o.get("status") in ("ok", "failed")
                             and not o.get("rolled_back") and norm_ref(o.get("rollback") or "") in rb_refs)
        for k in ("workdir", "files", "env_files", "old_id", "new_id"):
            o.pop(k, None)

    du = shutil.disk_usage(BASE)
    print(json.dumps({"dumps": dumps, "images": images, "ops": ops,
                      "dump_bytes": sum(d["size"] for d in dumps),
                      "free_bytes": du.free, "dir": BASE}, ensure_ascii=False))
    return 0


def cmd_delete_dump(rel):
    if not DUMP_RE.match(rel):
        raise Abort(64, f"Ungültiger Dump-Name: {rel}", f"Invalid dump name: {rel}")
    path = os.path.realpath(os.path.join(DUMPS, rel))
    if not path.startswith(os.path.realpath(DUMPS) + os.sep) or not os.path.isfile(path):
        raise Abort(2, f"Dump {rel} nicht gefunden.", f"Dump {rel} not found.")
    os.unlink(path)
    pdir = os.path.dirname(path)
    if not os.listdir(pdir):
        os.rmdir(pdir)
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


# ------------------------------------------------------------------ main ----
def main(argv):
    if os.geteuid() != 0:
        raise Abort(77, "Dafür werden Administratorrechte benötigt.", "This requires administrator privileges.")
    if len(argv) == 2 and argv[0] == "plan":
        plan, _ = make_plan(argv[1])
        print(json.dumps(plan, ensure_ascii=False))
        return 0
    if len(argv) in (2, 3) and argv[0] == "update" and (len(argv) == 2 or argv[2] == "--no-dump"):
        return cmd_update(argv[1], do_dump=len(argv) == 2)
    if len(argv) == 2 and argv[0] == "rollback":
        return cmd_rollback(argv[1])
    if argv == ["backups"]:
        return cmd_backups()
    if len(argv) == 2 and argv[0] == "delete-dump":
        return cmd_delete_dump(argv[1])
    if len(argv) == 2 and argv[0] == "delete-image":
        return cmd_delete_image(argv[1])
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

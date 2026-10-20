# SPDX-License-Identifier: Apache-2.0
"""Status of Image Update Monitor in the Azentrius status contract (schema 1).

Every Azentrius add-on answers `status --lang en|de` with the same JSON: the
add-on's state, one sentence and a few items. The Status Board on the bastion
reads it for every host, also where its session has no administrative access.
The items here:

  updates       Diun reports a new build of the configured tag          info
  failed        the newest update of an image failed, was aborted or
                its rollback failed                                     danger
  radar         the version radar's last run, outdated after 24 hours   warning
  rollout       a newer image is pulled, the container runs the old one info
  new-versions  the radar knows newer version tags                      info

updates, rollout and new-versions follow the rules of the page's status
column (rowFor and versionCell in plugin/overview.js): the same rows, counted.
Each item links the place on the page that shows it (LINKS): a click on the
Status Board's tile opens that tab, at its first row of the kind.

What takes time (Docker) happens before: the radar run (diun-versions.py
--output) and, as root, every update, tag change, rollback and cleanup
(diun-update.py) write /var/lib/diun-radar/status.json (0644, with the names
of containers only: no paths, addresses or user names). `status` reads that
file, also without root, renders its texts in the language asked for from
the file's "facts", judges the radar's age from versions.json at that moment
and, as root, reads the history again. Without the file the state is "none"
and the summary says why.

Lives in /usr/lib/cockpit-image-updates (root-owned). No side effects on import.
"""

import importlib.util
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.realpath(__file__))
STATUS = "/var/lib/diun-radar/status.json"
VERSIONS = "/var/lib/diun-radar/versions.json"
EVENTS = "/var/lib/diun/events.jsonl"
DISMISSED = "/var/lib/diun/dismissed.json"
OPS = "/var/backups/image-updates/operations.jsonl"
PLUGIN_JS = "/usr/share/cockpit/image-updates/diun.js"

SCHEMA = 1
ADDON = "image-updates"
LABEL = "Image Update Monitor"
STALE_AFTER = 24 * 3600        # the page calls the radar outdated after 24 h (RADAR_STALE_H)
NAMES_MAX = 5                  # names in a detail, then "+N"
EVENTS_MAX = 16 << 20          # bytes of events.jsonl read, the newest
DISMISSED_MAX = 1 << 20
LEVELS = ("patch", "minor", "major")
SEVERITY = {"danger": 0, "warning": 1, "info": 2}
# The places on the page (PLACES in plugin/diun.js): tab, then the part of it.
LINKS = {"updates": "#/updates/update", "failed": "#/backups/history", "radar": "#/updates",
         "rollout": "#/updates/rollout", "new-versions": "#/updates/new-versions"}
VERSION_RE = re.compile(r'^\s*const VERSION = "([^"]{1,40})";', re.M)
# The timestamps of Docker, Diun and the radar (builtAt in plugin/overview.js).
TIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?", re.A)
EPOCH_1980 = 315532800         # earlier build dates prove nothing (reproducible builds: 1970)
HUB_PREFIX_RE = re.compile(r"^(?:docker\.io/)?(?:library/)?")   # repoKey's two replace() calls


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def seconds_of(s):
    """Seconds since the epoch of "2026-10-15T19:00:50.089Z" (Docker, the
    radar) or "2026-10-15 19:00:50.089 +0000 UTC" (Diun); None if unreadable."""
    m = TIME_RE.match(s) if isinstance(s, str) else None
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1) + "T" + m.group(2), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    zone, offset = m.group(3), 0
    if zone and zone != "Z":
        offset = (-1 if zone[0] == "-" else 1) * (int(zone[1:3]) * 3600 + int(zone[-2:]) * 60)
    return dt.replace(tzinfo=timezone.utc).timestamp() - offset


# ----------------------------------------------------------------- input ----
def read_data(path, limit, tail=False):
    """A file below /var/lib/diun as text, or None. That folder is writable
    from the Diun container, so its files are data: only a regular file, no
    link, no device or pipe; at most `limit` bytes (tail: the last ones, from
    the first whole line)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            return None
        if st.st_size > limit:
            if not tail:
                return None
            f.seek(st.st_size - limit)
            data = f.read(limit)
            data = data[data.find(b"\n") + 1:]
        else:
            data = f.read(limit)
    return data.decode("utf-8", "replace")


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def inventory():
    """The running containers as the page gets them (diun-inventory.py),
    without its lock."""
    spec = importlib.util.spec_from_file_location("diun_inventory", os.path.join(HERE, "diun-inventory.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return list(mod.records())


def load_ops(path):
    """operations.jsonl folded into {op_id: op}, later lines completing
    earlier ones; runs taken back ("discarded") left out (as diun-update.py)."""
    ops = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("id"):
                    ops.setdefault(e["id"], {}).update(e)
    except OSError:
        pass
    return {k: o for k, o in ops.items() if o.get("status") != "discarded"}


def helper_alive(pid):
    """True if the process `pid` is diun-update.py (an update still running)."""
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            return b"diun-update" in f.read()
    except (OSError, TypeError, ValueError):
        return False


def addon_version():
    """The version of the page (const VERSION in diun.js; in a checkout, its
    plugin/ next to lib/), None if unreadable."""
    for path in (PLUGIN_JS, os.path.join(os.path.dirname(HERE), "plugin", "diun.js")):
        try:
            with open(path, encoding="utf-8") as f:
                m = VERSION_RE.search(f.read(1 << 16))
        except OSError:
            continue
        if m:
            return m.group(1)
    return None


# ------------------------------------------------- the page's rules, ported ----
def tag_split(ref):
    """tagSplit: "docker.io/library/postgres:16@sha256:…" -> ("docker.io/library/postgres", "16")."""
    s = str(ref or "").strip().split("@")[0]
    i = s.rfind(":")
    if i > -1 and "/" not in s[i + 1:]:
        return s[:i], s[i + 1:]
    return s, "latest"


def repo_key(ref):
    """repoKey: the repo without Docker Hub's prefixes, lower case (a name, not a URL)."""
    return HUB_PREFIX_RE.sub("", tag_split(ref)[0]).lower()


def ver_key(image, tag):
    return repo_key(image) + ":" + (tag or "latest")


def ev_key(ev):
    return "|".join(str(ev.get(k) or "") for k in ("hostname", "image", "digest"))


def events_by_tag(text):
    """(Diun's newest event per image and tag, whether there is any event).
    An event for a reference with a digest belongs to no row (parseEvents)."""
    by_tag, seen = {}, False
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(e, dict) or not isinstance(e.get("image"), str) or not e["image"]:
            continue
        seen = True
        if "@" in e["image"]:
            continue
        k = ver_key(*tag_split(e["image"]))
        prev = by_tag.get(k)
        if prev is None or str(e.get("ts") or "") >= str(prev.get("ts") or ""):
            by_tag[k] = e
    return by_tag, seen


def same_build(ev, ctn):
    """Diun's new digest is the build that runs (sameBuild): a new list of
    platforms under the same tag."""
    a, b = seconds_of(ev.get("created")), seconds_of(ctn.get("created"))
    return a is not None and a == b and a > EPOCH_1980


def refines(found, version):
    return isinstance(found, str) and bool(found) and (not version or found.startswith(version + "."))


def installed_of(ctn, rec):
    """The installed version (installedOf): the inventory's, unless the radar
    refines the tag's number; otherwise the radar's; None if unknown."""
    inst = rec.get("installed") if isinstance(rec, dict) else None
    r = inst.get(ctn.get("imgid")) if isinstance(inst, dict) and ctn.get("imgid") else None
    r = r if isinstance(r, dict) else None
    from_tag = (ctn.get("version_src") or "tag") == "tag"
    v = ctn.get("version")
    if v and not (from_tag and r and refines(r.get("version"), v)):
        return v
    return r.get("version") if r and r.get("version") else None


def has_news(rec, version):
    """versionCell's `news`: a newer patch or minor, or a major of an image
    that is no database; for an image built here, a newer base."""
    if not isinstance(rec, dict):
        return False
    versions = rec.get("versions")
    cmp = versions.get(version) if version and isinstance(versions, dict) else None
    if not isinstance(cmp, dict) or cmp.get("status") == "unknown":
        cmp = rec.get("by_tag") if isinstance(rec.get("by_tag"), dict) else None
    if cmp is None:
        base = rec.get("base")
        if rec.get("status") == "unlisted" and isinstance(base, list) and base:
            return any(isinstance(b, dict) and b.get("newer") and not b.get("pinned") and not b.get("error")
                       for b in base)
        return False
    return any(cmp.get(lvl) and not (lvl == "major" and rec.get("db")) for lvl in LEVELS)


def page_rows(containers, by_tag, dismissed, versions):
    """{"update": names, "pulled": names, "newver": names}: the rows the page
    marks "Update available", "Roll-out pending" and "New version" (rowFor).
    CI job containers are not rows."""
    radar_ok = isinstance(versions, dict) and isinstance(versions.get("images"), list)
    recs = {}
    for r in versions["images"] if radar_ok else []:
        if isinstance(r, dict) and r.get("image"):
            recs[ver_key(r["image"], r.get("tag"))] = r
    rows = {"update": [], "pulled": [], "newver": []}
    for c in containers:
        if not isinstance(c, dict) or c.get("ci_job"):
            continue
        key = ver_key(c.get("image"), c.get("tag"))
        ev = by_tag.get(key)
        if c.get("pulled"):
            kind = "pulled"
        elif (ev and ev.get("digest") and c.get("digest") and ev["digest"] != c["digest"]
              and not dismissed.get(ev_key(ev)) and not same_build(ev, c)):
            kind = "update"
        elif radar_ok and has_news(recs.get(key), installed_of(c, recs.get(key))):
            kind = "newver"
        else:
            continue
        rows[kind].append(str(c.get("name") or "?"))
    return {k: sorted(v) for k, v in rows.items()}


def run_scope(o):
    """What a run of the history ran for, as make_plan grouped it: the
    services of one compose project started from the same files (working
    directory and compose files), and the containers it names.
    Returns (scope, ctx, settles): the units the run stands for until a later
    run settles them, its compose files (None when not recorded), and the
    units it settles for earlier runs. A unit is (project, "service", name) or
    (project, "container", name). Entries without services (older versions)
    stand for the container names they name; without files they match the
    project's runs in any files. Conservative: an entry settles no service it
    does not name, so a failure of unknown scope stays until a later run of
    the same containers or a rollback."""
    project = o.get("project") or o.get("container") or ""
    files = o.get("files")
    ctx = (o.get("workdir") or "", tuple(str(f) for f in files)) if isinstance(files, list) and files else None
    services = sorted({s for s in o.get("services") or [] if isinstance(s, str) and s})
    names = {c for c in o.get("containers") or [] if isinstance(c, str) and c}
    if isinstance(o.get("container"), str) and o["container"]:
        names.add(o["container"])
    svc = [(project, "service", s) for s in services]
    ctr = [(project, "container", n) for n in sorted(names)]
    return (svc or ctr), ctx, svc + ctr


def settled(done, unit, ctx):
    """A later run settled this unit: a container by its name, a service in
    the same compose files (or where the files are not known)."""
    if unit[1] == "container":
        return unit in done
    return any(c is None or ctx is None or c == ctx for c in done.get(unit, ()))


def failed_ops(ops, alive=None):
    """The runs (updates and tag changes) that need a look: failed and not
    rolled back, their rollback failed, or ended without their last line
    (aborted) while no process runs them. A later run settles an earlier one
    only for the services it ran for (run_scope): the success of another
    service of the same project, another tag of the same image or the same
    image in other compose files hides nothing; a later run of the same
    services, also a tag change, settles it. A run partly settled names a
    container still affected and the services still affected.
    [{"name": container, "how": "failed" | "rollback" | "aborted"[, "services": [...]]}]."""
    alive = alive or helper_alive
    done, out = {}, []
    for o in sorted(ops.values(), key=lambda o: str(o.get("id") or ""), reverse=True):
        if o.get("kind") not in ("update", "retag") or not isinstance(o.get("image"), str):
            continue
        scope, ctx, settles = run_scope(o)
        still = [u for u in scope if not settled(done, u, ctx)]
        names = sorted(u[2] for u in settles if u[1] == "container" and not settled(done, u, ctx))
        for u in settles:
            if u[1] == "container":
                done[u] = True
            else:
                done.setdefault(u, []).append(ctx)
        if not still:
            continue
        if o.get("rollback_status") == "failed":
            how = "rollback"
        elif o.get("rolled_back"):
            how = None
        elif o.get("status") == "failed":
            how = "failed"
        elif o.get("status") == "running" and not alive(o.get("pid")):
            how = "aborted"
        else:
            how = None
        if how:
            own = str(o.get("container") or "?")
            entry = {"name": own if own in names or not names else names[0], "how": how}
            if len(still) < len(scope) and still[0][1] == "service":
                entry["services"] = [u[2] for u in still]
            out.append(entry)
    return sorted(out, key=lambda x: x["name"])


# ----------------------------------------------------------------- facts ----
def radar_facts(versions):
    """found, generated and, when its last run reached no registry (versions.json
    "no_registry"), "reached": false. The error stays out: it can name addresses."""
    gen = versions.get("generated") if isinstance(versions, dict) else None
    out = {"found": isinstance(versions, dict), "generated": gen if isinstance(gen, str) else None}
    if isinstance(versions, dict) and isinstance(versions.get("no_registry"), dict):
        out["reached"] = False
    return out


def facts_of(containers, events_text, dismissed, versions, ops, now, alive=None):
    """What status.json says, without words: names and times only."""
    at = iso(now)
    by_tag, any_event = events_by_tag(events_text)
    rows = page_rows(containers, by_tag, dismissed if isinstance(dismissed, dict) else {}, versions)
    radar = radar_facts(versions)
    gen = seconds_of(radar["generated"])
    radar_at = iso(datetime.fromtimestamp(gen, timezone.utc)) if gen is not None else at
    radar_ok = isinstance(versions, dict) and isinstance(versions.get("images"), list)
    return {
        "checked_at": at,
        "updates": {"checked_at": at, "names": rows["update"] if any_event else None},
        "failed": {"checked_at": at, "ops": failed_ops(ops, alive)},
        "radar": radar,
        "rollout": {"checked_at": at, "names": rows["pulled"]},
        "new-versions": {"checked_at": radar_at, "names": rows["newver"] if radar_ok else None},
    }


# ------------------------------------------------------------------ words ----
def tr(lang, de, en):
    return de if lang == "de" else en


def count(lang, n, de_one, de_other, en_one, en_other):
    return (tr(lang, de_one, en_one) if n == 1 else tr(lang, de_other, en_other)).format(n=n)


def names_text(names):
    text = ", ".join(names[:NAMES_MAX])
    return text + (f" +{len(names) - NAMES_MAX}" if len(names) > NAMES_MAX else "")


def age_text(lang, seconds):
    """How old, as the page's "ago": below an hour, hours below 48, then days."""
    minutes = max(0, round(seconds / 60))
    if minutes < 60:
        return tr(lang, "< 1 Std.", "< 1 h")
    hours = round(minutes / 60)
    if hours < 48:
        return tr(lang, f"{hours} Std.", f"{hours} h")
    return count(lang, round(hours / 24), "{n} Tag", "{n} Tage", "{n} day", "{n} days")


def item(id_, state, title, checked_at, value=None, detail=None):
    it = {"id": id_, "state": state, "title": title}
    if value is not None:
        it["value"] = value
    if detail:
        it["detail"] = detail
    it["link"] = LINKS[id_]
    it["checked_at"] = checked_at
    it["stale_after"] = STALE_AFTER
    return it


def part(facts, id_):
    return facts.get(id_) if isinstance(facts.get(id_), dict) else {}


def names_item(facts, id_, title, none_detail=None):
    """An item that counts containers: info with their names, ok without
    any, none when the source is missing (names null)."""
    f = part(facts, id_)
    at = f.get("checked_at") or facts.get("checked_at")
    if not isinstance(f.get("names"), list):
        return item(id_, "none", title, at, detail=none_detail)
    names = [str(n) for n in f["names"]]
    return item(id_, "info" if names else "ok", title, at, str(len(names)), names_text(names))


def failed_item(facts, lang):
    f = part(facts, "failed")
    ops = [o for o in f.get("ops") or [] if isinstance(o, dict)]
    note = {"rollback": tr(lang, " (Rollback fehlgeschlagen)", " (rollback failed)"),
            "aborted": tr(lang, " (abgebrochen)", " (aborted)")}

    def still(o):
        svc = [str(s) for s in o.get("services") or [] if s]
        return tr(lang, f" (noch betroffen: {', '.join(svc)})", f" (still affected: {', '.join(svc)})") if svc else ""
    return item("failed", "danger" if ops else "ok", tr(lang, "Fehlgeschlagene Updates", "Failed updates"),
                f.get("checked_at") or facts.get("checked_at"), str(len(ops)),
                names_text([str(o.get("name")) + note.get(o.get("how"), "") + still(o) for o in ops]))


UNREACHED = {"de": "Versions-Radar erreichte keine Registry", "en": "Version radar reached no registry"}


def radar_item(facts, lang, now):
    """ok, warning after 24 hours without a run (judged now) or when the last
    run reached no registry, none without versions.json."""
    r = part(facts, "radar")
    outdated = tr(lang, "Versions-Radar veraltet", "Version radar outdated")
    if not r.get("found"):
        return item("radar", "none", tr(lang, "Versions-Radar: keine Daten", "Version radar: no data"),
                    facts.get("checked_at"),
                    detail=tr(lang, "Richten Sie ihn auf der Seite ein oder warten Sie auf seinen ersten Lauf.",
                              "Set it up on the page or wait for its first run."))
    gen = seconds_of(r.get("generated"))
    if gen is None:
        return item("radar", "warning", outdated, facts.get("checked_at"),
                    detail=tr(lang, "Der Zeitpunkt seines letzten Laufs ist nicht lesbar.",
                              "The time of its last run is unreadable."))
    age = now.timestamp() - gen
    if age > STALE_AFTER:
        return item("radar", "warning", outdated, iso(datetime.fromtimestamp(gen, timezone.utc)),
                    age_text(lang, age),
                    tr(lang, "Seit mehr als 24 Stunden kein Lauf: Prüfen Sie diun-radar.timer.",
                       "No run for more than 24 hours: check diun-radar.timer."))
    if r.get("reached") is False:
        return item("radar", "warning", tr(lang, UNREACHED["de"], UNREACHED["en"]),
                    iso(datetime.fromtimestamp(gen, timezone.utc)), age_text(lang, age),
                    tr(lang, "Beim letzten Lauf antwortete keine Registry, auch nicht beim zweiten Versuch. Meist "
                             "liegt das an DNS oder Netz des Servers; angezeigt ist der Stand des letzten guten Laufs.",
                       "No registry answered the last run, nor its second try. Mostly that is DNS or the "
                       "server's network; shown is the data of the last good run."))
    return item("radar", "ok", tr(lang, "Versions-Radar", "Version radar"),
                iso(datetime.fromtimestamp(gen, timezone.utc)), age_text(lang, age))


def items_of(facts, lang, now):
    return [
        names_item(facts, "updates", tr(lang, "Updates verfügbar", "Updates available"),
                   tr(lang, "Diun hat noch nichts gemeldet.", "Diun has reported nothing yet.")),
        failed_item(facts, lang),
        radar_item(facts, lang, now),
        names_item(facts, "rollout", tr(lang, "Ausrollen ausstehend", "Roll-out pending")),
        names_item(facts, "new-versions", tr(lang, "Neue Versionen", "New versions"),
                   tr(lang, "Dafür braucht es den Versions-Radar.", "This needs the version radar.")),
    ]


def overall(items):
    """danger > warning > info; otherwise ok if an item is ok; otherwise none."""
    found = [it["state"] for it in items if it["state"] in SEVERITY]
    if found:
        return min(found, key=SEVERITY.get)
    return "ok" if any(it["state"] == "ok" for it in items) else "none"


def finding(it, lang):
    """One item with a finding as a part of the summary."""
    n = int(it["value"]) if str(it.get("value", "")).isdigit() else 0
    if it["id"] == "failed":
        return count(lang, n, "{n} fehlgeschlagenes Update", "{n} fehlgeschlagene Updates",
                     "{n} failed update", "{n} failed updates")
    if it["id"] == "radar":
        if it["title"] in UNREACHED.values():
            return tr(lang, UNREACHED["de"], UNREACHED["en"][:1].lower() + UNREACHED["en"][1:])
        return tr(lang, "Versions-Radar veraltet", "version radar outdated")
    if it["id"] == "updates":
        return count(lang, n, "{n} Update verfügbar", "{n} Updates verfügbar",
                     "{n} update available", "{n} updates available")
    if it["id"] == "rollout":
        return count(lang, n, "{n} Container auszurollen", "{n} Container auszurollen",
                     "{n} roll-out pending", "{n} roll-outs pending")
    if it["id"] == "new-versions":
        return count(lang, n, "{n} neue Version", "{n} neue Versionen", "{n} new version", "{n} new versions")
    return it["title"]


def summary_of(items, state, lang):
    """The findings, the gravest first: "1 failed update, 3 updates available"."""
    found = sorted((it for it in items if it["state"] in SEVERITY), key=lambda it: SEVERITY[it["state"]])
    text = ", ".join(finding(it, lang) for it in found)
    if not text:
        return (tr(lang, "Keine Updates oder Probleme gefunden", "No updates or problems found") if state == "ok"
                else tr(lang, "Keine Daten", "No data"))
    return text[:1].upper() + text[1:]


def document(facts, lang, now, version):
    items = items_of(facts, lang, now)
    state = overall(items)
    return {"schema": SCHEMA, "addon": ADDON, "label": LABEL, "version": version,
            "checked_at": facts.get("checked_at") or iso(now), "state": state,
            "summary": summary_of(items, state, lang), "items": items}


def without_status(lang, now, version, de, en):
    """No usable status.json: state none, the reason as the summary."""
    return {"schema": SCHEMA, "addon": ADDON, "label": LABEL, "version": version, "checked_at": iso(now),
            "state": "none", "summary": tr(lang, de, en), "items": []}


# --------------------------------------------------------- write and read ----
def write_atomic(path, doc):
    d = os.path.dirname(path) or "."
    os.makedirs(d, mode=0o755, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".status-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
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
    return doc


def refresh(path=None, versions=None, now=None):
    """status.json from the system as it is now (as root: Docker, the Diun
    events, the history). `versions`: the radar's document just written.
    Raises on errors (Docker not answering): the last file stays."""
    now = now or now_utc()
    containers = inventory()
    if versions is None:
        versions = read_json(VERSIONS)
    try:
        dismissed = json.loads(read_data(DISMISSED, DISMISSED_MAX) or "null")
    except (ValueError, RecursionError):
        dismissed = None
    facts = facts_of(containers, read_data(EVENTS, EVENTS_MAX, tail=True), dismissed, versions,
                     load_ops(OPS), now)
    doc = document(facts, "en", now, addon_version())
    doc["facts"] = facts
    return write_atomic(path or STATUS, doc)


def show(lang, now=None, root=None):
    """The answer to `status --lang <lang>`: status.json in that language,
    the radar's age as of now and, as root, the history read again."""
    now = now or now_utc()
    root = os.geteuid() == 0 if root is None else root
    version = addon_version()
    try:
        with open(STATUS, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        if os.path.exists(VERSIONS):
            return without_status(lang, now, version,
                                  "Noch kein Status: Er kommt mit dem nächsten Lauf des Versions-Radars.",
                                  "No status yet: it comes with the next run of the version radar.")
        return without_status(lang, now, version,
                              "Noch kein Status: Der Versions-Radar ist nicht eingerichtet oder noch nicht gelaufen.",
                              "No status yet: the version radar is not set up or has not run yet.")
    except (OSError, ValueError):
        doc = None
    facts = doc.get("facts") if isinstance(doc, dict) else None
    if not isinstance(facts, dict):
        return without_status(lang, now, version, "Die Statusdatei ist nicht lesbar.",
                              "The status file is unreadable.")
    facts = dict(facts)
    facts["radar"] = radar_facts(read_json(VERSIONS))
    if root:
        facts["failed"] = {"checked_at": iso(now), "ops": failed_ops(load_ops(OPS))}
    return document(facts, lang, now, version or doc.get("version"))

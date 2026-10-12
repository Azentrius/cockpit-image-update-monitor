// SPDX-License-Identifier: Apache-2.0
/* Image-Updates monitor for Cockpit
 * Lists running containers (their installed version + current digest) and
 * annotates each with independent signals:
 *   - inventory -> cockpit.spawn(diun-inventory.py)  configured tag, exact installed
 *                                                    version (tag / image variable),
 *                                                    compose project, "pulled, not rolled out"
 *   - Diun log  -> events.jsonl                      same tag rebuilt (new digest)
 *   - Radar     -> versions.json                     installed version via label /
 *                                                    digest, newer version tags
 *                                                    (patch / minor / major)
 *   - deps      -> cockpit.spawn(diun-deps.sh check) Docker, Diun, skopeo, radar
 *   - updates   -> cockpit.spawn(diun-update.py …)   plan / update / rollback /
 *                                                    backups / delete
 *
 * The radar file is written every 6h by diun-radar.timer
 * (/usr/local/lib/diun-radar/diun-versions.py). Everything Cockpit executes
 * as root lives in /usr/local/lib/diun-radar — never in /var/lib/diun, which
 * is writable from inside the Diun container (2026-10-12).
 *
 * Updates ("stage 1"): same tag only — pull + recreate the compose services
 * that use the image, after a database dump and with a rollback copy of the
 * old image. Dumps and rollback copies are listed on the "Backups" tab.
 *
 * Language: Cockpit's session language (cockpit.language, set by
 * ../base1/po.js). German -> DE, anything else -> EN.
 *
 * No build step, no framework, no external requests — same model as cronctl.
 */

(function () {
    "use strict";

    // --- Configuration -----------------------------------------------------
    // The one place for the version (SemVer). index.html loads this file and
    // diun.css with ?v=VERSION; tools/check-version.sh keeps them in step.
    const VERSION = "0.7.0";
    const LIB = "/usr/local/lib/diun-radar/";
    const EVENTS_PATH = "/var/lib/diun/events.jsonl";
    const DISMISSED_PATH = "/var/lib/diun/dismissed.json";
    const VERSIONS_PATH = "/var/lib/diun-radar/versions.json";
    const INVENTORY_CMD = LIB + "diun-inventory.py";
    const DEPS_CMD = LIB + "diun-deps.sh";
    const UPDATE_CMD = LIB + "diun-update.py";
    const FILE_OPTS = { superuser: "try" };
    const RADAR_STALE_H = 24;   // radar older than this -> flagged in the header

    // --- i18n --------------------------------------------------------------
    // Plural entries: { one, other }, picked by vars.n.
    const I18N = {
        en: {
            "app.title": "Image updates",
            "tab.updates": "Updates",
            "tab.backups": "Backups & dumps",
            "btn.refresh": "Refresh",
            "btn.loading": "Loading …",
            "btn.dismiss": "Dismiss",
            "btn.update": "Update",
            "btn.rollout": "Roll out",
            "btn.delete": "Delete",
            "btn.radar": "Run radar now",
            "btn.radarRunning": "Radar running …",
            "radar.tip": "Looks for newer versions in the registries right away instead of waiting for the next 6-hour run.",
            "radar.failed": "The radar run failed – details: journalctl -u diun-radar.service",
            "radar.notSetUp": "The version radar is not set up yet (see “Missing requirements”).",
            "loading.title": "Loading overview …",
            "loading.sub": "Inventorying containers and matching them against Diun and the version radar.",
            "col.container": "Container",
            "col.image": "Image",
            "col.configured": "Configured",
            "col.installed": "Installed",
            "col.new": "Newest version",
            "col.status": "Status",
            "col.when": "When",
            "col.result": "Result",
            "col.dumps": "Dumps",
            "col.project": "Project",
            "col.created": "Created",
            "col.file": "File",
            "col.size": "Size",
            "col.version": "Version",
            "empty": "No running containers found.",
            "badge.update": "Update available",
            "badge.pulled": "Roll-out pending",
            "badge.pulledTip": "A newer image is already on the server, but the container still runs the old one.",
            "badge.newver": "New version",
            "badge.ok": "up to date",
            "badge.unknown": "unknown",
            "row.manualOnly": "manual only",
            "row.manualOnlyTip": "Not started with docker compose – the add-on cannot recreate it safely.",
            "cfg.tip": "Tag as set in the compose file (or with docker run).",
            "inst.tag": "The tag itself names this version.",
            "inst.env": "Read from the image (variable {name}).",
            "inst.label": "From the image label org.opencontainers.image.version, confirmed by a matching tag in the registry.",
            "inst.digest": "Digest comparison: the registry tag {tag} points to exactly this image.",
            "inst.unknown": "Cannot be determined: the image names no version and no version tag in the registry points to it.",
            "inst.pending": "Not determined yet – the version radar runs every 6 hours.",
            "inst.digestLine": "Digest: {d}",
            "inst.pulledTip": "{v} is already on the server but not rolled out yet.",
            "ver.noRadar": "The version radar has no data yet",
            "ver.notCovered": "Not covered by the radar yet (runs every 6 h)",
            "ver.registryError": "Registry error",
            "ver.upToDateTip": "Installed = newest version (same tag scheme).",
            "ver.newestOnly": "newest: {v}",
            "ver.newestOnlyTip": "Installed version unknown – no comparison possible.",
            "ver.stale": "(older data)",
            "ver.levelTip": "{level} update",
            "ver.reachYes": "The tag {tag} delivers this with the Update button.",
            "ver.reachNo": "Needs a different tag in the compose file (currently {tag}).",
            "ver.dbMajor": "Database major: requires a migration (dump/restore or pg_upgrade), not just an image swap",
            "legend.filled": "reachable with the Update button",
            "legend.outlined": "needs a different tag in the compose file",
            "legend.dashed": "database major – needs a migration",
            "meta.updates": { one: "{n} update", other: "{n} updates" },
            "meta.newver": { one: "{n} new version", other: "{n} new versions" },
            "meta.containers": { one: "{n} container", other: "{n} containers" },
            "meta.radar": "Radar {ago}",
            "meta.radarNone": "Radar: no data yet",
            "meta.radarBad": "Radar: unreadable timestamp",
            "meta.outdated": "(outdated)",
            "ago.now": "just now",
            "ago.min": "{n} min ago",
            "ago.h": "{n} h ago",
            "ago.d": { one: "{n} day ago", other: "{n} days ago" },
            "status.noInventory": "Container inventory unavailable (Docker unreachable or no administrative access) — showing reported Diun updates only.",
            "status.loadError": "Loading failed: {msg}",
            "status.saveError": "Could not save status: {msg}",
            "deps.title": "Missing requirements",
            "deps.intro": "Some components are missing or not running. The overview works with limitations until they are in place.",
            "deps.needAdmin": "Checking requirements needs administrative access in Cockpit.",
            "dep.docker": "Docker",
            "dep.docker.desc": "Without Docker the add-on cannot list containers.",
            "dep.docker.hint": "Please install or start Docker manually.",
            "dep.diun": "Diun",
            "dep.diun.desc": "Reports new builds of the same tag (digest updates).",
            "dep.diun.noEvents": "Diun is running but sends no events to the add-on (script notifier missing in diun.yml).",
            "dep.diun.noCompose": "Docker Compose is required to install Diun.",
            "dep.diun.external": "Diun is managed outside /opt/diun – please start it there.",
            "dep.skopeo": "skopeo",
            "dep.skopeo.desc": "Reads tag lists from registries for the version radar.",
            "dep.skopeo.noPm": "No supported package manager (apt-get or dnf) found.",
            "dep.radar": "Version radar",
            "dep.radar.desc": "Checks every 6 hours whether newer versions have been released.",
            "dep.radar.noScript": "Radar script or python3 missing on this server.",
            "state.missing": "missing",
            "state.stopped": "stopped",
            "state.inactive": "inactive",
            "state.unreachable": "unreachable",
            "state.noEvents": "no events",
            "action.install": "Install",
            "action.start": "Start",
            "action.setup": "Set up",
            "dlg.install.skopeo.title": "Install skopeo?",
            "dlg.install.skopeo.desc": "Installs the “skopeo” package from the system's package repositories (package lists are refreshed first).",
            "dlg.install.diun.title": "Install Diun?",
            "dlg.install.diun.desc": "Creates /opt/diun (Diun 4.33.0 plus a read-only Docker socket proxy), writes the event notifier to /var/lib/diun and starts the stack with docker compose. Existing files are never overwritten.",
            "dlg.start.diun.title": "Start Diun?",
            "dlg.start.diun.desc": "Starts the existing stack in /opt/diun (docker compose up -d).",
            "dlg.install.radar.title": "Set up the version radar?",
            "dlg.install.radar.desc": "Sets up diun-radar.service and diun-radar.timer (runs every 6 hours) and starts a first run right away.",
            "dlg.install.cleanup.title": "Set up the daily cleanup?",
            "dlg.install.cleanup.desc": "Sets up diun-cleanup.service and diun-cleanup.timer (daily around 04:40). Each run deletes what the rules shown in this tab select; the first run happens at the next scheduled time.",
            "upd.title": "Update {name}?",
            "upd.titlePulled": "Roll out {name}?",
            "upd.desc": "Pulls the current image for {image} and recreates the affected containers. The image running now is kept as a rollback copy first.",
            "upd.descPulled": "The newer image for {image} is already on the server. The affected containers are recreated with it; the image running now is kept as a rollback copy first.",
            "upd.planning": "Checking what the update affects …",
            "upd.containers": "Affected containers",
            "upd.project": "Compose project",
            "upd.rollback": "Rollback copy",
            "upd.dbs": "Database dump",
            "upd.noDb": "No database container in this project",
            "upd.dbLine": "{name} (approx. {size} uncompressed)",
            "upd.dbLineNoSize": "{name}",
            "upd.dbUnsupported": "{name}: no dump (type not supported)",
            "upd.dbStopped": "{name} is not running – the update would abort",
            "upd.free": "Free space for dumps: {free}",
            "upd.dumpCheck": "Create database dump first (recommended)",
            "upd.configChanged": "The compose file changed since these containers were created – those changes will be rolled out too.",
            "upd.dataWarn": "A rollback restores the image, not the data. If the new version migrates its database, only the dump helps.",
            "upd.downtime": "The containers are briefly unavailable while they are recreated.",
            "upd.notPossible": "Not possible from the add-on: {reason}",
            "reason.not-compose": "the container was not started with docker compose, so it cannot be recreated safely.",
            "reason.compose-file-missing": "the compose file no longer exists ({files}).",
            "reason.digest-pinned": "the image is pinned by digest (@sha256) – updating means editing the compose file.",
            "reason.built-locally": "the image is built locally (build:) – rebuild it in the project.",
            "reason.compose-config-error": "docker compose config fails: {detail}",
            "upd.ok": "Update successful. Rollback copy: {rb}",
            "upd.okVersions": "Update successful: {from} → {to}. Rollback copy: {rb}",
            "upd.nochange": "No newer image available – nothing changed.",
            "upd.failed": "Update failed – the containers do not run stably. You can roll back to the previous version.",
            "upd.aborted": "Aborted before anything was changed – see output.",
            "upd.busy": "Another update or rollback is already running.",
            "rb.button": "Roll back",
            "rb.title": "Roll back {name}?",
            "rb.desc": "Points {image} back to {rb} and recreates {services}. Databases are not reset – if migrations cause trouble, restore the dump.",
            "rb.ok": "Rollback successful.",
            "rb.failed": "Rollback failed – see output.",
            "bk.summary": "{dumps} · {images} · {free} free in {dir}",
            "bk.dumpsN": { one: "{n} dump ({size})", other: "{n} dumps ({size})" },
            "bk.imagesN": { one: "{n} rollback image", other: "{n} rollback images" },
            "bk.note": "Restoring a dump is deliberately a manual step. Dump files are readable by root only.",
            "bk.ops": "Update history",
            "bk.dumps": "Database dumps",
            "bk.images": "Rollback images",
            "bk.noOps": "No updates yet.",
            "bk.noDumps": "No dumps.",
            "bk.noImages": "No rollback images.",
            "bk.error": "Backups could not be loaded: {msg}",
            "bk.needAdmin": "The backup overview needs administrative access in Cockpit.",
            "cl.title": "Cleanup",
            "cl.keep": { one: "keeps the newest dump per database container", other: "keeps the newest {n} dumps per database container" },
            "cl.keepOff": "never deletes dumps",
            "cl.days": { one: "removes unused rollback images after {n} day", other: "removes unused rollback images after {n} days" },
            "cl.daysOff": "never removes rollback images",
            "cl.rules": "Rules: {keep}, {days}.",
            "cl.srcFile": "Set in {file}.",
            "cl.srcDefault": "Defaults – change them in /etc/image-updates.conf (KEEP_DUMPS, ROLLBACK_IMAGE_DAYS; 0 = never).",
            "cl.protect": "The dumps of each project's last update and rollback images in use are never deleted.",
            "cl.timerOn": "Runs after every successful update (for its project) and daily via diun-cleanup.timer.",
            "cl.timerOff": "Runs after every successful update (for its project). The daily run is not set up.",
            "cl.dumpsN": { one: "{n} dump", other: "{n} dumps" },
            "cl.imagesN": { one: "{n} rollback image", other: "{n} rollback images" },
            "cl.preview": "To be deleted now: {dumps}, {images} ({size}).",
            "cl.nothing": "Nothing to clean up right now.",
            "cl.error": "Cleanup rules: {msg}",
            "cl.button": "Clean up now",
            "cl.mark": "to be deleted",
            "cl.markTip": "Selected by the cleanup rules (next run or “Clean up now”).",
            "cl.dlgTitle": "Clean up now?",
            "cl.dlgDesc": "Deletes the dumps and rollback images the rules select. They cannot be restored afterwards.",
            "cl.confirm": "Clean up",
            "cl.done": "Cleaned up: {dumps}, {images} ({size} freed).",
            "cl.partial": "{n} could not be removed – see output.",
            "cl.nochange": "Nothing to clean up.",
            "res.ok": "successful",
            "res.nochange": "no change",
            "res.failed": "failed",
            "res.running": "aborted",
            "res.rolledBack": "rolled back",
            "res.rollbackFailed": "rollback failed",
            "img.inUse": "in use",
            "img.inUseTip": "A container still uses this image – it cannot be deleted.",
            "img.free": "unused",
            "del.dumpTitle": "Delete dump?",
            "del.dumpDesc": "Deletes {path} permanently. It cannot be restored afterwards.",
            "del.imgTitle": "Delete rollback image?",
            "del.imgDesc": "Removes {ref}. Rolling back to this state is no longer possible afterwards.",
            "del.confirm": "Delete permanently",
            "del.ok": "Deleted.",
            "dialog.note": "Runs with administrator privileges via Cockpit. Output appears here live.",
            "dialog.cancel": "Cancel",
            "dialog.close": "Close",
            "dialog.running": "Running …",
            "dialog.success": "Completed successfully.",
            "dialog.failed": "Failed (exit code {code}).",
            "dialog.error": "Failed: {msg}",
            "dialog.noAdmin": "Denied: this needs administrative access in Cockpit (switch it on at the top of Cockpit).",
        },
        de: {
            "app.title": "Image-Updates",
            "tab.updates": "Updates",
            "tab.backups": "Backups & Dumps",
            "btn.refresh": "Aktualisieren",
            "btn.loading": "Lädt …",
            "btn.dismiss": "Erledigt",
            "btn.update": "Update",
            "btn.rollout": "Ausrollen",
            "btn.delete": "Löschen",
            "btn.radar": "Radar jetzt starten",
            "btn.radarRunning": "Radar läuft …",
            "radar.tip": "Sucht sofort in den Registries nach neueren Versionen, statt auf den nächsten 6-Stunden-Lauf zu warten.",
            "radar.failed": "Der Radar-Lauf ist fehlgeschlagen – Details: journalctl -u diun-radar.service",
            "radar.notSetUp": "Der Versions-Radar ist noch nicht eingerichtet (siehe „Voraussetzungen unvollständig“).",
            "loading.title": "Übersicht wird geladen …",
            "loading.sub": "Container werden inventarisiert und mit Diun und dem Versions-Radar abgeglichen.",
            "col.container": "Container",
            "col.image": "Image",
            "col.configured": "Konfiguriert",
            "col.installed": "Installiert",
            "col.new": "Neueste Version",
            "col.status": "Status",
            "col.when": "Zeitpunkt",
            "col.result": "Ergebnis",
            "col.dumps": "Dumps",
            "col.project": "Projekt",
            "col.created": "Erstellt",
            "col.file": "Datei",
            "col.size": "Größe",
            "col.version": "Version",
            "empty": "Keine laufenden Container gefunden.",
            "badge.update": "Update verfügbar",
            "badge.pulled": "Ausrollen ausstehend",
            "badge.pulledTip": "Ein neueres Image liegt schon auf dem Server, der Container läuft aber noch auf dem alten.",
            "badge.newver": "Neue Version",
            "badge.ok": "aktuell",
            "badge.unknown": "unbekannt",
            "row.manualOnly": "nur manuell",
            "row.manualOnlyTip": "Nicht mit docker compose gestartet – das Addon kann ihn nicht sicher neu erstellen.",
            "cfg.tip": "Tag, wie er in der Compose-Datei (bzw. bei docker run) steht.",
            "inst.tag": "Der Tag nennt die Version bereits exakt.",
            "inst.env": "Aus dem Image gelesen (Variable {name}).",
            "inst.label": "Aus dem Image-Label org.opencontainers.image.version, bestätigt durch einen passenden Tag in der Registry.",
            "inst.digest": "Digest-Abgleich: Der Registry-Tag {tag} zeigt genau auf dieses Image.",
            "inst.unknown": "Nicht ermittelbar: Das Image nennt keine Version, und kein Versions-Tag in der Registry zeigt darauf.",
            "inst.pending": "Noch nicht ermittelt – der Versions-Radar läuft alle 6 Stunden.",
            "inst.digestLine": "Digest: {d}",
            "inst.pulledTip": "{v} liegt schon auf dem Server, ist aber noch nicht ausgerollt.",
            "ver.noRadar": "Versions-Radar hat noch keine Daten",
            "ver.notCovered": "Noch nicht vom Radar erfasst (nächster Lauf alle 6 h)",
            "ver.registryError": "Registry-Fehler",
            "ver.upToDateTip": "Installiert = neueste Version (gleiches Tag-Schema).",
            "ver.newestOnly": "neueste: {v}",
            "ver.newestOnlyTip": "Installierte Version unbekannt – kein Vergleich möglich.",
            "ver.stale": "(Stand älter)",
            "ver.levelTip": "{level}-Update",
            "ver.reachYes": "Der Tag {tag} liefert das per Update-Knopf.",
            "ver.reachNo": "Erfordert einen anderen Tag in der Compose-Datei (derzeit {tag}).",
            "ver.dbMajor": "Datenbank-Major: braucht eine Migration (Dump/Restore bzw. pg_upgrade), kein reiner Image-Tausch",
            "legend.filled": "per Update-Knopf erreichbar",
            "legend.outlined": "braucht einen anderen Tag in der Compose-Datei",
            "legend.dashed": "Datenbank-Major – braucht eine Migration",
            "meta.updates": { one: "{n} Update", other: "{n} Updates" },
            "meta.newver": { one: "{n} neue Version", other: "{n} neue Versionen" },
            "meta.containers": { one: "{n} Container", other: "{n} Container" },
            "meta.radar": "Radar {ago}",
            "meta.radarNone": "Radar: noch keine Daten",
            "meta.radarBad": "Radar: Zeitstempel unlesbar",
            "meta.outdated": "(veraltet)",
            "ago.now": "gerade eben",
            "ago.min": "vor {n} min",
            "ago.h": "vor {n} h",
            "ago.d": { one: "vor {n} Tag", other: "vor {n} Tagen" },
            "status.noInventory": "Container-Inventar nicht verfügbar (Docker nicht erreichbar oder kein administrativer Zugriff) — zeige nur gemeldete Diun-Updates.",
            "status.loadError": "Fehler beim Laden: {msg}",
            "status.saveError": "Konnte Status nicht speichern: {msg}",
            "deps.title": "Voraussetzungen unvollständig",
            "deps.intro": "Einige Bestandteile fehlen oder laufen nicht. Bis dahin funktioniert die Übersicht nur eingeschränkt.",
            "deps.needAdmin": "Zum Prüfen der Voraussetzungen wird administrativer Zugriff in Cockpit benötigt.",
            "dep.docker": "Docker",
            "dep.docker.desc": "Ohne Docker kann das Addon keine Container auflisten.",
            "dep.docker.hint": "Bitte Docker manuell installieren bzw. starten.",
            "dep.diun": "Diun",
            "dep.diun.desc": "Meldet neue Builds desselben Tags (Digest-Updates).",
            "dep.diun.noEvents": "Diun läuft, liefert aber keine Ereignisse an das Addon (Script-Notifier fehlt in diun.yml).",
            "dep.diun.noCompose": "Für die Installation von Diun wird Docker Compose benötigt.",
            "dep.diun.external": "Diun wird außerhalb von /opt/diun verwaltet – bitte dort starten.",
            "dep.skopeo": "skopeo",
            "dep.skopeo.desc": "Liest Versionslisten aus den Registries für den Versions-Radar.",
            "dep.skopeo.noPm": "Kein unterstützter Paketmanager (apt-get oder dnf) gefunden.",
            "dep.radar": "Versions-Radar",
            "dep.radar.desc": "Prüft alle 6 Stunden, ob neuere Versionen erschienen sind.",
            "dep.radar.noScript": "Radar-Skript oder python3 fehlt auf diesem Server.",
            "state.missing": "fehlt",
            "state.stopped": "gestoppt",
            "state.inactive": "inaktiv",
            "state.unreachable": "nicht erreichbar",
            "state.noEvents": "keine Ereignisse",
            "action.install": "Installieren",
            "action.start": "Starten",
            "action.setup": "Einrichten",
            "dlg.install.skopeo.title": "skopeo installieren?",
            "dlg.install.skopeo.desc": "Installiert das Paket „skopeo“ aus den Paketquellen des Systems (die Paketlisten werden vorher aktualisiert).",
            "dlg.install.diun.title": "Diun installieren?",
            "dlg.install.diun.desc": "Legt /opt/diun an (Diun 4.33.0 plus schreibgeschützter Docker-Socket-Proxy), schreibt den Ereignis-Notifier nach /var/lib/diun und startet den Stack mit docker compose. Vorhandene Dateien werden nie überschrieben.",
            "dlg.start.diun.title": "Diun starten?",
            "dlg.start.diun.desc": "Startet den vorhandenen Stack in /opt/diun (docker compose up -d).",
            "dlg.install.radar.title": "Versions-Radar einrichten?",
            "dlg.install.radar.desc": "Richtet diun-radar.service und diun-radar.timer ein (Lauf alle 6 Stunden) und startet sofort einen ersten Lauf.",
            "dlg.install.cleanup.title": "Tägliches Aufräumen einrichten?",
            "dlg.install.cleanup.desc": "Richtet diun-cleanup.service und diun-cleanup.timer ein (täglich gegen 04:40). Jeder Lauf löscht, was die in diesem Tab gezeigten Regeln auswählen; der erste Lauf folgt zum nächsten geplanten Zeitpunkt.",
            "upd.title": "{name} aktualisieren?",
            "upd.titlePulled": "{name} ausrollen?",
            "upd.desc": "Zieht das aktuelle Image für {image} und erstellt die betroffenen Container neu. Das jetzt laufende Image wird vorher als Rollback-Kopie gesichert.",
            "upd.descPulled": "Das neuere Image für {image} liegt schon auf dem Server. Die betroffenen Container werden damit neu erstellt; das jetzt laufende Image wird vorher als Rollback-Kopie gesichert.",
            "upd.planning": "Prüfe, was das Update betrifft …",
            "upd.containers": "Betroffene Container",
            "upd.project": "Compose-Projekt",
            "upd.rollback": "Rollback-Kopie",
            "upd.dbs": "Datenbank-Dump",
            "upd.noDb": "Kein Datenbank-Container im Projekt",
            "upd.dbLine": "{name} (ca. {size} unkomprimiert)",
            "upd.dbLineNoSize": "{name}",
            "upd.dbUnsupported": "{name}: kein Dump (Typ nicht unterstützt)",
            "upd.dbStopped": "{name} läuft nicht – das Update würde abbrechen",
            "upd.free": "Freier Platz für Dumps: {free}",
            "upd.dumpCheck": "Vorher Datenbank-Dump erstellen (empfohlen)",
            "upd.configChanged": "Die Compose-Datei wurde seit dem Start dieser Container geändert – diese Änderungen werden mit ausgerollt.",
            "upd.dataWarn": "Ein Rollback stellt das Image wieder her, nicht die Daten. Migriert die neue Version ihre Datenbank, hilft nur der Dump.",
            "upd.downtime": "Die Container sind beim Neuerstellen kurz nicht erreichbar.",
            "upd.notPossible": "Über das Addon nicht möglich: {reason}",
            "reason.not-compose": "Der Container wurde nicht mit docker compose gestartet und lässt sich daher nicht sicher neu erstellen.",
            "reason.compose-file-missing": "Die Compose-Datei existiert nicht mehr ({files}).",
            "reason.digest-pinned": "Das Image ist per Digest festgelegt (@sha256) – ein Update heißt hier, die Compose-Datei zu ändern.",
            "reason.built-locally": "Das Image wird lokal gebaut (build:) – bitte im Projekt neu bauen.",
            "reason.compose-config-error": "docker compose config schlägt fehl: {detail}",
            "upd.ok": "Update erfolgreich. Rollback-Kopie: {rb}",
            "upd.okVersions": "Update erfolgreich: {from} → {to}. Rollback-Kopie: {rb}",
            "upd.nochange": "Kein neueres Image verfügbar – nichts geändert.",
            "upd.failed": "Update fehlgeschlagen – die Container laufen nicht stabil. Du kannst auf die vorherige Version zurückrollen.",
            "upd.aborted": "Abgebrochen, bevor etwas geändert wurde – siehe Ausgabe.",
            "upd.busy": "Es läuft bereits ein Update oder Rollback.",
            "rb.button": "Zurückrollen",
            "rb.title": "{name} zurückrollen?",
            "rb.desc": "Setzt {image} wieder auf {rb} und erstellt {services} neu. Datenbanken werden nicht zurückgesetzt – bei Migrationsproblemen den Dump einspielen.",
            "rb.ok": "Rollback erfolgreich.",
            "rb.failed": "Rollback fehlgeschlagen – siehe Ausgabe.",
            "bk.summary": "{dumps} · {images} · {free} frei in {dir}",
            "bk.dumpsN": { one: "{n} Dump ({size})", other: "{n} Dumps ({size})" },
            "bk.imagesN": { one: "{n} Rollback-Image", other: "{n} Rollback-Images" },
            "bk.note": "Das Wiederherstellen eines Dumps ist bewusst ein manueller Schritt. Die Dump-Dateien sind nur für root lesbar.",
            "bk.ops": "Update-Verlauf",
            "bk.dumps": "Datenbank-Dumps",
            "bk.images": "Rollback-Images",
            "bk.noOps": "Noch keine Updates.",
            "bk.noDumps": "Keine Dumps.",
            "bk.noImages": "Keine Rollback-Images.",
            "bk.error": "Backups konnten nicht geladen werden: {msg}",
            "bk.needAdmin": "Die Backup-Übersicht braucht administrativen Zugriff in Cockpit.",
            "cl.title": "Aufräumen",
            "cl.keep": { one: "behält den neuesten Dump je Datenbank-Container", other: "behält die neuesten {n} Dumps je Datenbank-Container" },
            "cl.keepOff": "löscht nie Dumps",
            "cl.days": { one: "entfernt ungenutzte Rollback-Images nach {n} Tag", other: "entfernt ungenutzte Rollback-Images nach {n} Tagen" },
            "cl.daysOff": "entfernt nie Rollback-Images",
            "cl.rules": "Regeln: {keep}, {days}.",
            "cl.srcFile": "Festgelegt in {file}.",
            "cl.srcDefault": "Standardwerte – änderbar in /etc/image-updates.conf (KEEP_DUMPS, ROLLBACK_IMAGE_DAYS; 0 = nie).",
            "cl.protect": "Die Dumps des letzten Updates je Projekt und benutzte Rollback-Images werden nie gelöscht.",
            "cl.timerOn": "Läuft nach jedem erfolgreichen Update (für dessen Projekt) und täglich über diun-cleanup.timer.",
            "cl.timerOff": "Läuft nach jedem erfolgreichen Update (für dessen Projekt). Der tägliche Lauf ist nicht eingerichtet.",
            "cl.dumpsN": { one: "{n} Dump", other: "{n} Dumps" },
            "cl.imagesN": { one: "{n} Rollback-Image", other: "{n} Rollback-Images" },
            "cl.preview": "Jetzt zu löschen: {dumps}, {images} ({size}).",
            "cl.nothing": "Derzeit nichts aufzuräumen.",
            "cl.error": "Aufräumregeln: {msg}",
            "cl.button": "Jetzt aufräumen",
            "cl.mark": "wird gelöscht",
            "cl.markTip": "Von den Aufräumregeln ausgewählt (nächster Lauf oder „Jetzt aufräumen“).",
            "cl.dlgTitle": "Jetzt aufräumen?",
            "cl.dlgDesc": "Löscht die von den Regeln ausgewählten Dumps und Rollback-Images. Sie lassen sich danach nicht wiederherstellen.",
            "cl.confirm": "Aufräumen",
            "cl.done": "Aufgeräumt: {dumps}, {images} ({size} frei geworden).",
            "cl.partial": "{n} ließen sich nicht entfernen – siehe Ausgabe.",
            "cl.nochange": "Nichts aufzuräumen.",
            "res.ok": "erfolgreich",
            "res.nochange": "keine Änderung",
            "res.failed": "fehlgeschlagen",
            "res.running": "abgebrochen",
            "res.rolledBack": "zurückgerollt",
            "res.rollbackFailed": "Rollback fehlgeschlagen",
            "img.inUse": "in Benutzung",
            "img.inUseTip": "Ein Container nutzt dieses Image noch – es kann nicht gelöscht werden.",
            "img.free": "ungenutzt",
            "del.dumpTitle": "Dump löschen?",
            "del.dumpDesc": "Löscht {path} endgültig. Danach lässt er sich nicht mehr wiederherstellen.",
            "del.imgTitle": "Rollback-Image löschen?",
            "del.imgDesc": "Entfernt {ref}. Ein Zurückrollen auf diesen Stand ist danach nicht mehr möglich.",
            "del.confirm": "Endgültig löschen",
            "del.ok": "Gelöscht.",
            "dialog.note": "Läuft mit Administratorrechten über Cockpit. Die Ausgabe erscheint hier live.",
            "dialog.cancel": "Abbrechen",
            "dialog.close": "Schließen",
            "dialog.running": "Läuft …",
            "dialog.success": "Erfolgreich abgeschlossen.",
            "dialog.failed": "Fehlgeschlagen (Exit-Code {code}).",
            "dialog.error": "Fehlgeschlagen: {msg}",
            "dialog.noAdmin": "Abgelehnt: Dafür wird administrativer Zugriff in Cockpit benötigt (oben in Cockpit einschalten).",
        },
    };
    const LANG = /^de([-_]|$)/i.test(String(cockpit.language || "")) ? "de" : "en";
    const LOCALE = LANG === "de" ? "de-DE" : "en-GB";

    function t(key, vars) {
        let s = I18N[LANG][key];
        if (s === undefined) s = I18N.en[key];
        if (s === undefined) return key;
        if (typeof s === "object") s = (vars && vars.n === 1) ? s.one : s.other;
        return s.replace(/\{(\w+)\}/g, (m, k) => (vars && k in vars ? String(vars[k]) : m));
    }

    function applyStaticTexts() {
        document.documentElement.lang = LANG;
        document.querySelectorAll("[data-i18n]").forEach((el) => {
            el.textContent = t(el.dataset.i18n);
        });
    }

    const $ = (id) => document.getElementById(id);

    let dismissed = {};
    const eventsFile = cockpit.file(EVENTS_PATH, FILE_OPTS);
    const dismissedFile = cockpit.file(DISMISSED_PATH, { ...FILE_OPTS, syntax: JSON });
    const versionsFile = cockpit.file(VERSIONS_PATH, { ...FILE_OPTS, syntax: JSON });

    // --- Small helpers -----------------------------------------------------
    function tagSplit(ref) {
        let s = (ref || "").trim().split("@")[0];
        const i = s.lastIndexOf(":");
        if (i > -1) {
            const after = s.slice(i + 1);
            if (!after.includes("/")) return { repo: s.slice(0, i), tag: after };
        }
        return { repo: s, tag: "latest" };
    }
    function repoKey(ref) {
        return tagSplit(ref).repo
            .replace(/^docker\.io\//, "")
            .replace(/^library\//, "")
            .toLowerCase();
    }
    function verKey(image, tag) {
        return repoKey(image) + ":" + (tag || "latest");
    }
    function evKey(ev) {
        return (ev.hostname || "") + "|" + (ev.image || "") + "|" + (ev.digest || "");
    }
    function shortDigest(d) {
        if (!d) return "—";
        const m = /sha256:([0-9a-f]{12})/.exec(d);
        return m ? m[1] : d.slice(0, 12);
    }
    function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
        }[c]));
    }
    function human(n) {
        if (n == null || isNaN(n)) return "?";
        const units = ["B", "KB", "MB", "GB", "TB"];
        let i = 0;
        while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
        return (i === 0 ? n.toFixed(0) : n.toFixed(1)).replace(".", LANG === "de" ? "," : ".") + " " + units[i];
    }
    function fmtDate(d) {
        if (!d || isNaN(d)) return "—";
        return d.toLocaleString(LOCALE, { dateStyle: "short", timeStyle: "short" });
    }
    function ago(iso) {
        const ts = Date.parse(iso);
        if (isNaN(ts)) return null;
        const min = Math.max(0, Math.round((Date.now() - ts) / 60000));
        if (min < 1) return { text: t("ago.now"), hours: 0 };
        if (min < 60) return { text: t("ago.min", { n: min }), hours: min / 60 };
        const h = Math.round(min / 60);
        if (h < 48) return { text: t("ago.h", { n: h }), hours: h };
        const d = Math.round(h / 24);
        return { text: t("ago.d", { n: d }), hours: h };
    }
    // Every root action goes through here: fixed script + fixed verbs.
    function spawnHelper(argv, superuser) {
        return cockpit.spawn(argv, { superuser, err: superuser === "require" ? "out" : "message",
                                     environ: ["DIUN_LANG=" + LANG] });
    }
    function isNoAdmin(ex) {
        ex = ex || {};
        return ex.exit_status === 77 || /access-denied|not-authorized|authentication/.test(ex.problem || "");
    }

    // --- Parsing -----------------------------------------------------------
    function parseEvents(content) {
        const byRepo = new Map();
        (content || "").split("\n").forEach((line) => {
            line = line.trim();
            if (!line) return;
            let e;
            try { e = JSON.parse(line); } catch (_) { return; }
            if (!e.image) return;
            const k = repoKey(e.image);
            const prev = byRepo.get(k);
            if (!prev || (e.ts || "") >= (prev.ts || "")) byRepo.set(k, e);
        });
        return byRepo;
    }
    function parseInventory(stdout) {
        const out = [];
        (stdout || "").split("\n").forEach((line) => {
            line = line.trim();
            if (!line) return;
            try { out.push(JSON.parse(line)); } catch (_) { /* skip */ }
        });
        return out;
    }
    function parseVersions(doc) {
        const map = new Map();
        const images = (doc && Array.isArray(doc.images)) ? doc.images : [];
        images.forEach((r) => {
            if (r && r.image) map.set(verKey(r.image, r.tag), r);
        });
        return map;
    }

    // --- Rendering: updates table ------------------------------------------
    function badge(kind) {
        const map = { update: ["upd", "badge.update"], pulled: ["upd", "badge.pulled"],
                      newver: ["new", "badge.newver"], ok: ["ok", "badge.ok"],
                      unknown: ["unknown", "badge.unknown"] };
        const [cls, key] = map[kind] || map.unknown;
        const tip = kind === "pulled" ? ` title="${esc(t("badge.pulledTip"))}"` : "";
        return `<span class="dm-badge dm-badge-${cls}"${tip}>${esc(t(key))}</span>`;
    }

    const LEVELS = [["patch", "Patch"], ["minor", "Minor"], ["major", "Major"]];

    // Installed version of a container, first known source wins:
    //   inventory: the tag itself or the image's version variable (always fresh)
    //   radar:     image label or digest comparison with the registry (every 6 h)
    function installedOf(ctn, rec) {
        if (ctn.version) return { v: ctn.version, src: ctn.version_src || "tag" };
        const r = rec && rec.installed && ctn.imgid ? rec.installed[ctn.imgid] : null;
        if (r && r.version) return { v: r.version, src: r.src, tag: r.tag };
        return { v: null, src: null, looked: !!r };
    }
    function pulledVersionOf(ctn, rec) {
        if (ctn.pulled_version) return ctn.pulled_version;
        const r = rec && rec.installed && ctn.tagid ? rec.installed[ctn.tagid] : null;
        return r && r.version ? r.version : null;
    }
    function sourceText(inst) {
        const s = inst.src || "";
        if (s === "tag") return t("inst.tag");
        if (s.indexOf("env:") === 0) return t("inst.env", { name: s.slice(4) });
        if (s === "label") return t("inst.label");
        if (s === "digest") return t("inst.digest", { tag: inst.tag || inst.v });
        return "";
    }

    // "Installed" column: exact version + where it comes from (tooltip);
    // "→ 16.15" when a newer image is already pulled but not rolled out.
    function installedCell(ctn, rec, inst) {
        const lines = [inst.v ? sourceText(inst) : t(inst.looked ? "inst.unknown" : "inst.pending")];
        if (ctn.digest) lines.push(t("inst.digestLine", { d: shortDigest(ctn.digest) }));
        const title = esc(lines.filter(Boolean).join("\n"));
        let html = inst.v
            ? `<span class="dm-ver" title="${title}">${esc(inst.v)}</span>`
            : `<span class="dm-muted" title="${title}">?</span>`;
        const pv = ctn.pulled ? pulledVersionOf(ctn, rec) : null;
        if (pv && pv !== inst.v) {
            html += ` <span class="dm-arrow" title="${esc(t("inst.pulledTip", { v: pv }))}">→ ${esc(pv)}</span>`;
        }
        return `<span class="dm-inst">${html}</span>`;
    }

    // "Newest version" column. Returns { html, news }:
    //   news = an actionable newer version exists (patch/minor, or a major
    //          on a non-database image). A database major alone is grey info.
    // Chips are compared with the INSTALLED version; filled = the configured
    // tag delivers it via the Update button, outlined = needs a new tag.
    function versionCell(ctn, rec, inst, radarOk) {
        const muted = (text, title) => ({
            html: `<span class="dm-muted"${title ? ` title="${esc(title)}"` : ""}>${esc(text)}</span>`,
            news: false
        });
        if (!radarOk) return muted("—", t("ver.noRadar"));
        if (!rec) return muted("—", t("ver.notCovered"));
        let cmp = inst.v && rec.versions ? rec.versions[inst.v] : null;
        if (!cmp || cmp.status === "unknown") cmp = rec.by_tag || null;   // pg17 -> pg18
        if (!cmp) {
            if (rec.status === "error") return muted(t("ver.registryError"), rec.error || "");
            if (rec.newest) return muted(t("ver.newestOnly", { v: rec.newest }), t("ver.newestOnlyTip"));
            return muted("—", t("ver.notCovered"));
        }
        const stale = rec.stale
            ? ` <span class="dm-muted" title="${esc(rec.error || "")}">${esc(t("ver.stale"))}</span>` : "";
        const reach = Array.isArray(rec.reach) ? rec.reach : [];
        const chips = [];
        let news = false;
        LEVELS.forEach(([lvl, label]) => {
            if (!cmp[lvl]) return;
            const info = lvl === "major" && rec.db;
            if (!info) news = true;
            const can = reach.indexOf(lvl) > -1;
            const cls = info ? "dm-chip-info" : "dm-chip-" + lvl + (can ? "" : " dm-chip-manual");
            const title = [info ? t("ver.dbMajor") : t("ver.levelTip", { level: label }),
                           t(can ? "ver.reachYes" : "ver.reachNo", { tag: ctn.tag })].join("\n");
            chips.push(`<span class="dm-chip ${cls}" title="${esc(title)}">` +
                       `${label} <span class="dm-ver">${esc(cmp[lvl])}</span></span>`);
        });
        if (!chips.length) {
            return { html: `<span class="dm-uptodate" title="${esc(t("ver.upToDateTip"))}">✓` +
                           `${inst.v ? " " + esc(inst.v) : ""}</span>${stale}`, news: false };
        }
        return { html: `<span class="dm-chips">${chips.join("")}</span>${stale}`, news };
    }

    function rowFor(ctn, eventsMap, versionsMap, radarOk) {
        const ev = eventsMap.get(repoKey(ctn.image));
        const acknowledged = ev && dismissed[evKey(ev)];
        let kind;
        if (ctn.pulled) {
            kind = "pulled";        // newer image already local, container on the old one
        } else if (ev && ev.digest && ctn.digest && ev.digest !== ctn.digest && !acknowledged) {
            kind = "update";
        } else if (!ctn.digest) {
            kind = "unknown";
        } else {
            kind = "ok";
        }
        const rec = versionsMap.get(verKey(ctn.image, ctn.tag));
        const inst = installedOf(ctn, rec);
        const ver = versionCell(ctn, rec, inst, radarOk);
        if ((kind === "ok" || kind === "unknown") && ver.news) kind = "newver";
        // Without a known installed version "up to date" would be a guess.
        if (kind === "ok" && radarOk && rec && !inst.v) kind = "unknown";

        const imgCell = ev && ev.hub_link
            ? `<a href="${esc(ev.hub_link)}" target="_blank" rel="noopener">${esc(ctn.image)}</a>`
            : esc(ctn.image);

        const actions = [];
        if (kind === "update" || kind === "pulled") {
            if (ctn.managed) {
                actions.push(`<button class="dm-btn dm-btn-ghost dm-btn-strong" data-update="${esc(ctn.name)}" data-kind="${kind}">` +
                             `${esc(t(kind === "pulled" ? "btn.rollout" : "btn.update"))}</button>`);
            } else {
                actions.push(`<span class="dm-muted dm-small" title="${esc(t("row.manualOnlyTip"))}">${esc(t("row.manualOnly"))}</span>`);
            }
        }
        if (kind === "update") {
            actions.push(`<button class="dm-btn dm-btn-ghost" data-key="${esc(evKey(ev))}">${esc(t("btn.dismiss"))}</button>`);
        }

        const L = (key) => esc(t(key));
        return { kind, html:
            `<td data-label="${L("col.container")}"><span class="dm-cell">${esc(ctn.name)}</span></td>` +
            `<td data-label="${L("col.image")}" class="dm-img"><span class="dm-cell">${imgCell}</span></td>` +
            `<td data-label="${L("col.configured")}"><span class="dm-cell dm-ver dm-cfg" title="${L("cfg.tip")}">${esc(ctn.tag)}</span></td>` +
            `<td data-label="${L("col.installed")}"><span class="dm-cell">${installedCell(ctn, rec, inst)}</span></td>` +
            `<td data-label="${L("col.new")}"><span class="dm-cell">${ver.html}</span></td>` +
            `<td data-label="${L("col.status")}"><span class="dm-cell">${badge(kind)}</span></td>` +
            `<td class="dm-action" data-label=""><span class="dm-cell">${actions.join("")}</span></td>`
        };
    }

    const ORDER = { update: 0, pulled: 1, newver: 2, unknown: 3, ok: 4 };

    function radarMeta(radarDoc) {
        if (!radarDoc || !radarDoc.generated) return t("meta.radarNone");
        const a = ago(radarDoc.generated);
        if (!a) return t("meta.radarBad");
        return t("meta.radar", { ago: a.text }) + (a.hours > RADAR_STALE_H ? " " + t("meta.outdated") : "");
    }

    function render(containers, eventsMap, radarDoc) {
        const rows = $("dm-rows");
        rows.innerHTML = "";

        const radarOk = !!(radarDoc && Array.isArray(radarDoc.images));
        const versionsMap = parseVersions(radarDoc);
        const built = containers.map((c) => ({ c, ...rowFor(c, eventsMap, versionsMap, radarOk) }));
        built.sort((a, b) =>
            (ORDER[a.kind] - ORDER[b.kind]) || a.c.name.localeCompare(b.c.name));

        const updates = built.filter((b) => b.kind === "update" || b.kind === "pulled").length;
        const newver = built.filter((b) => b.kind === "newver").length;
        $("dm-meta").textContent = [
            t("meta.updates", { n: updates }),
            t("meta.newver", { n: newver }),
            t("meta.containers", { n: containers.length }),
            radarMeta(radarDoc),
        ].join(" · ");

        if (containers.length === 0) {
            $("dm-table").hidden = true;
            $("dm-legend").hidden = true;
            $("dm-empty").hidden = false;
            return;
        }
        $("dm-empty").hidden = true;
        $("dm-table").hidden = false;
        $("dm-legend").hidden = !radarOk;

        built.forEach((b) => {
            const tr = document.createElement("tr");
            tr.innerHTML = b.html;
            rows.appendChild(tr);
        });

        rows.querySelectorAll("button[data-key]").forEach((btn) => {
            btn.addEventListener("click", () => dismiss(btn.dataset.key));
        });
        rows.querySelectorAll("button[data-update]").forEach((btn) => {
            btn.addEventListener("click", () => startUpdate(btn.dataset.update, btn.dataset.kind));
        });
    }

    // --- Requirements (Docker, Diun, skopeo, radar) -------------------------
    function checkDeps() {
        return spawnHelper([DEPS_CMD, "check"], "try")
            .then((out) => { try { return JSON.parse(out); } catch (_) { return null; } })
            .catch(() => null);     // helper not deployed -> panel stays hidden
    }

    // Turns the check result into the list of problems the panel shows.
    // action = [verb, target] the helper accepts, or null with a hint instead.
    function depProblems(d) {
        const out = [];
        if (!d) return out;
        const dk = d.docker || {}, di = d.diun || {}, sk = d.skopeo || {}, ra = d.radar || {};
        if (dk.state !== "ok") {
            out.push({ id: "docker", state: t(dk.state === "no-cli" ? "state.missing" : "state.unreachable"),
                       hint: t("dep.docker.hint") });
        }
        if (di.state === "missing") {
            out.push({ id: "diun", state: t("state.missing"),
                       action: di.can_install ? ["install", "diun", "action.install"] : null,
                       hint: di.can_install ? "" : (dk.state === "ok" ? t("dep.diun.noCompose") : t("dep.docker.hint")) });
        } else if (di.state === "stopped") {
            out.push({ id: "diun", state: t("state.stopped"),
                       action: di.can_start ? ["start", "diun", "action.start"] : null,
                       hint: di.can_start ? "" : t("dep.diun.external") });
        } else if (di.state === "running" && !di.events) {
            out.push({ id: "diun", state: t("state.noEvents"), hint: t("dep.diun.noEvents") });
        }
        if (sk.state !== "ok") {
            out.push({ id: "skopeo", state: t("state.missing"),
                       action: sk.can_install ? ["install", "skopeo", "action.install"] : null,
                       hint: sk.can_install ? "" : t("dep.skopeo.noPm") });
        }
        if (ra.state !== "active") {
            out.push({ id: "radar", state: t(ra.state === "inactive" ? "state.inactive" : "state.missing"),
                       action: ra.can_install ? ["install", "radar", "action.setup"] : null,
                       hint: ra.can_install ? "" : t("dep.radar.noScript") });
        }
        return out;
    }

    function renderDeps(d) {
        const panel = $("dm-deps");
        const list = $("dm-deps-list");
        list.innerHTML = "";
        if (!d) { panel.hidden = true; return; }
        if (!d.root) {
            // Without admin rights the check can't see Docker; say so once.
            $("dm-deps-intro").textContent = t("deps.needAdmin");
            panel.hidden = false;
            return;
        }
        const problems = depProblems(d);
        if (!problems.length) { panel.hidden = true; return; }
        $("dm-deps-intro").textContent = t("deps.intro");
        problems.forEach((p) => {
            const li = document.createElement("li");
            li.className = "dm-dep";
            const btn = p.action
                ? `<button class="dm-btn dm-btn-ghost" data-verb="${esc(p.action[0])}" data-target="${esc(p.action[1])}">${esc(t(p.action[2]))}</button>`
                : "";
            li.innerHTML =
                `<div class="dm-dep-main">` +
                  `<span class="dm-dep-name">${esc(t("dep." + p.id))}</span> ` +
                  `<span class="dm-badge dm-badge-warn">${esc(p.state)}</span>` +
                  `<div class="dm-dep-desc">${esc(t("dep." + p.id + ".desc"))}</div>` +
                  (p.hint ? `<div class="dm-dep-hint">${esc(p.hint)}</div>` : "") +
                `</div>` +
                `<div class="dm-dep-action">${btn}</div>`;
            list.appendChild(li);
        });
        list.querySelectorAll("button[data-verb]").forEach((b) => {
            b.addEventListener("click", () => openInstallDialog(b.dataset.verb, b.dataset.target));
        });
        panel.hidden = false;
    }

    // --- Run the radar now ---------------------------------------------------
    // `diun-update.py radar` starts diun-radar.service and waits for the run;
    // systemd never runs it twice at once (a second start joins the run). The
    // finished run rewrites versions.json, and the watch reloads the table.
    let lastDeps = null;
    let radarRunning = false;

    function renderRadarButton() {
        const btn = $("dm-radar");
        const ra = lastDeps && lastDeps.root ? (lastDeps.radar || {}) : null;
        btn.hidden = !!(ra && ra.state === "missing");
        const busy = radarRunning || !!(ra && ra.running);
        btn.disabled = busy;
        btn.classList.toggle("is-busy", busy);
        btn.title = t("radar.tip");
        btn.querySelector(".dm-btn-label").textContent = t(busy ? "btn.radarRunning" : "btn.radar");
    }

    function runRadar() {
        if (radarRunning) return;
        radarRunning = true;
        renderRadarButton();
        let failed = null;
        // Native promise around Cockpit's: catch/finally everywhere, also for a throwing spawn.
        Promise.resolve().then(() => spawnHelper([UPDATE_CMD, "radar"], "require"))
            .catch((ex) => {
                ex = ex || {};
                failed = isNoAdmin(ex) ? t("dialog.noAdmin")
                    : ex.exit_status === 2 ? t("radar.notSetUp") : t("radar.failed");
            })
            .finally(() => {
                radarRunning = false;
                // The run is over, whatever the last check said.
                if (lastDeps && lastDeps.radar) lastDeps.radar.running = false;
                renderRadarButton();
                if (failed) showStatus(failed, true); else safeLoad();
            });
    }

    // --- Action dialog (one for everything) --------------------------------
    // dlg.open({ title, desc, body, confirm: { label, danger, argv() },
    //            onDone(outcome) })  outcome = { ok, exit, result, error }
    // The helper prints its machine-readable result as "@@RESULT {json}".
    const dlg = {
        running: false,
        spec: null,

        btn(id, label, opts) {
            const b = $(id);
            opts = opts || {};
            b.hidden = !opts.visible;
            b.disabled = !!opts.busy || !!opts.disabled;
            b.classList.toggle("is-busy", !!opts.busy);
            b.classList.toggle("dm-btn-danger", !!opts.danger);
            b.querySelector(".dm-btn-label").textContent = label || "";
        },

        open(spec) {
            this.spec = spec;
            $("dm-dialog-title").textContent = spec.title;
            $("dm-dialog-desc").textContent = spec.desc || "";
            $("dm-dialog-desc").hidden = !spec.desc;
            const body = $("dm-dialog-body");
            body.innerHTML = spec.body || "";
            body.hidden = !spec.body;
            $("dm-dialog-note").hidden = !spec.confirm;
            const log = $("dm-dialog-log");
            log.textContent = "";
            log.hidden = true;
            this.result("", null);
            this.btn("dm-dialog-extra", "", { visible: false });
            if (spec.confirm) {
                this.btn("dm-dialog-confirm", spec.confirm.label,
                         { visible: true, danger: spec.confirm.danger, disabled: spec.confirm.disabled });
            } else {
                this.btn("dm-dialog-confirm", "", { visible: false });
            }
            const cancel = $("dm-dialog-cancel");
            cancel.textContent = t(spec.confirm ? "dialog.cancel" : "dialog.close");
            cancel.disabled = false;
            const el = $("dm-dialog");
            if (!el.hasAttribute("open")) {
                if (typeof el.showModal === "function") el.showModal(); else el.setAttribute("open", "");
            }
        },

        // Replace the body while the dialog is open (e.g. once the plan is in).
        update(spec) {
            this.open(spec);
        },

        result(text, ok) {
            const res = $("dm-dialog-result");
            res.textContent = text;
            res.className = "dm-dialog-result" + (ok === true ? " is-ok" : ok === false ? " is-error" : "");
        },

        close() {
            if (this.running) return;
            const el = $("dm-dialog");
            if (typeof el.close === "function") el.close(); else el.removeAttribute("open");
        },

        // Runs argv as root, streams the output into the log, resolves with the outcome.
        run(argv, runningLabel) {
            this.running = true;
            const log = $("dm-dialog-log");
            log.hidden = false;
            $("dm-dialog-note").hidden = false;
            this.btn("dm-dialog-confirm", t("dialog.running"), { visible: true, busy: true });
            this.btn("dm-dialog-extra", "", { visible: false });
            $("dm-dialog-cancel").disabled = true;
            if (runningLabel) this.result(runningLabel, null);
            let result = null;
            let pendingLine = "";

            const append = (text) => {
                // Keep the @@RESULT line out of the visible log.
                pendingLine += text;
                const lines = pendingLine.split("\n");
                pendingLine = lines.pop();
                lines.forEach((ln) => {
                    if (ln.startsWith("@@RESULT ")) {
                        try { result = JSON.parse(ln.slice(9)); } catch (_) { /* ignore */ }
                    } else {
                        log.textContent += ln + "\n";
                    }
                });
                log.scrollTop = log.scrollHeight;
            };

            return new Promise((resolve) => {
                const done = (outcome) => {
                    if (pendingLine) append("\n");
                    this.running = false;
                    this.btn("dm-dialog-confirm", "", { visible: false });
                    const cancel = $("dm-dialog-cancel");
                    cancel.textContent = t("dialog.close");
                    cancel.disabled = false;
                    resolve({ ...outcome, result });
                };
                let proc;
                try {
                    proc = spawnHelper(argv, "require");
                } catch (ex) {
                    done({ ok: false, error: (ex && ex.message) || String(ex) });
                    return;
                }
                proc.stream(append);
                proc.then(() => done({ ok: true, exit: 0 }))
                    .catch((ex) => {
                        ex = ex || {};
                        if (isNoAdmin(ex)) done({ ok: false, noAdmin: true });
                        else if (typeof ex.exit_status === "number") done({ ok: false, exit: ex.exit_status });
                        else done({ ok: false, error: ex.message || ex.problem || String(ex) });
                    });
            });
        },

        // Default wording for a finished run.
        genericOutcome(o) {
            if (o.ok) return [t("dialog.success"), true];
            if (o.noAdmin) return [t("dialog.noAdmin"), false];
            if (o.exit === 75) return [t("upd.busy"), false];
            if (typeof o.exit === "number") return [t("dialog.failed", { code: o.exit }), false];
            return [t("dialog.error", { msg: o.error || "?" }), false];
        },
    };

    function confirmAndRun(spec) {
        dlg.open(spec);
        dlg.onConfirm = () => {
            const argv = spec.confirm.argv();
            dlg.run(argv).then((o) => {
                const handled = spec.onDone ? spec.onDone(o) : false;
                if (!handled) {
                    const [text, ok] = dlg.genericOutcome(o);
                    dlg.result(text, ok);
                }
                safeLoad();     // refresh everything after any root action
            });
        };
    }

    // --- Install requirement -------------------------------------------------
    function openInstallDialog(verb, target) {
        const base = "dlg." + verb + "." + target;
        const setup = target === "radar" || target === "cleanup";
        const label = { install: setup ? "action.setup" : "action.install", start: "action.start" }[verb];
        confirmAndRun({
            title: t(base + ".title"),
            desc: t(base + ".desc"),
            confirm: { label: t(label), argv: () => [DEPS_CMD, verb, target] },
        });
    }

    // --- Update ------------------------------------------------------------
    function planBody(plan) {
        const li = (label, value) =>
            `<li><span class="dm-kv-k">${esc(label)}</span><span class="dm-kv-v">${value}</span></li>`;
        const code = (s) => `<code>${esc(s)}</code>`;
        const repo = tagSplit(plan.image).repo;
        const dbs = plan.dbs || [];
        const dbLines = dbs.map((d) => {
            if (d.kind === "unsupported") return esc(t("upd.dbUnsupported", { name: d.name }));
            if (!d.running) return `<span class="dm-warn-text">${esc(t("upd.dbStopped", { name: d.name }))}</span>`;
            return esc(d.size ? t("upd.dbLine", { name: d.name, size: human(d.size) })
                              : t("upd.dbLineNoSize", { name: d.name }));
        });
        const dumpable = dbs.some((d) => d.kind === "postgres" || d.kind === "mysql");
        let html = `<ul class="dm-kv">` +
            li(t("upd.containers"), (plan.containers || []).map(code).join(" ")) +
            li(t("upd.project"), `${code(plan.project)} <span class="dm-muted">${esc(plan.workdir || "")}</span>`) +
            li(t("upd.rollback"), code(repo + ":rollback-…")) +
            li(t("upd.dbs"), dbLines.length ? dbLines.join("<br>") : esc(t("upd.noDb"))) +
            (dumpable && plan.free_bytes != null ? li("", `<span class="dm-muted">${esc(t("upd.free", { free: human(plan.free_bytes) }))}</span>`) : "") +
            `</ul>`;
        html += `<label class="dm-check"><input type="checkbox" id="dm-dump-check"${dumpable ? " checked" : " disabled"}> ` +
                `${esc(t("upd.dumpCheck"))}</label>`;
        if (plan.config_changed) html += `<p class="dm-callout dm-callout-warn">${esc(t("upd.configChanged"))}</p>`;
        html += `<p class="dm-callout">${esc(t("upd.dataWarn"))} ${esc(t("upd.downtime"))}</p>`;
        return html;
    }

    let planSeq = 0;

    function startUpdate(name, kind) {
        const title = t(kind === "pulled" ? "upd.titlePulled" : "upd.title", { name });
        const ticket = ++planSeq;
        // A plan that arrives after the dialog was closed (or after another
        // container's dialog was opened) must not pop the dialog up again.
        const stale = () => ticket !== planSeq || !$("dm-dialog").hasAttribute("open");
        dlg.open({ title, desc: t("upd.planning") });
        spawnHelper([UPDATE_CMD, "plan", name], "try")
            .then((out) => {
                if (stale()) return;
                let plan;
                try { plan = JSON.parse(out); } catch (_) { throw new Error("bad plan output"); }
                if (!plan.updatable) {
                    const reason = t("reason." + plan.reason, {
                        files: (plan.missing || []).join(", "), detail: plan.detail || "" });
                    dlg.update({ title, desc: t("upd.notPossible", { reason }) });
                    return;
                }
                const desc = t(kind === "pulled" || plan.pulled ? "upd.descPulled" : "upd.desc", { image: plan.image });
                confirmAndRun({
                    title, desc, body: planBody(plan),
                    confirm: {
                        label: t(kind === "pulled" ? "btn.rollout" : "btn.update"),
                        argv: () => {
                            const box = $("dm-dump-check");
                            const dump = box && box.checked && !box.disabled;
                            return [UPDATE_CMD, "update", name].concat(dump ? [] : ["--no-dump"]);
                        },
                    },
                    onDone: (o) => updateOutcome(o, name),
                });
            })
            .catch((ex) => {
                if (stale()) return;
                dlg.update({ title, desc: isNoAdmin(ex) ? t("dialog.noAdmin")
                                                       : t("dialog.error", { msg: (ex && (ex.message || ex.problem)) || ex }) });
            });
    }

    function updateOutcome(o, name) {
        const r = o.result || {};
        if (o.ok && r.status === "nochange") { dlg.result(t("upd.nochange"), true); return true; }
        if (o.ok) {
            const vers = r.from_version && r.to_version && r.from_version !== r.to_version;
            dlg.result(t(vers ? "upd.okVersions" : "upd.ok",
                         { rb: r.rollback || "", from: r.from_version, to: r.to_version }), true);
            return true;
        }
        if (o.exit === 4 && r.op) {
            dlg.result(t("upd.failed"), false);
            offerRollback(r.op, name);
            return true;
        }
        if (o.exit === 5) { dlg.result(t("upd.aborted"), false); return true; }
        return false;
    }

    function offerRollback(op, name) {
        dlg.btn("dm-dialog-extra", t("rb.button"), { visible: true, danger: true });
        dlg.onExtra = () => {
            dlg.run([UPDATE_CMD, "rollback", op]).then((o) => {
                dlg.result(o.ok ? t("rb.ok") : (o.noAdmin ? t("dialog.noAdmin") : t("rb.failed")), !!o.ok);
                safeLoad();
            });
        };
    }

    function startRollback(op) {
        confirmAndRun({
            title: t("rb.title", { name: op.container }),
            desc: t("rb.desc", { image: op.image, rb: op.rollback, services: (op.services || []).join(", ") }),
            confirm: { label: t("rb.button"), danger: true, argv: () => [UPDATE_CMD, "rollback", op.id] },
            onDone: (o) => {
                if (o.ok) { dlg.result(t("rb.ok"), true); return true; }
                if (o.exit === 4) { dlg.result(t("rb.failed"), false); return true; }
                return false;
            },
        });
    }

    // --- Backups tab -------------------------------------------------------
    let backups = null;

    function loadBackups() {
        return spawnHelper([UPDATE_CMD, "backups"], "try")
            .then((out) => { backups = JSON.parse(out); renderBackups(); })
            .catch((ex) => {
                backups = null;
                showStatus(isNoAdmin(ex) ? t("bk.needAdmin")
                                         : t("bk.error", { msg: (ex && (ex.message || ex.problem)) || ex }), true);
                renderBackups();
            });
    }

    function opResult(o) {
        if (o.rolled_back) {
            return o.rollback_status === "failed"
                ? `<span class="dm-badge dm-badge-warn">${esc(t("res.rollbackFailed"))}</span>`
                : `<span class="dm-badge dm-badge-unknown">${esc(t("res.rolledBack"))}</span>`;
        }
        const map = { ok: ["ok", "res.ok"], nochange: ["unknown", "res.nochange"],
                      failed: ["warn", "res.failed"], running: ["warn", "res.running"] };
        const [cls, key] = map[o.status] || map.running;
        return `<span class="dm-badge dm-badge-${cls}">${esc(t(key))}</span>`;
    }

    // "16.14 → 16.15"; older history entries have no versions.
    function opVersions(o) {
        const a = o.from_version, z = o.to_version;
        if (!a && !z) return `<span class="dm-muted">—</span>`;
        if (a === z) return `<span class="dm-ver">${esc(a)}</span>`;
        return `<span class="dm-ver">${esc(a || "?")} <span class="dm-arrow">→</span> ${esc(z || "?")}</span>`;
    }

    function cleanupMark() {
        return ` <span class="dm-badge dm-badge-warn" title="${esc(t("cl.markTip"))}">${esc(t("cl.mark"))}</span>`;
    }

    // Rules, daily timer and "what would be deleted now" (from `backups`),
    // the timer state from `diun-deps.sh check`.
    function renderCleanup() {
        const c = backups && backups.cleanup;
        $("dm-cleanup").hidden = !c;
        if (!c) return;
        const rules = $("dm-cleanup-rules");
        if (c.error) {
            rules.innerHTML = `<span class="dm-warn-text">${esc(t("cl.error", { msg: c.error }))}</span>`;
        } else {
            const keep = c.keep_dumps ? t("cl.keep", { n: c.keep_dumps }) : t("cl.keepOff");
            const days = c.image_days ? t("cl.days", { n: c.image_days }) : t("cl.daysOff");
            rules.textContent = [t("cl.rules", { keep, days }),
                                 c.file ? t("cl.srcFile", { file: c.file }) : t("cl.srcDefault"),
                                 t("cl.protect")].join(" ");
        }
        const timer = $("dm-cleanup-timer");
        const cl = lastDeps && lastDeps.root ? lastDeps.cleanup : null;
        timer.hidden = !cl;
        timer.textContent = cl ? t(cl.state === "active" ? "cl.timerOn" : "cl.timerOff") : "";
        if (cl && cl.state !== "active" && cl.can_install) {
            const setup = document.createElement("button");
            setup.className = "dm-btn dm-btn-ghost dm-btn-inline";
            setup.textContent = t("action.setup");
            setup.addEventListener("click", () => openInstallDialog("install", "cleanup"));
            timer.append(" ", setup);
        }
        const nd = (c.dumps || []).length, ni = (c.images || []).length;
        $("dm-cleanup-preview").textContent = c.error ? "" : (nd + ni)
            ? t("cl.preview", { dumps: t("cl.dumpsN", { n: nd }), images: t("cl.imagesN", { n: ni }), size: human(c.bytes) })
            : t("cl.nothing");
        $("dm-cleanup-btn").hidden = !(nd + ni) || !!c.error;
    }

    function startCleanup() {
        const c = backups && backups.cleanup;
        if (!c) return;
        const items = (c.dumps || []).concat(c.images || []);
        confirmAndRun({
            title: t("cl.dlgTitle"),
            desc: t("cl.dlgDesc"),
            body: `<ul class="dm-list">${items.map((x) => `<li><code>${esc(x)}</code></li>`).join("")}</ul>`,
            confirm: { label: t("cl.confirm"), danger: true, argv: () => [UPDATE_CMD, "cleanup"] },
            onDone: (o) => {
                if (!o.ok) return false;
                const r = o.result || {};
                if (r.status === "nochange") { dlg.result(t("cl.nochange"), true); return true; }
                const failed = (r.failed || []).length;
                dlg.result(t("cl.done", { dumps: t("cl.dumpsN", { n: (r.dumps || []).length }),
                                          images: t("cl.imagesN", { n: (r.images || []).length }),
                                          size: human(r.bytes || 0) }) +
                           (failed ? " " + t("cl.partial", { n: failed }) : ""), !failed);
                return true;
            },
        });
    }

    function renderBackups() {
        const b = backups || { dumps: [], images: [], ops: [] };
        const L = (key) => esc(t(key));
        const doomed = new Set(b.cleanup ? (b.cleanup.dumps || []).concat(b.cleanup.images || []) : []);
        renderCleanup();
        if (backups) {
            $("dm-bk-summary").textContent = t("bk.summary", {
                dumps: t("bk.dumpsN", { n: b.dumps.length, size: human(b.dump_bytes) }),
                images: t("bk.imagesN", { n: b.images.length }),
                free: human(b.free_bytes), dir: b.dir,
            });
        } else {
            $("dm-bk-summary").textContent = "";
        }

        const ops = $("dm-ops");
        ops.innerHTML = b.ops.map((o, i) =>
            `<tr>` +
            `<td data-label="${L("col.when")}"><span class="dm-cell">${esc(fmtDate(new Date(o.ts)))}</span></td>` +
            `<td data-label="${L("col.container")}"><span class="dm-cell">${esc(o.container || "")}</span></td>` +
            `<td data-label="${L("col.image")}"><span class="dm-cell dm-ver">${esc(o.image || "")}</span></td>` +
            `<td data-label="${L("col.version")}"><span class="dm-cell">${opVersions(o)}</span></td>` +
            `<td data-label="${L("col.result")}"><span class="dm-cell">${opResult(o)}</span></td>` +
            `<td data-label="${L("col.dumps")}"><span class="dm-cell">${(o.dumps || []).length}</span></td>` +
            `<td class="dm-action" data-label=""><span class="dm-cell">` +
              (o.can_rollback ? `<button class="dm-btn dm-btn-ghost" data-op="${i}">${L("rb.button")}</button>` : "") +
            `</span></td></tr>`).join("");
        $("dm-ops-empty").hidden = b.ops.length > 0;
        $("dm-ops-table").hidden = b.ops.length === 0;
        ops.querySelectorAll("button[data-op]").forEach((btn) => {
            btn.addEventListener("click", () => startRollback(b.ops[Number(btn.dataset.op)]));
        });

        const dumps = $("dm-dumps");
        dumps.innerHTML = b.dumps.map((d, i) =>
            `<tr>` +
            `<td data-label="${L("col.created")}"><span class="dm-cell">${esc(fmtDate(new Date(d.mtime * 1000)))}</span></td>` +
            `<td data-label="${L("col.project")}"><span class="dm-cell">${esc(d.project)}</span></td>` +
            `<td data-label="${L("col.container")}"><span class="dm-cell">${esc(d.container)}</span></td>` +
            `<td data-label="${L("col.file")}"><span class="dm-cell"><span class="dm-digest">${esc(d.path.split("/").pop())}</span>` +
              `${doomed.has(d.path) ? cleanupMark() : ""}</span></td>` +
            `<td data-label="${L("col.size")}"><span class="dm-cell">${esc(human(d.size))}</span></td>` +
            `<td class="dm-action" data-label=""><span class="dm-cell">` +
              `<button class="dm-btn dm-btn-ghost dm-btn-ghost-danger" data-dump="${i}">${L("btn.delete")}</button>` +
            `</span></td></tr>`).join("");
        $("dm-dumps-empty").hidden = b.dumps.length > 0;
        $("dm-dumps-table").hidden = b.dumps.length === 0;
        dumps.querySelectorAll("button[data-dump]").forEach((btn) => {
            const d = b.dumps[Number(btn.dataset.dump)];
            btn.addEventListener("click", () => confirmAndRun({
                title: t("del.dumpTitle"),
                desc: t("del.dumpDesc", { path: d.path }),
                confirm: { label: t("del.confirm"), danger: true, argv: () => [UPDATE_CMD, "delete-dump", d.path] },
                onDone: (o) => { if (o.ok) { dlg.result(t("del.ok"), true); return true; } return false; },
            }));
        });

        const imgs = $("dm-images");
        imgs.innerHTML = b.images.map((im, i) =>
            `<tr>` +
            `<td data-label="${L("col.image")}"><span class="dm-cell dm-ver">${esc(im.ref)}</span></td>` +
            `<td data-label="${L("col.created")}"><span class="dm-cell">${esc(String(im.created || "").slice(0, 16))}</span></td>` +
            `<td data-label="${L("col.size")}"><span class="dm-cell">${esc(im.size)}</span></td>` +
            `<td data-label="${L("col.status")}"><span class="dm-cell">` +
              (im.in_use ? `<span class="dm-badge dm-badge-new" title="${L("img.inUseTip")}">${L("img.inUse")}</span>`
                         : `<span class="dm-muted">${L("img.free")}</span>${doomed.has(im.ref) ? cleanupMark() : ""}`) +
            `</span></td>` +
            `<td class="dm-action" data-label=""><span class="dm-cell">` +
              `<button class="dm-btn dm-btn-ghost dm-btn-ghost-danger" data-img="${i}"` +
              (im.in_use ? ` disabled title="${L("img.inUseTip")}"` : "") + `>${L("btn.delete")}</button>` +
            `</span></td></tr>`).join("");
        $("dm-images-empty").hidden = b.images.length > 0;
        $("dm-images-table").hidden = b.images.length === 0;
        imgs.querySelectorAll("button[data-img]").forEach((btn) => {
            const im = b.images[Number(btn.dataset.img)];
            btn.addEventListener("click", () => confirmAndRun({
                title: t("del.imgTitle"),
                desc: t("del.imgDesc", { ref: im.ref }),
                confirm: { label: t("del.confirm"), danger: true, argv: () => [UPDATE_CMD, "delete-image", im.ref] },
                onDone: (o) => { if (o.ok) { dlg.result(t("del.ok"), true); return true; } return false; },
            }));
        });
    }

    // --- Tabs ----------------------------------------------------------------
    let currentTab = "updates";

    function showTab(name) {
        currentTab = name;
        document.querySelectorAll(".dm-tab").forEach((b) => {
            const on = b.dataset.tab === name;
            b.classList.toggle("is-active", on);
            b.setAttribute("aria-selected", on ? "true" : "false");
        });
        $("dm-tab-updates").hidden = name !== "updates";
        $("dm-tab-backups").hidden = name !== "backups";
        if (name === "backups") safeLoad();
    }

    // --- State -------------------------------------------------------------
    function dismiss(key) {
        dismissed[key] = true;
        dismissedFile.replace(dismissed)
            .catch((err) => showStatus(t("status.saveError", { msg: err.message || err }), true))
            .finally(safeLoad);
    }
    function showStatus(msg, isError) {
        const el = $("dm-status");
        el.textContent = msg;
        el.classList.toggle("dm-status-error", !!isError);
        el.hidden = false;
    }

    // --- Load --------------------------------------------------------------
    function load() {
        const inv = spawnHelper([INVENTORY_CMD], "try");
        const jobs = Promise.all([
            inv.catch(() => null),
            eventsFile.read().catch(() => null),
            dismissedFile.read().catch(() => null),
            versionsFile.read().catch(() => null),
            checkDeps(),
        ]).then(([invOut, eventsContent, dis, radarDoc, deps]) => {
            dismissed = dis || {};
            lastDeps = deps;
            renderDeps(deps);
            renderRadarButton();
            const eventsMap = parseEvents(eventsContent);
            if (invOut === null) {
                // Inventory unavailable (no Docker access / no admin rights):
                // fall back to a Diun-events-only view.
                showStatus(t("status.noInventory"), false);
                const evContainers = Array.from(eventsMap.values()).map((e) => {
                    const tg = tagSplit(e.image);
                    return { name: "—", image: tg.repo, tag: tg.tag, digest: "" };
                });
                render(evContainers, eventsMap, radarDoc);
                return;
            }
            $("dm-status").hidden = true;
            render(parseInventory(invOut), eventsMap, radarDoc);
        }).catch((err) => {
            showStatus(t("status.loadError", { msg: err.message || err }), true);
        });
        // The backups listing runs only while its tab is open.
        return currentTab === "backups" ? jobs.then(loadBackups) : jobs;
    }

    // Loading indicator.
    //   First load : full-width "loading overview" block (in the HTML from
    //                the first paint, so there is no blank page).
    //   Later loads: the table stays visible but dimmed, and the refresh
    //                button shows a spinner and is disabled until done.
    function setBusy(on) {
        const btn = $("dm-refresh");
        btn.disabled = on;
        btn.classList.toggle("is-busy", on);
        btn.querySelector(".dm-btn-label").textContent = t(on ? "btn.loading" : "btn.refresh");
        document.querySelectorAll(".dm-table-wrap").forEach((wrap) => {
            wrap.classList.toggle("is-busy", on);
            wrap.setAttribute("aria-busy", on ? "true" : "false");
        });
        if (!on) $("dm-loading").hidden = true;    // first load is over for good
    }

    // Never run two loads at once; a request during a load is queued once.
    // (2026-10-12: watch() + read() re-triggered load() in a loop and started
    // hundreds of inventory scripts in parallel.)
    let loading = false;
    let pending = false;
    function safeLoad() {
        if (loading) { pending = true; return; }
        loading = true;
        setBusy(true);
        // Promise.resolve().then(): even a synchronous throw ends in finally,
        // so the indicator can never get stuck.
        Promise.resolve().then(load).catch((err) => {
            showStatus(t("status.loadError", { msg: (err && err.message) || err }), true);
        }).finally(() => {
            loading = false;
            if (pending) { pending = false; safeLoad(); return; }
            setBusy(false);
        });
    }

    // Reload only when a watched file really changed (new tag), not when our
    // own read() reports the same content again. First callback = initial
    // state, already covered by the initial safeLoad().
    function reloadOnChange(file) {
        let seen = false;
        let lastTag = null;
        file.watch((content, tag) => {
            if (seen && tag === lastTag) return;
            const first = !seen;
            seen = true;
            lastTag = tag;
            if (!first) safeLoad();
        });
    }

    // --- Init --------------------------------------------------------------
    applyStaticTexts();
    $("dm-refresh").addEventListener("click", () => safeLoad());
    $("dm-radar").addEventListener("click", runRadar);
    $("dm-cleanup-btn").addEventListener("click", startCleanup);
    document.querySelectorAll(".dm-tab").forEach((b) => {
        b.addEventListener("click", () => showTab(b.dataset.tab));
    });
    $("dm-dialog-confirm").addEventListener("click", () => {
        if (!dlg.running && dlg.onConfirm) dlg.onConfirm();
    });
    $("dm-dialog-extra").addEventListener("click", () => {
        if (!dlg.running && dlg.onExtra) dlg.onExtra();
    });
    $("dm-dialog-cancel").addEventListener("click", () => dlg.close());
    $("dm-dialog").addEventListener("cancel", (e) => { if (dlg.running) e.preventDefault(); });
    reloadOnChange(eventsFile);
    reloadOnChange(versionsFile);   // new radar run -> table updates itself
    safeLoad();

    cockpit.transport.wait(function () {});
})();

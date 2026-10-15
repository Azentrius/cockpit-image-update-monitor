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
 * as root lives in /usr/local/lib/diun-radar, never in /var/lib/diun, which
 * is writable from inside the Diun container (2026-10-12).
 *
 * Updates ("stage 1"): same tag only: pull + recreate the compose services
 * that use the image, after a database dump and with a rollback copy of the
 * old image. Dumps and rollback copies are listed on the "Backups" tab.
 *
 * Version changes ("stage 2"): a selection field with what the radar offers
 * for the installed version (versions[v].choices); the helper shows the
 * change of the compose file first (retag --dry-run) and makes exactly that
 * change (retag --expect <fingerprint>), with a byte-exact copy of the files.
 *
 * Language: Cockpit's session language (cockpit.language, set by
 * ../base1/po.js). German -> DE, anything else -> EN.
 *
 * No build step, no framework, no external requests; same model as cronctl.
 */

(function () {
    "use strict";

    // --- Configuration -----------------------------------------------------
    // The one place for the version (SemVer). index.html loads every script and
    // stylesheet of the add-on with ?v=VERSION; tools/check-version.sh keeps
    // them in step.
    const VERSION = "0.11.0";
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
            "tabs.label": "Sections",
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
            "col.actions": "Actions",
            "empty": "No running containers found.",
            "badge.update": "Update available",
            "badge.pulled": "Roll-out pending",
            "badge.pulledTip": "A newer image is already on the server, but the container still runs the old one.",
            "badge.newver": "New version",
            "badge.ok": "Up to date",
            "badge.unknown": "Unknown",
            "row.manualOnly": "manual only",
            "row.manualOnlyTip": "Not started with docker compose – the add-on cannot recreate it safely.",
            "cfg.tip": "Tag as set in the compose file (or with docker run).",
            "cfg.tagNow": "In the registry, {tag} points to {v} right now (checked by the radar).",
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
            "ver.clickRetag": "Click to change the tag to {tag}.",
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
            "status.noInventory": "Container inventory unavailable (Docker unreachable or no administrative access). Showing reported Diun updates only.",
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
            "upd.cardTitle": "Running containers",
            "upd.planning": "Checking what the update affects …",
            "upd.containers": "Recreated (same image)",
            "upd.jobs": "One-off jobs (run again)",
            "upd.dependsOn": "Waits for",
            "upd.dependents": "Depend on it (watched afterwards)",
            "cond.healthy": "healthy",
            "cond.started": "started",
            "cond.completed": "finished successfully",
            "upd.jobOrder": "{services} do not wait for {job}: they can start before it is done. In the compose file, depends_on with condition: service_completed_successfully makes them wait.",
            "upd.sharedNet": "Also on the network {network}: {projects}. They may use this project; the update interrupts it briefly.",
            "row.together": "together with {names}",
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
            "upd.installed": "Installed version",
            "upd.versionUnknown": "unknown",
            "upd.jumpUnknown": "The installed version is unknown and the tag {tag} follows every new release, so nobody can tell how big the jump is. It may be a new major version that needs other settings or additional services. Check the release notes first.",
            "upd.jumpConfirm": "I know that this update may bring a new major version.",
            "upd.jumpTarget": "{tag} points to {v} right now.",
            "upd.target": "Brings",
            "upd.targetSame": "{v} (the version that runs now)",
            "upd.majorJump": "This update brings a new major version: {from} → {to}. New major versions often need other settings or additional services; check the release notes first.",
            "upd.majorConfirm": "I have checked the release notes for {to}.",
            "upd.notPossible": "Not possible from the add-on: {reason}",
            "reason.not-compose": "the container was not started with docker compose, so it cannot be recreated safely.",
            "reason.compose-file-missing": "the compose file no longer exists ({files}).",
            "reason.digest-pinned": "the image is pinned by digest (@sha256) – updating means editing the compose file.",
            "reason.built-locally": "the image is built locally (build:) – rebuild it in the project.",
            "reason.compose-config-error": "docker compose config fails: {detail}",
            "reason.same-tag": "{tag} is the tag configured now.",
            "reason.no-choices": "the radar has no versions to offer for the installed version (it is unknown, follows no version scheme, or the radar data is older than 0.11.0).",
            "reason.not-offered": "the radar does not offer {tag} for the installed version: it is older, a pre-release or another flavour.",
            "reason.db-major": "a new major version of a database needs a migration (dump and restore or pg_upgrade); the add-on cannot do that yet.",
            "reason.major-skip": "Nextcloud can only be upgraded one major version per step.",
            "reason.anchors": "the image line sits under a YAML anchor or outside a service ({detail}). Please change the tag by hand.",
            "reason.not-found": "no image line with this reference was found for {detail}, for example because of include: or a generated file. Please change the tag by hand.",
            "reason.variable-complex": "the tag comes from a variable that cannot be changed safely ({detail}): it is not in the .env file, set more than once, only has a default, or the reference is built from several variables.",
            "reason.variable-shared": "the variable {detail} is used elsewhere too; changing it would change more than this image.",
            "reason.unreadable": "{detail} is not a regular UTF-8 file of at most 1 MiB.",
            "upd.ok": "Update successful. Rollback copy: {rb}",
            "upd.okVersions": "Update successful: {from} → {to}. Rollback copy: {rb}",
            "upd.nochange": "No newer image available – nothing changed.",
            "upd.failed": "Update failed – the containers do not run stably. You can roll back to the previous version.",
            "upd.aborted": "Aborted before anything was changed – see output.",
            "upd.busy": "Another update or rollback is already running.",
            "upd.reasonTitle": "Most likely cause ({name})",
            "upd.reasonComposeTitle": "Most likely cause (docker compose)",
            "upd.reasonHealth": "Health check: {line}",
            "upd.reasonOom": "Stopped because it ran out of memory.",
            "upd.reasonExit": "Stopped with exit code {code}; the log shows no error line.",
            "upd.reasonTimeout": "Not ready within the waiting time; the log shows no error line.",
            "upd.reasonNone": "The log shows no error line; see the output.",
            "upd.failedBuild": "The update to exactly this build failed on {date}. Try again only if the cause is fixed, for example a setting in the compose file.",
            "upd.retryConfirm": "Try this build again anyway.",
            "row.failedBuild": "The update to this build failed on {date}.",
            "btn.retag": "Change version",
            "rt.title": "Change the version of {name}?",
            "rt.desc": "Changes the tag in the compose file and rolls out the version it names. As with an update, a database dump and a rollback copy come first; the files are copied byte for byte, and a rollback puts them back.",
            "rt.select": "Tag in the compose file",
            "rt.help": "Installed: {v}. Only versions of the same scheme are offered, none older; going back works with a rollback.",
            "rt.optConfigured": "{tag} (configured now)",
            "rt.optPin": "{tag} (runs now)",
            "rt.optLine": "{tag} (currently {v})",
            "rt.optDbMajor": "{tag} – database major, needs a migration",
            "rt.optSkip": "{tag} – Nextcloud: one major version per step",
            "rt.grp.pin": "Pin the running version",
            "rt.grp.lines": "Version lines: updates within the line",
            "rt.grp.patch": "Patch versions",
            "rt.grp.minor": "Minor versions",
            "rt.grp.major": "Major versions",
            "rt.grp.floating": "Follows every release",
            "rt.checking": "Checking the change …",
            "rt.diff": "What changes in the files",
            "rt.variable": "The tag comes from the variable {name}; it changes where it is set.",
            "rt.git": "The project lies in the git repository {path}. The add-on does not commit; commit the change there if you want it in the history.",
            "rt.floating": "{tag} follows every new release, new major versions included: which version it brings shows only after the pull, and every later update can bring a new major version. Check the release notes first.",
            "rt.floatingConfirm": "I know that {tag} follows every new release.",
            "rt.ok": "Version changed: the compose file now names {tag}. Rollback copy: {rb}",
            "rt.okVersions": "Version changed: {from} → {to}, the compose file now names {tag}. Rollback copy: {rb}",
            "rt.failed": "Tag change failed – the containers do not run stably. Rolling back puts the files and the previous image back.",
            "op.retag": "Tag changed: {from} → {to}",
            "btn.showLog": "Show output",
            "log.title": "Output: {name}",
            "log.desc": "{when} · {image}",
            "log.loading": "Loading the output …",
            "log.none": "No output is kept for this operation (updates before 0.9.0 kept none).",
            "rb.button": "Roll back",
            "rb.title": "Roll back {name}?",
            "rb.desc": "Points {image} back to {rb} and recreates {services}. Databases are not reset – if migrations cause trouble, restore the dump.",
            "rb.descRetag": "Puts back {files} as they were before the tag change, points {image} back to {rb} and recreates {services}. Databases are not reset – if migrations cause trouble, restore the dump.",
            "rb.ok": "Rollback successful.",
            "rb.failed": "Rollback failed – see output.",
            "rb.aborted": "Rollback stopped before anything was changed – see output.",
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
            "tabs.label": "Bereiche",
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
            "col.actions": "Aktionen",
            "empty": "Keine laufenden Container gefunden.",
            "badge.update": "Update verfügbar",
            "badge.pulled": "Ausrollen ausstehend",
            "badge.pulledTip": "Ein neueres Image liegt schon auf dem Server, der Container läuft aber noch auf dem alten.",
            "badge.newver": "Neue Version",
            "badge.ok": "Aktuell",
            "badge.unknown": "Unbekannt",
            "row.manualOnly": "nur manuell",
            "row.manualOnlyTip": "Nicht mit docker compose gestartet – das Addon kann ihn nicht sicher neu erstellen.",
            "cfg.tip": "Tag, wie er in der Compose-Datei (bzw. bei docker run) steht.",
            "cfg.tagNow": "In der Registry zeigt {tag} gerade auf {v} (vom Radar geprüft).",
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
            "ver.clickRetag": "Klicken, um den Tag auf {tag} zu ändern.",
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
            "status.noInventory": "Container-Inventar nicht verfügbar (Docker nicht erreichbar oder kein administrativer Zugriff). Es werden nur gemeldete Diun-Updates gezeigt.",
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
            "upd.cardTitle": "Laufende Container",
            "upd.planning": "Prüfe, was das Update betrifft …",
            "upd.containers": "Neu erstellt (gleiches Image)",
            "upd.jobs": "Einmal-Jobs (laufen erneut)",
            "upd.dependsOn": "Wartet auf",
            "upd.dependents": "Hängen davon ab (werden danach beobachtet)",
            "cond.healthy": "gesund",
            "cond.started": "gestartet",
            "cond.completed": "erfolgreich beendet",
            "upd.jobOrder": "{services} warten nicht auf {job}: Sie können starten, bevor der Job fertig ist. In der Compose-Datei sorgt depends_on mit condition: service_completed_successfully dafür, dass sie warten.",
            "upd.sharedNet": "Ebenfalls im Netzwerk {network}: {projects}. Sie nutzen dieses Projekt womöglich; das Update unterbricht es kurz.",
            "row.together": "zusammen mit {names}",
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
            "upd.installed": "Installierte Version",
            "upd.versionUnknown": "unbekannt",
            "upd.jumpUnknown": "Die installierte Version ist unbekannt, und der Tag {tag} folgt jeder neuen Version. Wie groß der Sprung ist, lässt sich deshalb nicht sagen. Es kann eine neue Hauptversion sein, die andere Einstellungen oder zusätzliche Dienste braucht. Prüfen Sie vorher die Release-Notes.",
            "upd.jumpConfirm": "Mir ist bewusst, dass dieses Update eine neue Hauptversion bringen kann.",
            "upd.jumpTarget": "{tag} zeigt gerade auf {v}.",
            "upd.target": "Bringt",
            "upd.targetSame": "{v} (die Version, die jetzt läuft)",
            "upd.majorJump": "Dieses Update bringt eine neue Hauptversion: {from} → {to}. Neue Hauptversionen brauchen oft andere Einstellungen oder zusätzliche Dienste; prüfen Sie vorher die Release-Notes.",
            "upd.majorConfirm": "Ich habe die Release-Notes zu {to} geprüft.",
            "upd.notPossible": "Über das Addon nicht möglich: {reason}",
            "reason.not-compose": "Der Container wurde nicht mit docker compose gestartet und lässt sich daher nicht sicher neu erstellen.",
            "reason.compose-file-missing": "Die Compose-Datei existiert nicht mehr ({files}).",
            "reason.digest-pinned": "Das Image ist per Digest festgelegt (@sha256) – ein Update heißt hier, die Compose-Datei zu ändern.",
            "reason.built-locally": "Das Image wird lokal gebaut (build:) – bitte im Projekt neu bauen.",
            "reason.compose-config-error": "docker compose config schlägt fehl: {detail}",
            "reason.same-tag": "{tag} ist der Tag, der jetzt konfiguriert ist.",
            "reason.no-choices": "Der Radar hat für die installierte Version keine Versionen anzubieten (sie ist unbekannt, folgt keinem Versionsschema, oder die Radar-Daten sind älter als 0.11.0).",
            "reason.not-offered": "Der Radar bietet {tag} für die installierte Version nicht an: Er ist älter, eine Vorabversion oder eine andere Variante.",
            "reason.db-major": "Eine neue Hauptversion einer Datenbank braucht eine Migration (Dump und Restore bzw. pg_upgrade); das kann das Addon noch nicht.",
            "reason.major-skip": "Nextcloud lässt sich nur um eine Hauptversion pro Schritt aktualisieren.",
            "reason.anchors": "Die Image-Zeile steht unter einem YAML-Anker oder außerhalb eines Service ({detail}). Bitte ändern Sie den Tag von Hand.",
            "reason.not-found": "Für {detail} wurde keine Image-Zeile mit dieser Referenz gefunden, etwa wegen include: oder einer erzeugten Datei. Bitte ändern Sie den Tag von Hand.",
            "reason.variable-complex": "Der Tag kommt aus einer Variable, die sich nicht sicher ändern lässt ({detail}): Sie steht nicht in der .env-Datei, ist mehrfach gesetzt, hat nur einen Standardwert, oder die Referenz setzt sich aus mehreren Variablen zusammen.",
            "reason.variable-shared": "Die Variable {detail} wird auch anderswo verwendet; sie zu ändern, würde mehr als dieses Image ändern.",
            "reason.unreadable": "{detail} ist keine reguläre UTF-8-Datei von höchstens 1 MiB.",
            "upd.ok": "Update erfolgreich. Rollback-Kopie: {rb}",
            "upd.okVersions": "Update erfolgreich: {from} → {to}. Rollback-Kopie: {rb}",
            "upd.nochange": "Kein neueres Image verfügbar – nichts geändert.",
            "upd.failed": "Update fehlgeschlagen – die Container laufen nicht stabil. Sie können auf die vorherige Version zurückrollen.",
            "upd.aborted": "Abgebrochen, bevor etwas geändert wurde – siehe Ausgabe.",
            "upd.busy": "Es läuft bereits ein Update oder Rollback.",
            "upd.reasonTitle": "Vermutliche Ursache ({name})",
            "upd.reasonComposeTitle": "Vermutliche Ursache (docker compose)",
            "upd.reasonHealth": "Health-Check: {line}",
            "upd.reasonOom": "Wegen Speichermangels beendet.",
            "upd.reasonExit": "Mit Exit-Code {code} beendet; im Log steht keine Fehlerzeile.",
            "upd.reasonTimeout": "In der Wartezeit nicht bereit geworden; im Log steht keine Fehlerzeile.",
            "upd.reasonNone": "Im Log steht keine Fehlerzeile; siehe Ausgabe.",
            "upd.failedBuild": "Das Update auf genau diesen Build ist am {date} gescheitert. Versuchen Sie es nur erneut, wenn die Ursache behoben ist, etwa eine Einstellung in der Compose-Datei.",
            "upd.retryConfirm": "Diesen Build trotzdem erneut versuchen.",
            "row.failedBuild": "Das Update auf diesen Build ist am {date} gescheitert.",
            "btn.retag": "Version ändern",
            "rt.title": "Version von {name} ändern?",
            "rt.desc": "Ändert den Tag in der Compose-Datei und rollt die Version aus, die er nennt. Wie beim Update kommen vorher ein Datenbank-Dump und eine Rollback-Kopie; die Dateien werden byte-genau gesichert, und ein Rollback stellt sie wieder her.",
            "rt.select": "Tag in der Compose-Datei",
            "rt.help": "Installiert: {v}. Angeboten werden nur Versionen im selben Schema und keine älteren; zurück geht es per Rollback.",
            "rt.optConfigured": "{tag} (jetzt konfiguriert)",
            "rt.optPin": "{tag} (läuft jetzt)",
            "rt.optLine": "{tag} (derzeit {v})",
            "rt.optDbMajor": "{tag} – Datenbank-Major, braucht eine Migration",
            "rt.optSkip": "{tag} – Nextcloud: nur eine Hauptversion pro Schritt",
            "rt.grp.pin": "Laufende Version festsetzen",
            "rt.grp.lines": "Versionslinien: Updates innerhalb der Linie",
            "rt.grp.patch": "Patch-Versionen",
            "rt.grp.minor": "Minor-Versionen",
            "rt.grp.major": "Major-Versionen",
            "rt.grp.floating": "Folgt jeder neuen Version",
            "rt.checking": "Prüfe die Änderung …",
            "rt.diff": "Was sich in den Dateien ändert",
            "rt.variable": "Der Tag kommt aus der Variable {name}; sie wird dort geändert, wo sie gesetzt ist.",
            "rt.git": "Das Projekt liegt im Git-Repository {path}. Das Addon committet nicht; übernehmen Sie die Änderung dort, wenn sie in die Historie soll.",
            "rt.floating": "{tag} folgt jeder neuen Version, auch neuen Hauptversionen: Welche Version er bringt, zeigt sich erst nach dem Pull, und jedes spätere Update kann eine neue Hauptversion bringen. Prüfen Sie vorher die Release-Notes.",
            "rt.floatingConfirm": "Mir ist bewusst, dass {tag} jeder neuen Version folgt.",
            "rt.ok": "Version geändert: Die Compose-Datei nennt jetzt {tag}. Rollback-Kopie: {rb}",
            "rt.okVersions": "Version geändert: {from} → {to}, die Compose-Datei nennt jetzt {tag}. Rollback-Kopie: {rb}",
            "rt.failed": "Tag-Wechsel fehlgeschlagen – die Container laufen nicht stabil. Zurückrollen stellt die Dateien und das vorherige Image wieder her.",
            "op.retag": "Tag geändert: {from} → {to}",
            "btn.showLog": "Ausgabe anzeigen",
            "log.title": "Ausgabe: {name}",
            "log.desc": "{when} · {image}",
            "log.loading": "Lädt die Ausgabe …",
            "log.none": "Für diesen Vorgang ist keine Ausgabe gespeichert (Updates vor 0.9.0 speicherten keine).",
            "rb.button": "Zurückrollen",
            "rb.title": "{name} zurückrollen?",
            "rb.desc": "Setzt {image} wieder auf {rb} und erstellt {services} neu. Datenbanken werden nicht zurückgesetzt – bei Migrationsproblemen den Dump einspielen.",
            "rb.descRetag": "Stellt {files} wieder her, wie sie vor dem Tag-Wechsel waren, setzt {image} wieder auf {rb} und erstellt {services} neu. Datenbanken werden nicht zurückgesetzt – bei Migrationsproblemen den Dump einspielen.",
            "rb.ok": "Rollback erfolgreich.",
            "rb.failed": "Rollback fehlgeschlagen – siehe Ausgabe.",
            "rb.aborted": "Rollback abgebrochen, bevor etwas geändert wurde – siehe Ausgabe.",
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

    // --- Icons -------------------------------------------------------------
    // All icons come from the shared set, window.AzIcons in icons.js (Font
    // Awesome Free 5 Solid, CC BY 4.0, as shipped by PatternFly).
    // Inline SVG (HTML string) in the text colour; "" when the set is missing.
    function icon(name) {
        const def = (window.AzIcons || {})[name];
        if (!def) return "";
        return `<svg class="az-icon" viewBox="${def.viewBox}" aria-hidden="true" focusable="false" data-icon="${name}">` +
               `<path d="${def.d}"></path></svg>`;
    }

    function applyStaticTexts() {
        document.documentElement.lang = LANG;
        document.querySelectorAll("[data-i18n]").forEach((el) => {
            el.textContent = t(el.dataset.i18n);
        });
        document.querySelectorAll("[data-i18n-aria]").forEach((el) => {
            el.setAttribute("aria-label", t(el.dataset.i18nAria));
        });
        document.querySelectorAll("span[data-icon]").forEach((el) => {
            el.outerHTML = icon(el.dataset.icon);
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
        const [cls, ic, key] = STATUS[kind] || STATUS.unknown;
        return label(cls, ic, t(key), kind === "pulled" ? t("badge.pulledTip") : "");
    }

    // Status language of the design system: colour, icon and word say the
    // same; gold is never a status. kind "" = without judgement.
    const STATUS = {
        update: ["info", "info-circle", "badge.update"],
        pulled: ["info", "info-circle", "badge.pulled"],
        newver: ["info", "info-circle", "badge.newver"],
        ok: ["success", "check-circle", "badge.ok"],
        unknown: ["", "ban", "badge.unknown"],
    };
    function label(kind, iconName, text, title) {
        return `<span class="az-label${kind ? " az-label--" + kind : ""}"${title ? ` title="${esc(title)}"` : ""}>` +
               `${iconName ? icon(iconName) : ""}${esc(text)}</span>`;
    }
    // Text button with an icon on the left (az-btn--secondary az-btn--sm).
    function textButton(iconName, text, attrs) {
        return `<button class="az-btn az-btn--secondary az-btn--sm" type="button" ${attrs}>${icon(iconName)}<span>${esc(text)}</span></button>`;
    }
    // Tool in a row: icon only, the word as aria-label and tooltip.
    function iconButton(iconName, text, attrs, title) {
        return `<button class="az-btn az-btn--plain az-btn--sm" type="button" aria-label="${esc(text)}" ` +
               `title="${esc(title || text)}" ${attrs}>${icon(iconName)}</button>`;
    }

    const LEVELS = [["patch", "Patch"], ["minor", "Minor"], ["major", "Major"]];
    const LEVEL_NAMES = { patch: "Patch", minor: "Minor", major: "Major" };

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

    // "Configured" column: what a floating tag (latest, 16-alpine) points to
    // in the registry right now, from the radar ("tag_now"). Only when that
    // counts like the installed version: pgvector:pg17 runs PostgreSQL 17.10,
    // its tags count pgvector releases (0.8.7-pg17).
    function tagNowNote(ctn, rec, inst) {
        const v = rec && rec.tag_now && rec.tag_now.version;
        const cmp = inst.v && rec && rec.versions ? rec.versions[inst.v] : null;
        if (!v || (inst.v && (!cmp || cmp.status === "unknown"))) return "";
        return ` <span class="dm-tagnow" title="${esc(t("cfg.tagNow", { tag: ctn.tag, v }))}">= ${esc(v)}</span>`;
    }

    // What the selection field of "Change version" offers: the radar's
    // versions[installed].choices (since 0.11.0), in the field's order, as
    // [{ group, tag, text, disabled }]. Database majors and Nextcloud jumps
    // over more than one major are listed but cannot be chosen; the helper
    // refuses them as well (db-major, major-skip). The configured tag is the
    // field's first entry and is left out here.
    const NEXTCLOUD_RE = /(^|\/)nextcloud$/;

    function majorOf(v) {
        const m = /\d+/.exec(String(v || ""));
        return m ? Number(m[0]) : null;
    }

    function retagOptions(ctn, rec, inst) {
        const vr = inst.v && rec && rec.versions ? rec.versions[inst.v] : null;
        const ch = vr && vr.choices;
        if (!ch) return [];
        const cur = majorOf(inst.v);
        const nextcloud = NEXTCLOUD_RE.test(repoKey(ctn.image));
        const blocked = (target) => {      // the version the tag brings; null: follows every release
            const m = target == null ? null : majorOf(target);
            if (rec.db && m !== cur) return "rt.optDbMajor";
            if (nextcloud && (m == null || m - cur > 1)) return "rt.optSkip";
            return null;
        };
        const out = [];
        const add = (group, tag, key, target, vars) => {
            if (!tag || tag === ctn.tag || out.some((o) => o.tag === tag)) return;
            const why = blocked(target);
            out.push({ group, tag, disabled: !!why,
                       text: why ? t(why, { tag }) : key ? t(key, { ...vars, tag }) : tag });
        };
        add("rt.grp.pin", ch.pin, "rt.optPin", inst.v);
        (ch.lines || []).forEach((l) => add("rt.grp.lines", l.tag, "rt.optLine", l.newest, { v: l.newest }));
        LEVELS.forEach(([lvl]) => (ch.newer || []).forEach((x) => {
            if (x.level === lvl) add("rt.grp." + lvl, x.tag, null, x.tag);
        }));
        (ch.floating || []).forEach((f) => add("rt.grp.floating", f, null, null));
        return out;
    }

    // "Newest version" column. Returns { html, news }:
    //   news = an actionable newer version exists (patch/minor, or a major
    //          on a non-database image). A database major alone is grey info.
    // Chips are compared with the INSTALLED version; filled = the configured
    // tag delivers it via the Update button, outlined = needs a new tag. An
    // outlined chip whose tag the selection field offers (pick) is a button
    // that opens "Change version" with that tag.
    function versionCell(ctn, rec, inst, radarOk, pick) {
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
            const tag = !info && !can && pick && pick.has(cmp[lvl]) ? cmp[lvl] : null;
            const title = [info ? t("ver.dbMajor") : t("ver.levelTip", { level: label }),
                           t(can ? "ver.reachYes" : "ver.reachNo", { tag: ctn.tag })]
                .concat(tag ? [t("ver.clickRetag", { tag })] : []).join("\n");
            const text = `${label} <span class="dm-ver">${esc(cmp[lvl])}</span>`;
            chips.push(tag
                ? `<button type="button" class="dm-chip ${cls}" title="${esc(title)}" ` +
                  `data-retag="${esc(ctn.name)}" data-tag="${esc(tag)}">${text}</button>`
                : `<span class="dm-chip ${cls}" title="${esc(title)}">${text}</span>`);
        });
        if (!chips.length) {
            return { html: `<span class="dm-uptodate" title="${esc(t("ver.upToDateTip"))}">✓` +
                           `${inst.v ? " " + esc(inst.v) : ""}</span>${stale}`, news: false };
        }
        return { html: `<span class="dm-chips">${chips.join("")}</span>${stale}`, news };
    }

    // Failed builds from the history: image ref -> the newest update of that
    // image, kept only if it failed. Its digests mark the same build when
    // Diun reports it again (see sameFailedBuild).
    let failedBuilds = new Map();
    const rowFailed = new Map();        // container name -> failed update op
    const siblings = new Map();         // container name -> others an update recreates with it
    const retagInfo = new Map();        // container name -> { ctn, inst, options } for "Change version"
    let lastRender = null;

    function failedBuildsOf(ops) {
        const map = new Map();
        const seen = new Set();
        (ops || []).forEach((o) => {     // newest first
            if (o.kind !== "update" || !o.image || o.status === "running") return;
            const tg = tagSplit(o.image);
            const k = verKey(tg.repo, tg.tag);
            if (seen.has(k)) return;
            seen.add(k);
            if (o.status === "failed") map.set(k, o);
        });
        return map;
    }

    function sameFailedBuild(ctn, ev) {
        const fb = failedBuilds.get(verKey(ctn.image, ctn.tag));
        return fb && ev && ev.digest && (fb.new_digests || []).indexOf(ev.digest) > -1 ? fb : null;
    }

    // The history arrives after the table; draw the rows again if it changes
    // which builds failed.
    function refreshFailedBuilds() {
        const next = failedBuildsOf(backups && backups.ops);
        const sig = (m) => JSON.stringify([...m.entries()].map(([k, o]) => [k, o.id]));
        if (sig(next) === sig(failedBuilds)) return;
        failedBuilds = next;
        if (lastRender) render(...lastRender);
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
        const options = ctn.managed ? retagOptions(ctn, rec, inst) : [];
        const pick = new Set(options.filter((o) => !o.disabled).map((o) => o.tag));
        if (pick.size) retagInfo.set(ctn.name, { ctn, inst, options });
        const ver = versionCell(ctn, rec, inst, radarOk, pick);
        if ((kind === "ok" || kind === "unknown") && ver.news) kind = "newver";
        // Without a known installed version "up to date" would be a guess.
        if (kind === "ok" && radarOk && rec && !inst.v) kind = "unknown";
        const failedBuild = kind === "update" ? sameFailedBuild(ctn, ev) : null;
        rowFailed.set(ctn.name, failedBuild);
        const note = failedBuild
            ? `<span class="dm-row-note">${esc(t("row.failedBuild", { date: fmtDate(new Date(failedBuild.ts)) }))}</span>` : "";

        const imgCell = ev && ev.hub_link
            ? `<a href="${esc(ev.hub_link)}" target="_blank" rel="noopener">${esc(ctn.image)}</a>`
            : esc(ctn.image);

        const actions = [];
        if (kind === "update" || kind === "pulled") {
            if (ctn.managed) {
                actions.push(textButton("arrow-circle-up", t(kind === "pulled" ? "btn.rollout" : "btn.update"),
                                        `data-update="${esc(ctn.name)}" data-kind="${kind}"`));
            } else {
                actions.push(`<span class="dm-muted dm-small" title="${esc(t("row.manualOnlyTip"))}">${esc(t("row.manualOnly"))}</span>`);
            }
        }
        if (pick.size) actions.push(iconButton("exchange-alt", t("btn.retag"), `data-retag="${esc(ctn.name)}"`));
        if (kind === "update") {
            actions.push(iconButton("check", t("btn.dismiss"), `data-key="${esc(evKey(ev))}"`));
        }

        const sib = siblings.get(ctn.name) || [];
        const together = sib.length
            ? `<span class="dm-row-note">${esc(t("row.together", {
                names: sib.slice(0, 3).join(", ") + (sib.length > 3 ? " +" + (sib.length - 3) : "") }))}</span>`
            : "";

        const L = (key) => esc(t(key));
        return { kind, html:
            `<td data-label="${L("col.container")}"><span class="dm-cell"><span class="dm-name">${esc(ctn.name)}</span>${together}</span></td>` +
            `<td data-label="${L("col.image")}" class="dm-img"><span class="dm-cell az-mono">${imgCell}</span></td>` +
            `<td data-label="${L("col.configured")}"><span class="dm-cell"><span class="dm-ver dm-cfg" title="${L("cfg.tip")}">${esc(ctn.tag)}</span>${tagNowNote(ctn, rec, inst)}</span></td>` +
            `<td class="az-nowrap" data-label="${L("col.installed")}"><span class="dm-cell">${installedCell(ctn, rec, inst)}</span></td>` +
            `<td data-label="${L("col.new")}"><span class="dm-cell">${ver.html}</span></td>` +
            `<td data-label="${L("col.status")}"><span class="dm-cell">${badge(kind)}${note}</span></td>` +
            `<td class="az-cell-actions"><span class="dm-cell">${actions.join("")}</span></td>`
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
        lastRender = [containers, eventsMap, radarDoc];
        rowFailed.clear();
        retagInfo.clear();
        // An update recreates every service of the project with the same image.
        siblings.clear();
        const groups = new Map();
        containers.forEach((c) => {
            if (!c.project) return;
            const k = c.project + "|" + verKey(c.image, c.tag);
            if (!groups.has(k)) groups.set(k, []);
            groups.get(k).push(c.name);
        });
        groups.forEach((names) => names.forEach((n) => {
            const others = names.filter((x) => x !== n).sort();
            if (others.length) siblings.set(n, others);
        }));
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
        // The row's "Change version" and the outlined chips (with their tag).
        rows.querySelectorAll("button[data-retag]").forEach((btn) => {
            btn.addEventListener("click", () => startRetag(btn.dataset.retag, btn.dataset.tag || ""));
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
                ? textButton(p.action[0] === "start" ? "play" : "plus", t(p.action[2]),
                             `data-verb="${esc(p.action[0])}" data-target="${esc(p.action[1])}"`)
                : "";
            li.innerHTML =
                `<div class="dm-dep-main">` +
                  `<span class="dm-dep-name">${esc(t("dep." + p.id))}</span> ` +
                  label("warning", "exclamation-triangle", p.state) +
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
            b.classList.toggle("az-btn--danger", !!opts.danger);
            b.classList.toggle("az-btn--primary", !opts.danger);
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
            this.reason(null);
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

        // Most likely cause of a failed update or rollback (@@RESULT reason),
        // above the log; null hides it.
        reason(r) {
            const el = $("dm-dialog-reason");
            if (!r) {
                el.hidden = true;
                el.innerHTML = "";
                return;
            }
            const title = r.container ? t("upd.reasonTitle", { name: r.container }) : t("upd.reasonComposeTitle");
            el.innerHTML = `<span class="az-alert__icon">${icon("exclamation-circle")}</span>` +
                           `<p class="az-alert__title">${esc(title)}</p>` +
                           `<p class="az-alert__body dm-reason-line">${esc(reasonText(r))}</p>`;
            el.hidden = false;
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
            // What was chosen for this run is fixed now (tick boxes, the version field).
            $("dm-dialog-body").querySelectorAll("input, select").forEach((x) => { x.disabled = true; });
            this.reason(null);
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
    // The plan of an update; with rt (`retag --dry-run`) the plan of a version
    // change, whose unknown jump comes from a tag that follows every release.
    function planBody(plan, failed, rt) {
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
        let html = "";
        // Unknown version + a tag that follows every release, or a new major
        // version: say so first; the confirm button waits for every tick box
        // (.dm-gate, see startUpdate). A warning and its tick box stay
        // together (.dm-gate-group).
        const tag = tagSplit(plan.image).tag;
        const gate = (warning, id, confirm) =>
            `<div class="dm-gate-group"><p class="dm-callout dm-callout-warn">${warning}</p>` +
            `<label class="dm-check"><input type="checkbox" class="dm-gate" id="${id}"> ${esc(confirm)}</label></div>`;
        if (plan.jump_unknown && rt) {
            html += gate(esc(t("rt.floating", { tag: rt.tag })), "dm-jump-check", t("rt.floatingConfirm", { tag: rt.tag }));
        } else if (plan.jump_unknown) {
            html += gate(esc(t("upd.jumpUnknown", { tag })) +
                         (plan.target_version ? " " + esc(t("upd.jumpTarget", { tag, v: plan.target_version })) : ""),
                         "dm-jump-check", t("upd.jumpConfirm"));
        }
        // The update brings a new major version (installed and target known).
        if (plan.jump === "major") {
            html += gate(esc(t("upd.majorJump", { from: plan.installed_version, to: plan.target_version })),
                         "dm-major-check", t("upd.majorConfirm", { to: plan.target_version }));
        }
        // The registry offers exactly the build whose update failed before.
        if (failed) {
            const cause = failed.reason ? reasonText(failed.reason) : "";
            html += gate(esc(t("upd.failedBuild", { date: fmtDate(new Date(failed.ts)) })) +
                         (cause ? `<span class="dm-reason-line dm-callout-line">${esc(cause)}</span>` : ""),
                         "dm-retry-check", t("upd.retryConfirm"));
        }
        const jobs = plan.jobs || [];
        const COND = { service_healthy: "cond.healthy", service_started: "cond.started",
                       service_completed_successfully: "cond.completed" };
        const deps = (plan.depends_on || []).map((d) =>
            `${code(d.service)} <span class="dm-muted">${esc(COND[d.condition] ? t(COND[d.condition]) : d.condition)}</span>`);
        const watched = (plan.dependents || []).map((d) =>
            (d.containers && d.containers.length ? d.containers : [d.service]).map(code).join(" "));
        html += `<ul class="dm-kv">` +
            li(t("upd.containers"), (plan.containers || []).filter((c) => jobs.indexOf(c) < 0).map(code).join(" ")) +
            (jobs.length ? li(t("upd.jobs"), jobs.map(code).join(" ")) : "") +
            li(t("upd.project"), `${code(plan.project)} <span class="dm-muted">${esc(plan.workdir || "")}</span>`) +
            ("installed_version" in plan
                ? li(t("upd.installed"), plan.installed_version ? code(plan.installed_version) : esc(t("upd.versionUnknown")))
                : "") +
            (plan.target_version
                ? li(t("upd.target"), plan.jump === "same" ? esc(t("upd.targetSame", { v: plan.target_version }))
                    : code(plan.target_version) + (plan.jump ? ` <span class="dm-muted">${esc(LEVEL_NAMES[plan.jump] || "")}</span>` : ""))
                : "") +
            li(t("upd.rollback"), code(repo + ":rollback-…")) +
            li(t("upd.dbs"), dbLines.length ? dbLines.join("<br>") : esc(t("upd.noDb"))) +
            (dumpable && plan.free_bytes != null ? li("", `<span class="dm-muted">${esc(t("upd.free", { free: human(plan.free_bytes) }))}</span>`) : "") +
            (deps.length ? li(t("upd.dependsOn"), deps.join(", ")) : "") +
            (watched.length ? li(t("upd.dependents"), watched.join(" ")) : "") +
            `</ul>`;
        html += `<label class="dm-check"><input type="checkbox" id="dm-dump-check"${dumpable ? " checked" : " disabled"}> ` +
                `${esc(t("upd.dumpCheck"))}</label>`;
        if (plan.config_changed) html += `<p class="dm-callout dm-callout-warn">${esc(t("upd.configChanged"))}</p>`;
        (plan.job_order || []).forEach((o) => {
            html += `<p class="dm-callout dm-callout-warn">${esc(t("upd.jobOrder", { job: o.job, services: o.services.join(", ") }))}</p>`;
        });
        (plan.shared_networks || []).forEach((n) => {
            html += `<p class="dm-callout">${esc(t("upd.sharedNet", { network: n.network, projects: n.projects.join(", ") }))}</p>`;
        });
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
                const label = t(kind === "pulled" ? "btn.rollout" : "btn.update");
                const failed = rowFailed.get(name) || null;
                confirmAndRun({
                    title, desc, body: planBody(plan, failed),
                    confirm: {
                        label,
                        disabled: !!plan.jump_unknown || !!failed || plan.jump === "major",
                        argv: () => {
                            const box = $("dm-dump-check");
                            const dump = box && box.checked && !box.disabled;
                            return [UPDATE_CMD, "update", name].concat(dump ? [] : ["--no-dump"]);
                        },
                    },
                    onDone: (o) => updateOutcome(o, name),
                });
                const gates = [...$("dm-dialog-body").querySelectorAll(".dm-gate")];
                gates.forEach((g) => g.addEventListener("change", () =>
                    dlg.btn("dm-dialog-confirm", label, { visible: true, disabled: !gates.every((x) => x.checked) })));
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
            dlg.reason(r.reason);
            dlg.result(t("upd.failed"), false);
            offerRollback(r.op, name);
            return true;
        }
        if (o.exit === 5) { dlg.result(t("upd.aborted"), false); return true; }
        return false;
    }

    // Text of a failure reason (@@RESULT / history): the log line, otherwise
    // what the container's state tells.
    function reasonText(r) {
        if (r.line) return r.source === "health" ? t("upd.reasonHealth", { line: r.line }) : r.line;
        if (r.oom) return t("upd.reasonOom");
        if (r.state === "running" || r.state === "starting") return t("upd.reasonTimeout");
        if (r.exit_code) return t("upd.reasonExit", { code: r.exit_code });
        return t("upd.reasonNone");
    }

    // Exit 2 and 5 stop a rollback before anything changed: the rollback
    // image is gone, or a file of a tag change was edited since (or its copy
    // is damaged).
    function offerRollback(op, name) {
        dlg.btn("dm-dialog-extra", t("rb.button"), { visible: true });
        dlg.onExtra = () => {
            dlg.run([UPDATE_CMD, "rollback", op]).then((o) => {
                if (!o.ok) dlg.reason((o.result || {}).reason);
                dlg.result(o.ok ? t("rb.ok") : o.noAdmin ? t("dialog.noAdmin")
                    : o.exit === 2 || o.exit === 5 ? t("rb.aborted") : t("rb.failed"), !!o.ok);
                safeLoad();
            });
        };
    }

    function startRollback(op) {
        const vars = { image: op.image, rb: op.rollback, services: (op.services || []).join(", "),
                       files: [...new Set((op.changes || []).map((c) => c.file))].join(", ") };
        confirmAndRun({
            title: t("rb.title", { name: op.container }),
            desc: t(op.kind === "retag" ? "rb.descRetag" : "rb.desc", vars),
            confirm: { label: t("rb.button"), argv: () => [UPDATE_CMD, "rollback", op.id] },
            onDone: (o) => {
                if (o.ok) { dlg.result(t("rb.ok"), true); return true; }
                if (o.exit === 4) {
                    dlg.reason((o.result || {}).reason);
                    dlg.result(t("rb.failed"), false);
                    return true;
                }
                if (o.exit === 2 || o.exit === 5) { dlg.result(t("rb.aborted"), false); return true; }
                return false;
            },
        });
    }

    // --- Change version (stage 2) --------------------------------------------
    // The selection field first; each choice is planned by the helper
    // (`retag … --dry-run`, read-only) and shown as the lines that change plus
    // the plan of an update. Confirming passes the plan's fingerprint, so the
    // helper changes exactly what was shown or nothing (exit 5).
    function retagField(ctn, inst, options) {
        const groups = [];
        options.forEach((o) => {
            let g = groups.find((x) => x.key === o.group);
            if (!g) groups.push(g = { key: o.group, items: [] });
            g.items.push(o);
        });
        return `<div class="az-field">` +
            `<label class="az-field__label" for="dm-retag-select">${esc(t("rt.select"))}</label>` +
            `<select id="dm-retag-select" class="az-input">` +
              `<option value="">${esc(t("rt.optConfigured", { tag: ctn.tag }))}</option>` +
              groups.map((g) => `<optgroup label="${esc(t(g.key))}">` + g.items.map((o) =>
                  `<option value="${esc(o.tag)}"${o.disabled ? " disabled" : ""}>${esc(o.text)}</option>`).join("") +
                  `</optgroup>`).join("") +
            `</select>` +
            `<p class="az-field__help">${esc(t("rt.help", { v: inst.v }))}</p></div>` +
            `<div id="dm-retag-preview" class="dm-retag-preview"></div>`;
    }

    // Every line before and after, with file and line number, as the helper
    // will write it.
    function retagDiff(rt) {
        return `<div class="dm-diff-group"><p class="dm-diff-title">${esc(t("rt.diff"))}</p>` +
            (rt.changes || []).map((c) =>
                `<div class="dm-diff"><div class="dm-diff-file">${esc(c.file)}:${esc(c.line)}` +
                (c.service ? ` <span class="dm-muted">(${esc(c.service)})</span>` : "") + `</div>` +
                `<div class="dm-diff-lines"><div class="dm-diff-old">- ${esc(c.old)}</div>` +
                `<div class="dm-diff-new">+ ${esc(c.new)}</div></div></div>`).join("") +
            `</div>` +
            (rt.variable ? `<p class="dm-callout">${esc(t("rt.variable", { name: rt.variable }))}</p>` : "") +
            (rt.git ? `<p class="dm-callout">${esc(t("rt.git", { path: rt.git }))}</p>` : "");
    }

    function retagReason(plan) {
        const rt = plan.retag || {};
        const key = "reason." + rt.reason;
        const text = t(key, { tag: rt.tag, detail: rt.detail || "", files: (plan.missing || []).join(", ") });
        return text === key ? rt.reason + (rt.detail ? ` (${rt.detail})` : "") : text;
    }

    // preset: the tag of a clicked chip; "" opens the field on the configured tag.
    function startRetag(name, preset) {
        const info = retagInfo.get(name);
        if (!info) return;
        const ticket = ++planSeq;
        const label = t("btn.retag");
        const shown = { tag: null, fp: null };      // the change the preview shows
        const ready = (on) => dlg.btn("dm-dialog-confirm", label, { visible: true, disabled: !on });
        confirmAndRun({
            title: t("rt.title", { name }), desc: t("rt.desc"),
            body: retagField(info.ctn, info.inst, info.options),
            confirm: {
                label, disabled: true,
                argv: () => {
                    const box = $("dm-dump-check");
                    const dump = box && box.checked && !box.disabled;
                    return [UPDATE_CMD, "retag", name, shown.tag, "--expect", shown.fp].concat(dump ? [] : ["--no-dump"]);
                },
            },
            onDone: (o) => retagOutcome(o, name, shown.tag),
        });
        const sel = $("dm-retag-select");
        const box = $("dm-retag-preview");
        let seq = 0;
        const preview = () => {
            const tag = sel.value;
            const mine = ++seq;
            // A plan that arrives after another choice, after the dialog was
            // closed or reopened, or after the run started, is dropped.
            const stale = () => mine !== seq || ticket !== planSeq || !$("dm-dialog").hasAttribute("open") || dlg.running;
            shown.tag = shown.fp = null;
            ready(false);
            box.innerHTML = tag ? `<p class="dm-dialog-note">${esc(t("rt.checking"))}</p>` : "";
            if (!tag) return;
            Promise.resolve().then(() => spawnHelper([UPDATE_CMD, "retag", name, tag, "--dry-run"], "try"))
                .then((out) => {
                    if (stale()) return;
                    let plan;
                    try { plan = JSON.parse(out); } catch (_) { throw new Error("bad plan output"); }
                    const rt = plan.retag || {};
                    if (!rt.possible) {
                        box.innerHTML = `<p class="dm-callout dm-callout-warn">${esc(t("upd.notPossible", { reason: retagReason(plan) }))}</p>`;
                        return;
                    }
                    box.innerHTML = retagDiff(rt) + planBody(plan, null, rt);
                    shown.tag = tag;
                    shown.fp = rt.fingerprint;
                    const gates = [...box.querySelectorAll(".dm-gate")];
                    const check = () => ready(gates.every((g) => g.checked));
                    gates.forEach((g) => g.addEventListener("change", check));
                    check();
                })
                .catch((ex) => {
                    if (stale()) return;
                    box.innerHTML = `<p class="dm-callout dm-callout-warn">${esc(isNoAdmin(ex) ? t("dialog.noAdmin")
                        : t("dialog.error", { msg: (ex && (ex.message || ex.problem)) || ex }))}</p>`;
                });
        };
        sel.addEventListener("change", preview);
        if (preset && info.options.some((o) => o.tag === preset && !o.disabled)) {
            sel.value = preset;
            preview();
        }
    }

    function retagOutcome(o, name, tag) {
        const r = o.result || {};
        if (o.ok) {
            const vers = r.from_version && r.to_version && r.from_version !== r.to_version;
            dlg.result(t(vers ? "rt.okVersions" : "rt.ok", { tag: r.image ? tagSplit(r.image).tag : tag,
                from: r.from_version, to: r.to_version, rb: r.rollback || "" }), true);
            return true;
        }
        if (o.exit === 4 && r.op) {
            dlg.reason(r.reason);
            dlg.result(t("rt.failed"), false);
            offerRollback(r.op, name);
            return true;
        }
        if (o.exit === 5) { dlg.result(t("upd.aborted"), false); return true; }
        return false;
    }

    // --- Backups tab -------------------------------------------------------
    let backups = null;

    let backupsError = "";

    function loadBackups() {
        return Promise.resolve().then(() => spawnHelper([UPDATE_CMD, "backups"], "try"))
            .then((out) => { backups = JSON.parse(out); backupsError = ""; renderBackups(); refreshFailedBuilds(); })
            .catch((ex) => {
                backups = null;
                backupsError = isNoAdmin(ex) ? t("bk.needAdmin")
                    : t("bk.error", { msg: (ex && (ex.message || ex.problem)) || ex });
                renderBackups();
            });
    }

    function opResult(o) {
        if (o.rolled_back) {
            return o.rollback_status === "failed"
                ? label("danger", "exclamation-circle", t("res.rollbackFailed"))
                : label("", "history", t("res.rolledBack"));
        }
        const map = { ok: ["success", "check-circle", "res.ok"], nochange: ["", null, "res.nochange"],
                      failed: ["danger", "exclamation-circle", "res.failed"],
                      running: ["warning", "exclamation-triangle", "res.running"] };
        const [cls, ic, key] = map[o.status] || map.running;
        return label(cls, ic, t(key));
    }

    // The kept output of an update and its rollback (diun-update.py log).
    function showLog(op) {
        const ticket = ++planSeq;
        const stale = () => ticket !== planSeq || !$("dm-dialog").hasAttribute("open");
        dlg.open({ title: t("log.title", { name: op.container || "" }),
                   desc: t("log.desc", { when: fmtDate(new Date(op.ts)), image: op.image || "" }) });
        dlg.result(t("log.loading"), null);
        Promise.resolve().then(() => spawnHelper([UPDATE_CMD, "log", op.id], "try"))
            .then((out) => {
                if (stale()) return;
                const log = $("dm-dialog-log");
                log.textContent = out;
                log.hidden = false;
                dlg.result("", null);
            })
            .catch((ex) => {
                if (stale()) return;
                ex = ex || {};
                dlg.result(isNoAdmin(ex) ? t("dialog.noAdmin") : ex.exit_status === 2 ? t("log.none")
                    : t("dialog.error", { msg: ex.message || ex.problem || ex }), false);
            });
    }

    // Under the result: why the rollback or, otherwise, the update failed.
    function opReason(o) {
        const r = (o.rolled_back && o.rollback_status === "failed" && o.rollback_reason) ||
                  (o.status === "failed" && o.reason) || null;
        if (!r) return "";
        const text = (r.container ? r.container + ": " : "") + reasonText(r);
        return `<span class="dm-op-reason" title="${esc(text)}">${esc(text)}</span>`;
    }

    // "16.14 → 16.15"; older history entries have no versions.
    function opVersions(o) {
        const a = o.from_version, z = o.to_version;
        if (!a && !z) return `<span class="dm-muted">—</span>`;
        if (a === z) return `<span class="dm-ver">${esc(a)}</span>`;
        return `<span class="dm-ver">${esc(a || "?")} <span class="dm-arrow">→</span> ${esc(z || "?")}</span>`;
    }

    function cleanupMark() {
        return " " + label("warning", "exclamation-triangle", t("cl.mark"), t("cl.markTip"));
    }

    // Rules, daily timer and "what would be deleted now" (from `backups`),
    // the timer state from `diun-deps.sh check`.
    function renderCleanup() {
        const c = backups && backups.cleanup;
        $("dm-cleanup").hidden = !c;
        if (!c) return;
        const rules = $("dm-cleanup-rules");
        if (c.error) {
            rules.innerHTML = `<span class="dm-danger-text">${esc(t("cl.error", { msg: c.error }))}</span>`;
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
            timer.insertAdjacentHTML("beforeend", " " + textButton("plus", t("action.setup"), 'data-setup="cleanup"'));
            timer.querySelector("button[data-setup]").addEventListener("click", () => openInstallDialog("install", "cleanup"));
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
        const summary = $("dm-bk-summary");
        summary.classList.toggle("dm-danger-text", !backups && !!backupsError);
        if (backups) {
            summary.textContent = t("bk.summary", {
                dumps: t("bk.dumpsN", { n: b.dumps.length, size: human(b.dump_bytes) }),
                images: t("bk.imagesN", { n: b.images.length }),
                free: human(b.free_bytes), dir: b.dir,
            });
        } else {
            summary.textContent = backupsError;
        }

        const ops = $("dm-ops");
        ops.innerHTML = b.ops.map((o, i) =>
            `<tr>` +
            `<td class="az-nowrap" data-label="${L("col.when")}"><span class="dm-cell">${esc(fmtDate(new Date(o.ts)))}</span></td>` +
            `<td class="az-nowrap" data-label="${L("col.container")}"><span class="dm-cell">${esc(o.container || "")}</span></td>` +
            `<td data-label="${L("col.image")}"><span class="dm-cell"><span class="az-mono">${esc(o.image || "")}</span>` +
              (o.kind === "retag" ? `<span class="dm-row-note">${esc(t("op.retag", { from: o.from_tag, to: o.to_tag }))}</span>` : "") +
            `</span></td>` +
            `<td data-label="${L("col.version")}"><span class="dm-cell">${opVersions(o)}</span></td>` +
            `<td data-label="${L("col.result")}"><span class="dm-cell">${opResult(o)}${opReason(o)}</span></td>` +
            `<td data-label="${L("col.dumps")}"><span class="dm-cell">${(o.dumps || []).length}</span></td>` +
            `<td class="az-cell-actions"><span class="dm-cell">` +
              (o.can_rollback ? textButton("history", t("rb.button"), `data-op="${i}"`) : "") +
              (o.log || o.rollback_log ? iconButton("angle-right", t("btn.showLog"), `data-log="${i}"`) : "") +
            `</span></td></tr>`).join("");
        $("dm-ops-empty").hidden = b.ops.length > 0;
        $("dm-ops-table").hidden = b.ops.length === 0;
        ops.querySelectorAll("button[data-op]").forEach((btn) => {
            btn.addEventListener("click", () => startRollback(b.ops[Number(btn.dataset.op)]));
        });
        ops.querySelectorAll("button[data-log]").forEach((btn) => {
            btn.addEventListener("click", () => showLog(b.ops[Number(btn.dataset.log)]));
        });

        const dumps = $("dm-dumps");
        dumps.innerHTML = b.dumps.map((d, i) =>
            `<tr>` +
            `<td class="az-nowrap" data-label="${L("col.created")}"><span class="dm-cell">${esc(fmtDate(new Date(d.mtime * 1000)))}</span></td>` +
            `<td class="az-nowrap" data-label="${L("col.project")}"><span class="dm-cell">${esc(d.project)}</span></td>` +
            `<td class="az-nowrap" data-label="${L("col.container")}"><span class="dm-cell">${esc(d.container)}</span></td>` +
            `<td data-label="${L("col.file")}"><span class="dm-cell"><span class="az-mono dm-digest">${esc(d.path.split("/").pop())}</span>` +
              `${doomed.has(d.path) ? cleanupMark() : ""}</span></td>` +
            `<td class="az-nowrap" data-label="${L("col.size")}"><span class="dm-cell">${esc(human(d.size))}</span></td>` +
            `<td class="az-cell-actions"><span class="dm-cell">` +
              iconButton("trash", t("btn.delete"), `data-dump="${i}"`) +
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
            `<td data-label="${L("col.image")}"><span class="dm-cell az-mono">${esc(im.ref)}</span></td>` +
            `<td class="az-nowrap" data-label="${L("col.created")}"><span class="dm-cell">${esc(String(im.created || "").slice(0, 16))}</span></td>` +
            `<td class="az-nowrap" data-label="${L("col.size")}"><span class="dm-cell">${esc(im.size)}</span></td>` +
            `<td data-label="${L("col.status")}"><span class="dm-cell">` +
              (im.in_use ? label("", null, t("img.inUse"), t("img.inUseTip"))
                         : `<span class="dm-muted">${L("img.free")}</span>${doomed.has(im.ref) ? cleanupMark() : ""}`) +
            `</span></td>` +
            `<td class="az-cell-actions"><span class="dm-cell">` +
              iconButton("trash", t("btn.delete"), `data-img="${i}"` + (im.in_use ? " disabled" : ""),
                         im.in_use ? t("img.inUseTip") : "") +
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

    // WAI-ARIA tabs: only the tab shown is reachable with Tab, the arrow keys
    // move between the tabs, Home and End go to the first and the last.
    // Switching only shows another panel; it loads nothing, and "Refresh"
    // keeps the tab that is shown.
    const tabButtons = () => Array.from(document.querySelectorAll('.az-tabs [role="tab"]'));

    function showTab(name, focus) {
        currentTab = name;
        tabButtons().forEach((b) => {
            const on = b.dataset.tab === name;
            b.setAttribute("aria-selected", on ? "true" : "false");
            if (on) b.removeAttribute("tabindex"); else b.setAttribute("tabindex", "-1");
            $(b.getAttribute("aria-controls")).hidden = !on;
            if (on && focus) b.focus();
        });
    }

    function onTabKey(ev) {
        const all = tabButtons();
        const k = all.indexOf(document.activeElement);
        if (k < 0) return;
        let next = null;
        if (ev.key === "ArrowRight") next = all[(k + 1) % all.length];
        else if (ev.key === "ArrowLeft") next = all[(k - 1 + all.length) % all.length];
        else if (ev.key === "Home") next = all[0];
        else if (ev.key === "End") next = all[all.length - 1];
        if (!next) return;
        ev.preventDefault();
        showTab(next.dataset.tab, true);
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
        el.innerHTML = `<span class="az-alert__icon">${icon(isError ? "exclamation-circle" : "info-circle")}</span>` +
                       `<p class="az-alert__body">${esc(msg)}</p>`;
        el.classList.toggle("az-alert--danger", !!isError);
        el.hidden = false;
    }

    // --- Signature -----------------------------------------------------------
    // Foot of the page: product name with version, then the server's host name.
    function renderSignature() {
        $("dm-sig-version").textContent = t("app.title") + " monitor " + VERSION;
        Promise.resolve().then(() => cockpit.file("/etc/hostname").read())
            .then((text) => {
                const host = String(text || "").trim().split(/\s+/)[0];
                $("dm-sig-host").textContent = host;
                $("dm-sig-host").hidden = !host;
            })
            .catch(() => { /* no host name: the signature shows the product only */ });
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
        // Both tabs are loaded together, so switching tabs loads nothing.
        return jobs.then(loadBackups);
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
        btn.setAttribute("aria-busy", on ? "true" : "false");
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
    tabButtons().forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab, false)));
    document.querySelector(".az-tabs").addEventListener("keydown", onTabKey);
    renderSignature();
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

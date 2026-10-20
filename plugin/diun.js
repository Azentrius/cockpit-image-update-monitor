// SPDX-License-Identifier: Apache-2.0
/* Image Update Monitor, a Cockpit add-on
 * Lists running containers (their installed version + current digest) and
 * annotates each with independent signals:
 *   - inventory -> cockpit.spawn(diun-inventory.py)  configured tag, exact installed
 *                                                    version (tag / image variable),
 *                                                    compose project, "pulled, not rolled out"
 *   - Diun log  -> events.jsonl                      same tag rebuilt (new digest)
 *   - Radar     -> versions.json                     installed version via label /
 *                                                    digest, newer version tags
 *                                                    (patch / minor / major)
 *                  releases.json                     release notes from GitHub, read
 *                                                    when a dialog shows a plan
 *   - deps      -> cockpit.spawn(diun-deps.sh check) Docker, Diun, skopeo, radar
 *   - updates   -> cockpit.spawn(diun-update.py …)   plan / update / rollback /
 *                                                    backups / delete
 *
 * The radar file is written every 6h by diun-radar.timer
 * (/usr/lib/cockpit-image-updates/diun-versions.py). Everything Cockpit executes
 * as root lives in /usr/lib/cockpit-image-updates, never in /var/lib/diun, which
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
 * The page's scripts, in the order index.html loads them; they share
 * window.ImageUpdatesPage (common.js opens it):
 *   common.js    paths, texts, icons, formatting, labels, alerts, buttons
 *   dialog.js    the one action dialog, its run and live output
 *   notes.js     the release notes in a plan (stage 3)
 *   update.js    update, update in two steps, rollback (stage 1)
 *   retag.js     "Change version" (stage 2)
 *   overview.js  the Updates tab: rows, columns, status, meta line
 *   backups.js   the Backups tab: history, dumps, rollback images, cleanup
 *   diun.js      the version, requirements, "Run radar now", tabs, the places
 *                of the status links, loading, and the start of the page
 * Each takes the building blocks of common.js and dialog.js when it loads and
 * calls the others through the namespace when it runs.
 *
 * No build step, no framework, no external requests; same model as cronctl.
 */

(function () {
    "use strict";
    // The one place for the version (SemVer). index.html loads every script and
    // stylesheet of the add-on with ?v=VERSION; tools/check-version.sh keeps
    // them in step.
    const VERSION = "0.24.0";

    // --- All parts loaded? -------------------------------------------------
    // Each script of the page, by a name it gives the namespace. Cockpit's
    // bridge keeps the list of a package's files it read first in the session;
    // a file that an update of the add-on brings new answers 404 until the
    // bridge reads the packages again. The shell asks for that when it starts
    // (ReloadHint), but loads this page at the same time, so after an update
    // and a reload of the browser the page can miss a script (Cockpit 314, CI,
    // 2026-10-20). Then the page asks the bridge to read the packages again
    // and loads itself once more; if a part is still missing, it says so.
    const PARTS = { "common.js": "t", "dialog.js": "confirmAndRun", "notes.js": "showNotes",
                    "update.js": "startUpdate", "retag.js": "startRetag", "overview.js": "render",
                    "backups.js": "loadBackups" };
    const RELOADED = "image-updates:reloaded:" + VERSION;
    const P = window.ImageUpdatesPage;
    const missing = Object.keys(PARTS).filter((f) => !P || typeof P[PARTS[f]] !== "function");
    if (missing.length) {
        reloadParts(missing);
        return;
    }
    try { window.sessionStorage.removeItem(RELOADED); } catch (_) { /* no storage */ }
    const { EVENTS_PATH, DISMISSED_PATH, VERSIONS_PATH, INVENTORY_CMD, DEPS_CMD, UPDATE_CMD, FILE_OPTS,
            LANG, t, icon, $, tagSplit, esc, rich, spawnHelper, isNoAdmin, label, alertInner,
            textButton, dlg, openInstallDialog } = P;

    function reloadParts(files) {
        let again = false;
        try {
            again = window.sessionStorage.getItem(RELOADED) === "1";
            window.sessionStorage.setItem(RELOADED, "1");
        } catch (_) { /* no storage: one more load at most per load of the page */ }
        if (!again) {
            Promise.resolve()
                .then(() => cockpit.dbus(null, { bus: "internal" }).call("/packages", "cockpit.Packages", "Reload", []))
                .catch(() => null)
                .then(() => window.location.reload());
            return;
        }
        // Still missing after that: say so, in the texts of i18n.js (common.js may be the one missing).
        const texts = window.ImageUpdatesI18n || {};
        const lang = /^de([-_]|$)/i.test(String(cockpit.language || "")) ? "de" : "en";
        const say = (key) => ((texts[lang] || {})[key] || (texts.en || {})[key] || key).replace("{files}", files.join(", "));
        const status = document.getElementById("dm-status");
        status.textContent = say("status.partsTitle") + " " + say("status.parts");
        status.className = "az-alert az-alert--danger dm-status";
        status.setAttribute("role", "alert");
        status.hidden = false;
        document.getElementById("dm-loading").hidden = true;
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

    let dismissed = {};
    const eventsFile = cockpit.file(EVENTS_PATH, FILE_OPTS);
    const dismissedFile = cockpit.file(DISMISSED_PATH, { ...FILE_OPTS, syntax: JSON });
    const versionsFile = cockpit.file(VERSIONS_PATH, { ...FILE_OPTS, syntax: JSON });

    // --- Requirements (Docker, Diun, skopeo, radar) -------------------------
    function checkDeps() {
        return spawnHelper([DEPS_CMD, "check"], "try")
            .then((out) => { try { return JSON.parse(out); } catch (_) { return null; } })
            .catch(() => null);     // helper not deployed -> panel stays hidden
    }

    // Turns the check result into the list of problems the panel shows.
    // action = [verb, target] the helper accepts, or null with a hint instead.
    // Without Docker nothing works: an error; everything else is a warning.
    function depProblems(d) {
        const out = [];
        if (!d) return out;
        const dk = d.docker || {}, di = d.diun || {}, sk = d.skopeo || {}, ra = d.radar || {};
        if (dk.state !== "ok") {
            out.push({ id: "docker", error: true, state: t(dk.state === "no-cli" ? "state.missing" : "state.unreachable"),
                       hint: t("dep.docker.hint") });
        }
        if (di.state === "missing") {
            out.push({ id: "diun", state: t("state.missing"),
                       action: di.can_install ? ["install", "diun"] : null,
                       hint: di.can_install ? "" : (dk.state === "ok" ? t("dep.diun.noCompose") : t("dep.docker.hint")) });
        } else if (di.state === "stopped") {
            out.push({ id: "diun", state: t("state.stopped"),
                       action: di.can_start ? ["start", "diun"] : null,
                       hint: di.can_start ? "" : t("dep.diun.external") });
        } else if (di.state === "running" && !di.events) {
            out.push({ id: "diun", state: t("state.noEvents"), hint: t("dep.diun.noEvents") });
        }
        if (sk.state !== "ok") {
            out.push({ id: "skopeo", state: t("state.missing"),
                       action: sk.can_install ? ["install", "skopeo"] : null,
                       hint: sk.can_install ? "" : t("dep.skopeo.noPm") });
        }
        if (ra.state !== "active") {
            out.push({ id: "radar", state: t(ra.state === "inactive" ? "state.inactive" : "state.missing"),
                       action: ra.can_install ? ["install", "radar"] : null,
                       hint: ra.can_install ? "" : t("dep.radar.noScript") });
        }
        return out;
    }

    // The panel is an error when Docker is the problem, a warning otherwise.
    function depsLevel(error) {
        const panel = $("dm-deps");
        panel.classList.toggle("az-alert--danger", error);
        panel.classList.toggle("az-alert--warning", !error);
        $("dm-deps-icon").innerHTML = icon(error ? "exclamation-circle" : "exclamation-triangle");
    }

    function renderDeps(d) {
        const panel = $("dm-deps");
        const list = $("dm-deps-list");
        list.innerHTML = "";
        if (!d) { panel.hidden = true; return; }
        if (!d.root) {
            // Without admin rights the check can't see Docker; say so once.
            depsLevel(false);
            $("dm-deps-intro").textContent = t("deps.needAdmin");
            panel.hidden = false;
            return;
        }
        const problems = depProblems(d);
        if (!problems.length) { panel.hidden = true; return; }
        depsLevel(problems.some((p) => p.error));
        $("dm-deps-intro").textContent = t("deps.intro");
        problems.forEach((p) => {
            const li = document.createElement("li");
            li.className = "dm-dep";
            const btn = p.action
                ? textButton(p.action[0] === "start" ? "play" : "plus", t("action." + p.action.join(".")),
                             `data-verb="${esc(p.action[0])}" data-target="${esc(p.action[1])}"`)
                : "";
            li.innerHTML =
                `<div class="dm-dep-main">` +
                  `<span class="dm-dep-name">${esc(t("dep." + p.id))}</span> ` +
                  (p.error ? label("danger", "exclamation-circle", p.state) : label("warning", "exclamation-triangle", p.state)) +
                  `<div class="dm-dep-desc">${esc(t("dep." + p.id + ".desc"))}</div>` +
                  (p.hint ? `<div class="dm-dep-hint">${rich(p.hint)}</div>` : "") +
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
                failed = isNoAdmin(ex) ? ["status.noAdminTitle", "dialog.noAdmin"]
                    : ex.exit_status === 2 ? ["radar.notSetUpTitle", "radar.notSetUp"] : ["radar.failedTitle", "radar.failed"];
            })
            .finally(() => {
                radarRunning = false;
                // The run is over, whatever the last check said.
                if (lastDeps && lastDeps.radar) lastDeps.radar.running = false;
                renderRadarButton();
                if (failed) showStatus(t(failed[0]), t(failed[1]), true); else safeLoad();
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
        chooseTab(next.dataset.tab, true);
    }

    // A tab chosen on the page goes into the location as well (unless it is
    // there already), so that a later link to the place shown before is a
    // change of the location again.
    function chooseTab(name, focus) {
        showTab(name, focus);
        if (locationPath()[0] !== name && cockpit.location && typeof cockpit.location.go === "function") {
            cockpit.location.go([name]);
        }
    }

    // --- Places: where the Status Board's links lead ------------------------
    // The add-on's status links each item to a place on this page (LINKS in
    // diunstatus.py): a tab, then a part of it.
    //   #/updates                                    the tab (the radar's item)
    //   #/updates/update, …/rollout, …/new-versions  its first row with that status
    //   #/backups/history                            the update history
    // Cockpit keeps a loaded page and, when the board jumps to it again, only
    // changes its location (locationchanged). So every location shows its tab
    // at once and scrolls to its part once, when the data is there and the page
    // is shown. No place: the first tab; a path that is no place changes nothing.
    const PLACES = { updates: { update: "update", rollout: "pulled", "new-versions": "newver" },
                     backups: { history: "history" } };
    const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
    let place = null;           // the part still to scroll to: {tab, part}

    function locationPath() {
        const path = cockpit.location && cockpit.location.path;
        return Array.isArray(path) ? path : [];
    }

    function followLocation() {
        const [tab, part] = locationPath();
        if (tab === undefined) {
            place = null;
            showTab("updates", false);
            return;
        }
        if (!own(PLACES, tab)) return;
        showTab(tab, false);
        place = own(PLACES[tab], part) ? { tab, part: PLACES[tab][part] } : null;
        scrollToPlace();
    }

    function scrollToPlace() {
        if (!place || cockpit.hidden) return;
        let target;
        if (place.tab === "updates") {
            if (!P.rendered()) return;              // the rows are not there yet
            target = $("dm-rows").querySelector(`tr[data-kind="${place.part}"]`);
        } else {
            const bk = P.backupsState();
            if (!bk) return;                        // the history is not there yet
            target = bk === "ok" ? $("dm-ops-title").closest(".az-card") : $("dm-bk-error");
        }
        place = null;
        if (target && typeof target.scrollIntoView === "function") target.scrollIntoView({ block: "start" });
    }

    // --- State -------------------------------------------------------------
    function dismiss(key) {
        dismissed[key] = true;
        dismissedFile.replace(dismissed)
            .catch((err) => showStatus(t("status.saveErrorTitle"), t("status.saveError", { msg: err.message || err }), true))
            .finally(safeLoad);
    }
    // Message for the whole page: a title that says what happened, the text
    // how to fix it; an error is announced at once (role="alert").
    function showStatus(title, body, isError) {
        const el = $("dm-status");
        el.innerHTML = alertInner(isError ? "danger" : "info", title, rich(body));
        el.classList.toggle("az-alert--danger", !!isError);
        el.setAttribute("role", isError ? "alert" : "status");
        el.hidden = false;
    }

    // --- Signature -----------------------------------------------------------
    // Foot of the page: product name with version, then the server's host name.
    function renderSignature() {
        $("dm-sig-version").textContent = "Image Update Monitor " + VERSION;
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
            const events = P.parseEvents(eventsContent);
            if (invOut === null) {
                // Inventory unavailable (no Docker access / no admin rights):
                // fall back to a Diun-events-only view.
                showStatus(t("status.noInventoryTitle"), t("status.noInventory"), false);
                const evContainers = Array.from(events.byTag.values()).map((e) => {
                    const tg = tagSplit(e.image);
                    return { name: "—", image: tg.repo, tag: tg.tag, digest: "" };
                });
                P.render(evContainers, events, radarDoc, true);
                return;
            }
            $("dm-status").hidden = true;
            P.render(P.parseInventory(invOut), events, radarDoc);
        }).catch((err) => {
            showStatus(t("status.loadErrorTitle"), t("status.loadError", { msg: err.message || err }), true);
        });
        // Both tabs are loaded together, so switching tabs loads nothing.
        return jobs.then(() => P.loadBackups());
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
            showStatus(t("status.loadErrorTitle"), t("status.loadError", { msg: (err && err.message) || err }), true);
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

    Object.assign(P, { safeLoad, scrollToPlace, showStatus, dismiss,
                       isDismissed: (key) => !!dismissed[key], deps: () => lastDeps });

    // --- Init --------------------------------------------------------------
    applyStaticTexts();
    $("dm-refresh").addEventListener("click", () => safeLoad());
    $("dm-radar").addEventListener("click", runRadar);
    $("dm-cleanup-btn").addEventListener("click", () => P.startCleanup());
    tabButtons().forEach((b) => b.addEventListener("click", () => chooseTab(b.dataset.tab, false)));
    document.querySelector(".az-tabs").addEventListener("keydown", onTabKey);
    // cockpit.js has both events; the tests' stand-ins for it may leave them out.
    if (typeof cockpit.addEventListener === "function") {
        cockpit.addEventListener("locationchanged", followLocation);
        cockpit.addEventListener("visibilitychange", scrollToPlace);
    }
    followLocation();
    $("dm-dialog-confirm").addEventListener("click", () => {
        if (!dlg.running && dlg.onConfirm) dlg.onConfirm();
    });
    $("dm-dialog-extra").addEventListener("click", () => {
        if (!dlg.running && dlg.onExtra) dlg.onExtra();
    });
    $("dm-dialog-cancel").addEventListener("click", () => dlg.close());
    $("dm-dialog").addEventListener("cancel", (e) => { if (dlg.running) e.preventDefault(); });

    // Cockpit loads this page hidden right after the login, for the status
    // icon in its menu (menu-status.js). Everything that runs or reads on the
    // server starts only once the page is shown.
    function start() {
        renderSignature();
        reloadOnChange(eventsFile);
        reloadOnChange(versionsFile);   // new radar run -> table updates itself
        safeLoad();
    }
    const menu = window.ImageUpdatesMenuStatus;
    if (menu) menu.shown.then(start);
    else start();

    cockpit.transport.wait(function () {});

})();

// SPDX-License-Identifier: Apache-2.0
/* Image Update Monitor, a Cockpit add-on: the building blocks every script
 * of the page uses (paths, texts, icons, formatting, labels, alerts, buttons,
 * the calls of the helpers). index.html loads it first: it opens
 * window.ImageUpdatesPage, the namespace the page's scripts share. Each
 * script takes the building blocks of common.js and dialog.js when it loads
 * and calls the others through the namespace when it runs (diun.js starts
 * the page once all are loaded). */
(function () {
    "use strict";
    const P = window.ImageUpdatesPage = {};

    // --- Configuration -----------------------------------------------------
    const LIB = "/usr/lib/cockpit-image-updates/";
    const EVENTS_PATH = "/var/lib/diun/events.jsonl";
    const DISMISSED_PATH = "/var/lib/diun/dismissed.json";
    const VERSIONS_PATH = "/var/lib/diun-radar/versions.json";
    const RELEASES_PATH = "/var/lib/diun-radar/releases.json";
    const INVENTORY_CMD = LIB + "diun-inventory.py";
    const DEPS_CMD = LIB + "diun-deps.sh";
    const UPDATE_CMD = LIB + "diun-update.py";
    const FILE_OPTS = { superuser: "try" };

    // --- i18n --------------------------------------------------------------
    // Plural entries: { one, other }, picked by vars.n.
    // The texts in English and German: plugin/i18n.js (window.ImageUpdatesI18n).
    const I18N = window.ImageUpdatesI18n;
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

    const $ = (id) => document.getElementById(id);

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
    // A translated text as HTML: `…` marks a path or a command (<code>).
    function rich(s) {
        return esc(s).replace(/`([^`]+)`/g, "<code>$1</code>");
    }
    // Sizes on a 1024 base with two decimals: "1,67 GiB".
    function human(n) {
        if (n == null || isNaN(n)) return "?";
        const units = ["B", "KiB", "MiB", "GiB", "TiB"];
        let i = 0;
        while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
        return (i === 0 ? n.toFixed(0) : n.toFixed(2)).replace(".", LANG === "de" ? "," : ".") + " " + units[i];
    }
    function fmtDay(iso) {
        const d = new Date(iso || "");
        return isNaN(d) ? "?" : d.toLocaleDateString(LOCALE, { dateStyle: "medium" });
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
    // "unit (task 2415, started 21 min ago)" from a CI job container's name
    // (FORGEJO-ACTIONS-TASK-2415_WORKFLOW-…_JOB-unit); other names as they are.
    function ciJobText(name, started) {
        const m = /^(?:FORGEJO|GITEA)-ACTIONS-TASK-(\d+)_.*?(?:_JOB-(.+))?$/i.exec(name || "");
        if (!m) return name;
        const a = started ? ago(started) : null;
        const job = m[2] || name;
        return a ? t("ci.job", { job, task: m[1], ago: a.text }) : `${job} (${m[1]})`;
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

    // Label (az-label): colour, icon and word say the same; gold is never a
    // status. kind "" = without judgement.
    function label(kind, iconName, text, title) {
        return `<span class="az-label${kind ? " az-label--" + kind : ""}"${title ? ` title="${esc(title)}"` : ""}>` +
               `${iconName ? icon(iconName) : ""}${esc(text)}</span>`;
    }
    // Alert (az-alert): a note with info-circle, a warning with the triangle,
    // an error with the circle and role="alert". body is HTML.
    const ALERT_ICON = { info: "info-circle", warning: "exclamation-triangle", danger: "exclamation-circle" };
    function alertInner(kind, title, body) {
        return `<span class="az-alert__icon">${icon(ALERT_ICON[kind])}</span>` +
               (title ? `<p class="az-alert__title">${esc(title)}</p>` : "") +
               `<p class="az-alert__body">${body}</p>`;
    }
    function alertBox(kind, title, body) {
        return `<div class="az-alert${kind === "info" ? "" : " az-alert--" + kind}" role="${kind === "danger" ? "alert" : "status"}">` +
               alertInner(kind, title, body) + `</div>`;
    }
    // An error in running text: never colour alone, always the icon.
    function errorText(text) {
        return `<span class="dm-error">${icon("exclamation-circle")}${esc(text)}</span>`;
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
    // The place of an icon button a row does not have: as large as the button
    // and invisible, so the buttons of all rows line up in the right-aligned
    // actions cell. Gone on a phone, where the rows are blocks.
    function iconSlot(button) {
        return button || `<span class="az-btn az-btn--plain az-btn--sm dm-slot" aria-hidden="true"></span>`;
    }

    const LEVELS = [["patch", "Patch"], ["minor", "Minor"], ["major", "Major"]];
    const LEVEL_NAMES = { patch: "Patch", minor: "Minor", major: "Major" };
    // The engines a database major runs for (stage 5); an operation of 0.23.0 or
    // 0.24.x names none and is PostgreSQL.
    const ENGINES = { postgres: "PostgreSQL", mariadb: "MariaDB" };
    const engineName = (e) => ENGINES[e || "postgres"] || e;

    Object.assign(P, { LIB, EVENTS_PATH, DISMISSED_PATH, VERSIONS_PATH, RELEASES_PATH, INVENTORY_CMD,
                       DEPS_CMD, UPDATE_CMD, FILE_OPTS, LANG, LOCALE, t, icon, $, tagSplit, repoKey,
                       verKey, evKey, shortDigest, esc, rich, human, fmtDay, fmtDate, ago, ciJobText,
                       spawnHelper, isNoAdmin, label, alertInner, alertBox, errorText, textButton,
                       iconButton, iconSlot, LEVELS, LEVEL_NAMES, engineName });
})();

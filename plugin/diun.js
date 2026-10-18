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
 * No build step, no framework, no external requests; same model as cronctl.
 */

(function () {
    "use strict";

    // --- Configuration -----------------------------------------------------
    // The one place for the version (SemVer). index.html loads every script and
    // stylesheet of the add-on with ?v=VERSION; tools/check-version.sh keeps
    // them in step.
    const VERSION = "0.18.1";
    const LIB = "/usr/lib/cockpit-image-updates/";
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

    // --- Parsing -----------------------------------------------------------
    // Diun's newest event per image and tag (verKey: "postgres:16"), and a
    // registry link per image. A row takes only the event of its own tag: an
    // event for postgres:18 says nothing about postgres:16. An event for a
    // reference with a digest (postgres@sha256:…, also a CI job's service
    // container) belongs to no row: a pinned image does not follow its tag,
    // so the Update button could not bring what Diun saw.
    function parseEvents(content) {
        const byTag = new Map();
        const links = new Map();
        (content || "").split("\n").forEach((line) => {
            line = line.trim();
            if (!line) return;
            let e;
            try { e = JSON.parse(line); } catch (_) { return; }
            if (!e.image) return;
            if (e.hub_link) links.set(repoKey(e.image), e.hub_link);
            if (e.image.includes("@")) return;
            const tg = tagSplit(e.image);
            const k = verKey(tg.repo, tg.tag);
            const prev = byTag.get(k);
            if (!prev || (e.ts || "") >= (prev.ts || "")) byTag.set(k, e);
        });
        return { byTag, links };
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

    // Installed version of a container, first known source wins:
    //   inventory: the tag itself or the image's version variable (always fresh)
    //   radar:     image label or digest comparison with the registry (every 6 h)
    // A line tag (cubejs/cube:v1.6, 24.12-alpine) names only its line: the
    // radar's version wins when it refines the tag's number (1.6 -> 1.6.74).
    const refines = (found, v) => !!found && (!v || found.startsWith(v + "."));
    function installedOf(ctn, rec) {
        const r = rec && rec.installed && ctn.imgid ? rec.installed[ctn.imgid] : null;
        const fromTag = (ctn.version_src || "tag") === "tag";
        if (ctn.version && !(fromTag && r && refines(r.version, ctn.version))) {
            return { v: ctn.version, src: ctn.version_src || "tag" };
        }
        if (r && r.version) return { v: r.version, src: r.src, tag: r.tag };
        return { v: null, src: null, looked: !!r };
    }
    function pulledVersionOf(ctn, rec) {
        const r = rec && rec.installed && ctn.tagid ? rec.installed[ctn.tagid] : null;
        const found = r && r.version ? r.version : null;
        return refines(found, ctn.pulled_version) ? found : ctn.pulled_version || found;
    }
    function sourceText(inst) {
        const s = inst.src || "";
        if (s === "tag") return t("inst.tag");
        if (s.indexOf("env:") === 0) return t("inst.env", { name: s.slice(4) });
        if (s === "label") return t("inst.label");
        if (s === "digest") return t("inst.digest", { tag: inst.tag || inst.v });
        return "";
    }

    // A source revision as people quote it: 7 characters of a commit hash,
    // anything else as it is (at most 12 characters).
    function shortRevision(rev) {
        const r = String(rev || "").trim();
        return /^[0-9a-f]{7,64}$/i.test(r) ? r.slice(0, 7) : r.slice(0, 12);
    }

    // "Installed" column: exact version + where it comes from (tooltip);
    // "→ 16.15" when a newer image is already pulled but not rolled out.
    // Without a version, a development build that names its source revision
    // shows it: "main @35b2a97" (pomerium:main on a test host, 2026-10-18).
    function installedCell(ctn, rec, inst) {
        const rev = !inst.v && ctn.revision ? shortRevision(ctn.revision) : "";
        const lines = [inst.v ? sourceText(inst)
            : rev ? t("inst.revision", { rev: ctn.revision })
            : t(inst.looked ? "inst.unknown" : "inst.pending")];
        if (rev && ctn.created && !isNaN(new Date(ctn.created))) lines.push(t("inst.built", { date: fmtDate(new Date(ctn.created)) }));
        if (ctn.digest) lines.push(t("inst.digestLine", { d: shortDigest(ctn.digest) }));
        const title = esc(lines.filter(Boolean).join("\n"));
        let html = inst.v
            ? `<span class="dm-ver" title="${title}">${esc(inst.v)}</span>`
            : rev ? `<span class="dm-ver" title="${title}">${esc(ctn.tag)} @${esc(rev)}</span>`
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

    // "Newest version" column. Returns { html, news, compared }:
    //   news = an actionable newer version exists (patch/minor, or a major
    //          on a non-database image). A database major alone is grey info.
    //   compared = the radar compared the installed version (or the tag) with
    //          the registry's tags; not for an image no registry shows
    //          ("unlisted") or a registry error without earlier data.
    // Each level (patch, minor, major) is a label with the level as a word,
    // compared with the INSTALLED version. data-reach says how to get it:
    //   update    the configured tag delivers it via the Update button: a hint
    //             (az-label--info, info-circle)
    //   tag       needs a different tag: without judgement (az-label)
    //   migration a database major: without judgement, with ban
    // A "tag" level the selection field offers (pick) sits in a button that
    // opens "Change version" with that tag. Gold is never a status.
    const REACH = { update: ["az-label az-label--info", "info-circle"], tag: ["az-label", ""],
                    migration: ["az-label", "ban"] };

    // An image built on this server (compose `build:`): no registry shows it,
    // but the radar checks the images its Dockerfile starts FROM (rec.base).
    // A base published after the build, or with a newer version than the
    // build carries, is news; the tooltip says how to rebuild. With every base
    // older than the build the row is up to date as far as the radar can tell.
    function baseCell(ctn, rec, muted) {
        const ref = (b) => `${b.image}:${b.tag}`;
        const lines = [t("ver.baseBuilt", { date: fmtDay(rec.built) })];
        rec.base.forEach((b) => {
            if (b.pinned) { lines.push(t("ver.basePinned", { ref: ref(b) })); return; }
            if (b.error) { lines.push(t("ver.baseError", { ref: ref(b), error: b.error })); return; }
            lines.push(t("ver.baseLine", { ref: ref(b), role: t(b.final ? "ver.baseFinal" : "ver.baseStage"),
                                          now: b.now || "?", pushed: fmtDay(b.pushed) }) +
                       (b.version ? t("ver.baseCarries", { version: b.version }) : "") +
                       (b.newer ? t("ver.baseIsNewer") : ""));
        });
        const checked = rec.base.filter((b) => !b.pinned && !b.error);
        const newer = checked.filter((b) => b.newer);
        if (newer.length) {
            const first = newer.find((b) => b.final) || newer[0];
            const files = ctn.files || [];
            const dir = files.length ? files[0].slice(0, files[0].lastIndexOf("/")) || "/" : "?";
            lines.push(t("ver.baseRebuild", { service: ctn.service || "?", dir }));
            const text = first.now ? t("ver.baseNewerNow", { ref: ref(first), now: first.now }) : t("ver.baseNewer", { ref: ref(first) });
            return { html: `<span class="az-label az-label--info dm-level" data-level="base" title="${esc(lines.join("\n"))}">` +
                           `${icon("info-circle")}${esc(text)}</span>`, news: true, compared: true, baseChecked: true };
        }
        if (!checked.length && rec.base.some((b) => b.error)) return { ...muted(t("ver.baseUnchecked"), lines.join("\n")), compared: false };
        lines.push(t("ver.baseCurrentTip"));
        return { html: `<span class="dm-uptodate" title="${esc(lines.join("\n"))}">${icon("check-circle")}` +
                       `<span>${esc(t("ver.baseCurrent"))}</span></span>`, news: false, compared: true, baseChecked: true };
    }

    // A tag like latest that no release tag shares a digest with, pushed to
    // Docker Hub after the newest release (radar: tag_now.after; MobSF's latest
    // is a 4.5.4 build while v4.5.3 is the newest release). Only while that
    // release is still the newest the radar knows.
    function buildAfter(rec) {
        const tn = rec.tag_now;
        const a = tn && !tn.version && tn.after;
        return a && a.tag && a.tag === rec.newest ? a : null;
    }
    // "build after v4.5.3": up to date (check-circle) when the container runs
    // the build the tag points to now; the version number stays unknown.
    function buildAfterCell(ctn, rec, after, muted) {
        const same = !!ctn.digest && ctn.digest === rec.tag_now.digest;
        const text = t("ver.buildAfter", { tag: after.tag });
        const tip = [t("ver.buildAfterTip", { cfg: ctn.tag, tag: after.tag, pushed: fmtDay(rec.tag_now.pushed),
                                              released: fmtDay(after.pushed) }),
                     t(same ? "ver.buildAfterRuns" : "ver.buildAfterOther", { cfg: ctn.tag })].join("\n");
        if (!same) return { ...muted(text, tip), compared: false };
        return { html: `<span class="dm-uptodate" title="${esc(tip)}">${icon("check-circle")}<span>${esc(text)}</span></span>`,
                 news: false, compared: true, sameBuild: true };
    }

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
            const detail = (key, vars) => [t(key, vars), rec.error].filter(Boolean).join("\n");
            if (rec.status === "unlisted" && Array.isArray(rec.base) && rec.base.length) return baseCell(ctn, rec, muted);
            if (rec.status === "unlisted") return muted(t("ver.unlisted"), detail("ver.unlistedTip", { image: ctn.image }));
            if (rec.status === "error") return muted(t("ver.registryError"), detail("ver.registryErrorTip"));
            const after = buildAfter(rec);
            if (after) return buildAfterCell(ctn, rec, after, muted);
            if (rec.newest) return muted(t("ver.newestOnly", { v: rec.newest }), t("ver.newestOnlyTip"));
            return muted("—", t("ver.notCovered"));
        }
        const stale = rec.stale
            ? ` <span class="dm-muted" title="${esc(rec.error || "")}">${esc(t("ver.stale"))}</span>` : "";
        const reach = Array.isArray(rec.reach) ? rec.reach : [];
        const levels = [];
        let news = false;
        LEVELS.forEach(([lvl, label]) => {
            if (!cmp[lvl]) return;
            const info = lvl === "major" && rec.db;
            if (!info) news = true;
            const can = reach.indexOf(lvl) > -1;
            const how = info ? "migration" : can ? "update" : "tag";
            const [cls, ic] = REACH[how];
            const tag = how === "tag" && pick && pick.has(cmp[lvl]) ? cmp[lvl] : null;
            const title = esc([info ? t("ver.dbMajor") : t("ver.levelTip", { level: label }),
                               t(can ? "ver.reachYes" : "ver.reachNo", { tag: ctn.tag })]
                .concat(tag ? [t("ver.clickRetag", { tag })] : []).join("\n"));
            const lab = `<span class="${cls} dm-level" data-level="${lvl}" data-reach="${how}"${tag ? "" : ` title="${title}"`}>` +
                        `${ic ? icon(ic) : ""}${label} <span class="dm-ver">${esc(cmp[lvl])}</span></span>`;
            levels.push(tag
                ? `<button type="button" class="dm-level-btn" title="${title}" ` +
                  `data-retag="${esc(ctn.name)}" data-tag="${esc(tag)}">${lab}</button>`
                : lab);
        });
        if (!levels.length) {
            return { html: `<span class="dm-uptodate" title="${esc(t("ver.upToDateTip"))}">${icon("check-circle")}` +
                           `${inst.v ? `<span class="dm-ver">${esc(inst.v)}</span>` : ""}</span>${stale}`, news: false, compared: true };
        }
        return { html: `<span class="dm-levels">${levels.join("")}</span>${stale}`, news, compared: true };
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

    // When an image was built, in seconds: Docker's "2026-10-15T19:00:50.089Z"
    // and Diun's "2026-10-15 19:00:50.089 +0000 UTC". null if unreadable.
    function builtAt(s) {
        const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?/.exec(s || "");
        if (!m) return null;
        const zone = !m[3] || m[3] === "Z" ? "Z" : m[3].slice(0, 3) + ":" + m[3].slice(-2);
        const ms = Date.parse(m[1] + "T" + m[2] + zone);
        return isNaN(ms) ? null : ms / 1000;
    }
    // Diun reports a new digest of the tag, but the build is the one that runs:
    // Docker Hub publishes the list of platforms of an official image again
    // when it adds one (nextcloud:apache, 2026-10-16: riscv64), and its digest
    // changes while the amd64 build stays. A fixed date (1970, reproducible
    // builds) proves nothing.
    function sameBuild(ev, ctn) {
        const a = builtAt(ev.created), b = builtAt(ctn.created);
        return a !== null && a === b && a > Date.UTC(1980, 0, 1) / 1000;
    }

    function rowFor(ctn, events, versionsMap, radarOk) {
        const ev = events.byTag.get(verKey(ctn.image, ctn.tag));
        const acknowledged = ev && dismissed[evKey(ev)];
        let kind;
        if (ctn.pulled) {
            kind = "pulled";        // newer image already local, container on the old one
        } else if (ev && ev.digest && ctn.digest && ev.digest !== ctn.digest && !acknowledged && !sameBuild(ev, ctn)) {
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
        // Without a known installed version, or when the radar compared
        // nothing (no registry shows the image, a registry error), "up to
        // date" would be a guess: Docker gives locally built images a digest
        // too, and Diun reports nothing for them. Not a guess: the container
        // runs the build a tag like latest points to now (sameBuild).
        if (kind === "ok" && radarOk && rec && ((!inst.v && !ver.sameBuild && !ver.baseChecked) || !ver.compared)) kind = "unknown";
        const failedBuild = kind === "update" ? sameFailedBuild(ctn, ev) : null;
        rowFailed.set(ctn.name, failedBuild);
        const note = failedBuild
            ? `<span class="dm-row-note">${esc(t("row.failedBuild", { date: fmtDate(new Date(failedBuild.ts)) }))}</span>` : "";

        const link = events.links.get(repoKey(ctn.image));
        const imgCell = link
            ? `<a href="${esc(link)}" target="_blank" rel="noopener">${esc(ctn.image)}</a>`
            : esc(ctn.image);

        // Three places: the update, "Change version", "Dismiss" (see iconSlot).
        let main = "";
        if (kind === "update" || kind === "pulled") {
            main = ctn.managed
                ? textButton("arrow-circle-up", t(kind === "pulled" ? "btn.rollout" : "btn.update"),
                             `data-update="${esc(ctn.name)}" data-kind="${kind}"`)
                : `<span class="dm-muted dm-small" title="${esc(t("row.manualOnlyTip"))}">${esc(t("row.manualOnly"))}</span>`;
        }
        const retag = pick.size ? iconButton("exchange-alt", t("btn.retag"), `data-retag="${esc(ctn.name)}"`) : "";
        const dismissBtn = kind === "update" ? iconButton("check", t("btn.dismiss"), `data-key="${esc(evKey(ev))}"`) : "";
        const actions = main || retag || dismissBtn ? [main, iconSlot(retag), iconSlot(dismissBtn)] : [];

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

    // fromEvents: no inventory, the rows come from Diun's events alone.
    function render(all, events, radarDoc, fromEvents) {
        lastRender = [all, events, radarDoc, fromEvents];
        // Containers of CI jobs come and go with the job and cannot be
        // updated: counted in the meta line, not listed (ci_job, inventory).
        const ciJobs = all.filter((c) => c.ci_job);
        const containers = all.filter((c) => !c.ci_job);
        rowFailed.clear();
        retagInfo.clear();
        // An update recreates every service of the project with the same image
        // that was started from the same compose files.
        siblings.clear();
        const groups = new Map();
        containers.forEach((c) => {
            if (!c.project) return;
            const k = c.project + "|" + (c.files || []).join(",") + "|" + verKey(c.image, c.tag);
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
        const built = containers.map((c) => ({ c, ...rowFor(c, events, versionsMap, radarOk) }));
        built.sort((a, b) =>
            (ORDER[a.kind] - ORDER[b.kind]) || a.c.name.localeCompare(b.c.name));

        const updates = built.filter((b) => b.kind === "update" || b.kind === "pulled").length;
        const newver = built.filter((b) => b.kind === "newver").length;
        $("dm-meta").textContent = [
            t("meta.updates", { n: updates }),
            t("meta.newver", { n: newver }),
            t("meta.containers", { n: containers.length }),
        ].concat(ciJobs.length ? [t("meta.ciJobs", { n: ciJobs.length })] : [], [radarMeta(radarDoc)]).join(" · ");
        $("dm-meta").title = ciJobs.length
            ? [t("meta.ciJobsTip")].concat(ciJobs.map((c) => ciJobText(c.name, null))).join("\n") : "";

        if (containers.length === 0) {
            $("dm-table").hidden = true;
            $("dm-legend").hidden = true;
            $("dm-empty-title").textContent = t(fromEvents ? "empty.eventsTitle" : "empty.title");
            $("dm-empty-body").textContent = t(fromEvents ? "empty.eventsBody" : "empty.body");
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
            $("dm-dialog-desc").innerHTML = rich(spec.desc || "");
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

        // The outcome line; success and failure carry their icon, not colour alone.
        result(text, ok) {
            const res = $("dm-dialog-result");
            res.innerHTML = text ? (ok === true ? icon("check-circle") : ok === false ? icon("exclamation-circle") : "") + esc(text) : "";
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
            const title = r.container ? t("upd.reasonTitle", { name: r.container })
                : t(r.source === "error" ? "upd.reasonErrorTitle" : "upd.reasonComposeTitle");
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
        confirmAndRun({
            title: t(base + ".title"),
            desc: t(base + ".desc"),
            confirm: { label: t("action." + verb + "." + target), argv: () => [DEPS_CMD, verb, target] },
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
            if (!d.running) return errorText(t("upd.dbStopped", { name: d.name }));
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
        const gate = (title, warning, id, confirm) =>
            `<div class="dm-gate-group">${alertBox("warning", title, warning)}` +
            `<label class="dm-check"><input type="checkbox" class="dm-gate" id="${id}"> ${esc(confirm)}</label></div>`;
        if (plan.jump_unknown && rt) {
            html += gate(t("rt.floatingTitle"), esc(t("rt.floating", { tag: rt.tag })), "dm-jump-check",
                         t("rt.floatingConfirm", { tag: rt.tag }));
        } else if (plan.jump_unknown) {
            html += gate(t("upd.jumpTitle"), esc(t("upd.jumpUnknown", { tag })) +
                         (plan.target_version ? " " + esc(t("upd.jumpTarget", { tag, v: plan.target_version })) : ""),
                         "dm-jump-check", t("upd.jumpConfirm"));
        }
        // The update brings a new major version (installed and target known).
        if (plan.jump === "major") {
            html += gate(t("upd.majorTitle"), esc(t("upd.majorJump", { from: plan.installed_version, to: plan.target_version })),
                         "dm-major-check", t("upd.majorConfirm", { to: plan.target_version }));
        }
        // The registry offers exactly the build whose update failed before.
        if (failed) {
            const cause = failed.reason ? reasonText(failed.reason) : "";
            html += gate(t("upd.failedBuildTitle"), esc(t("upd.failedBuild", { date: fmtDate(new Date(failed.ts)) })) +
                         (cause ? `<span class="dm-reason-line dm-alert-line">${esc(cause)}</span>` : ""),
                         "dm-retry-check", t("upd.retryConfirm"));
        }
        // A CI runner with running jobs: they break off.
        if ((plan.ci_jobs || []).length) {
            html += gate(t("upd.ciJobsTitle"), esc(t("upd.ciJobs", {
                jobs: plan.ci_jobs.map((j) => ciJobText(j.name, j.started)).join(", ") })),
                         "dm-ci-check", t("upd.ciJobsConfirm"));
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
        // Only with a database the add-on can dump; without one the list above
        // says so and there is nothing to choose (see dumpArgs).
        if (dumpable) {
            html += `<label class="dm-check"><input type="checkbox" id="dm-dump-check" checked> ` +
                    `${esc(t("upd.dumpCheck"))}</label>`;
        }
        if (plan.config_changed) html += alertBox("warning", t("upd.configChangedTitle"), esc(t("upd.configChanged")));
        (plan.job_order || []).forEach((o) => {
            html += alertBox("warning", t("upd.jobOrderTitle"), rich(t("upd.jobOrder", { job: o.job, services: o.services.join(", ") })));
        });
        (plan.shared_networks || []).forEach((n) => {
            html += alertBox("info", t("upd.sharedNetTitle"), esc(t("upd.sharedNet", { network: n.network, projects: n.projects.join(", ") })));
        });
        html += alertBox("info", t("upd.dataTitle"), `${esc(t("upd.dataWarn"))} ${esc(t("upd.downtime"))}`);
        return html;
    }

    // --no-dump only when the dump was unticked. Without the tick box the
    // project has no database to dump, and the helper says so itself.
    function dumpArgs() {
        const box = $("dm-dump-check");
        return box && !box.checked ? ["--no-dump"] : [];
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
                    const missing = (plan.missing || []).join(", ");
                    const reason = t("reason." + plan.reason, { files: missing, services: missing, detail: plan.detail || "" });
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
                        disabled: !!plan.jump_unknown || !!failed || plan.jump === "major" || (plan.ci_jobs || []).length > 0,
                        argv: () => [UPDATE_CMD, "update", name].concat(dumpArgs()),
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
            (rt.variable ? alertBox("info", t("rt.variableTitle"), rich(t("rt.variable", { name: rt.variable }))) : "") +
            (rt.git ? alertBox("info", t("rt.gitTitle"), rich(t("rt.git", { path: rt.git }))) : "");
    }

    // Why a version change is not possible, as a sentence of its own (it
    // stands under a title); a reason this page does not know yet as it is.
    function retagReason(plan) {
        const rt = plan.retag || {};
        const key = "reason." + rt.reason;
        const missing = (plan.missing || []).join(", ");
        const text = t(key, { tag: rt.tag, detail: rt.detail || "", files: missing, services: missing });
        return text === key ? rt.reason + (rt.detail ? ` (${rt.detail})` : "") : text.charAt(0).toUpperCase() + text.slice(1);
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
                argv: () => [UPDATE_CMD, "retag", name, shown.tag, "--expect", shown.fp].concat(dumpArgs()),
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
                        box.innerHTML = alertBox("warning", t("upd.notPossibleTitle"), rich(retagReason(plan)));
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
                    box.innerHTML = alertBox("danger", t("rt.checkFailed"), esc(isNoAdmin(ex) ? t("dialog.noAdmin")
                        : t("dialog.error", { msg: (ex && (ex.message || ex.problem)) || ex })));
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

    // A failed rollback may be tried again; up to 0.18.0 it was marked
    // rolled_back as well. "running" without a process that still runs
    // (active) is an update that ended without its last line.
    function opResult(o) {
        if (o.rollback_status === "failed") return label("danger", "exclamation-circle", t("res.rollbackFailed"));
        if (o.rolled_back) return label("", null, t("res.rolledBack"));
        if (o.active) return label("", null, t("res.active"));
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
        const r = (o.rollback_status === "failed" && o.rollback_reason) ||
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
            rules.innerHTML = errorText(t("cl.error", { msg: c.error }));
        } else {
            const keep = c.keep_dumps ? t("cl.keep", { n: c.keep_dumps }) : t("cl.keepOff");
            const days = c.image_days ? t("cl.days", { n: c.image_days }) : t("cl.daysOff");
            rules.innerHTML = [t("cl.rules", { keep, days }),
                               c.file ? t("cl.srcFile", { file: c.file }) : t("cl.srcDefault"),
                               t("cl.protect")].map(rich).join(" ");
        }
        const timer = $("dm-cleanup-timer");
        const cl = lastDeps && lastDeps.root ? lastDeps.cleanup : null;
        timer.hidden = !cl;
        timer.innerHTML = cl ? rich(t(cl.state === "active" ? "cl.timerOn" : "cl.timerOff")) : "";
        if (cl && cl.state !== "active" && cl.can_install) {
            timer.insertAdjacentHTML("beforeend", " " + textButton("plus", t("action.install.cleanup"), 'data-setup="cleanup"'));
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

    // Size of a rollback image in bytes, its creation time in ISO 8601; older
    // helpers sent Docker's text ("275MB", "2026-10-12 03:15:00 +0200 CEST").
    const imageSize = (s) => typeof s === "number" ? human(s) : s || "?";
    const imageDate = (s) => fmtDate(new Date(String(s || "").replace(/^(\S+) (\S+) ([+-]\d\d)(\d\d)\b.*$/, "$1T$2$3:$4")));

    function renderBackups() {
        const b = backups || { dumps: [], images: [], ops: [] };
        const L = (key) => esc(t(key));
        const doomed = new Set(b.cleanup ? (b.cleanup.dumps || []).concat(b.cleanup.images || []) : []);
        renderCleanup();
        const summary = $("dm-bk-summary");
        const failure = $("dm-bk-error");
        failure.hidden = !!backups || !backupsError;
        failure.innerHTML = failure.hidden ? "" : alertInner("danger", t("bk.errorTitle"), esc(backupsError));
        summary.hidden = !backups;
        summary.textContent = backups ? t("bk.summary", {
            dumps: t("bk.dumpsN", { n: b.dumps.length, size: human(b.dump_bytes) }),
            images: t("bk.imagesN", { n: b.images.length }),
            free: human(b.free_bytes), dir: b.dir,
        }) : "";
        // Without data the cards would call themselves empty: only the error shows.
        ["dm-ops-title", "dm-dumps-title", "dm-images-title"].forEach((id) => { $(id).closest(".az-card").hidden = !backups; });

        const ops = $("dm-ops");
        const logButton = (o, i) => (o.log || o.rollback_log
            ? iconButton("angle-right", t("btn.showLog"), `data-log="${i}"`) : "");
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
              (o.can_rollback ? textButton("history", t("rb.button"), `data-op="${i}"`) + iconSlot(logButton(o, i))
                              : logButton(o, i)) +
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
                confirm: { label: t("del.dumpConfirm"), danger: true, argv: () => [UPDATE_CMD, "delete-dump", d.path] },
                onDone: (o) => { if (o.ok) { dlg.result(t("del.ok"), true); return true; } return false; },
            }));
        });

        const imgs = $("dm-images");
        imgs.innerHTML = b.images.map((im, i) =>
            `<tr>` +
            `<td data-label="${L("col.image")}"><span class="dm-cell az-mono">${esc(im.ref)}</span></td>` +
            `<td class="az-nowrap" data-label="${L("col.created")}"><span class="dm-cell">${esc(imageDate(im.created))}</span></td>` +
            `<td class="az-nowrap" data-label="${L("col.size")}"><span class="dm-cell">${esc(imageSize(im.size))}</span></td>` +
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
                confirm: { label: t("del.imgConfirm"), danger: true, argv: () => [UPDATE_CMD, "delete-image", im.ref] },
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
            const events = parseEvents(eventsContent);
            if (invOut === null) {
                // Inventory unavailable (no Docker access / no admin rights):
                // fall back to a Diun-events-only view.
                showStatus(t("status.noInventoryTitle"), t("status.noInventory"), false);
                const evContainers = Array.from(events.byTag.values()).map((e) => {
                    const tg = tagSplit(e.image);
                    return { name: "—", image: tg.repo, tag: tg.tag, digest: "" };
                });
                render(evContainers, events, radarDoc, true);
                return;
            }
            $("dm-status").hidden = true;
            render(parseInventory(invOut), events, radarDoc);
        }).catch((err) => {
            showStatus(t("status.loadErrorTitle"), t("status.loadError", { msg: err.message || err }), true);
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

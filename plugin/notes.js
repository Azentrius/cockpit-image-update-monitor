// SPDX-License-Identifier: Apache-2.0
/* Image Update Monitor, a Cockpit add-on: the release notes in the plan of an
 * update or a version change (stage 3), from the radar's releases.json. Part of
 * the page, see common.js. */
(function () {
    "use strict";
    const P = window.ImageUpdatesPage;
    const { RELEASES_PATH, FILE_OPTS, t, $, esc, rich, fmtDay, fmtDate, label } = P;

    const releasesFile = cockpit.file(RELEASES_PATH, { ...FILE_OPTS, syntax: JSON });

    // --- Release notes (stage 3) -------------------------------------------
    // The releases between the installed and the target version, from the
    // radar's releases.json (the radar fetches them from GitHub; the browser
    // never does). Text only: Markdown is shown as its text, images and HTML
    // are left out, words that call for care are marked. Anything missing is a
    // note in the dialog, never an error.
    function verNums(v) {
        const m = /^v?(\d+(?:\.\d+)*)/.exec(String(v || ""));
        if (!m) return null;
        const n = m[1].split(".").map(Number);
        while (n.length > 1 && n[n.length - 1] === 0) n.pop();     // 16.14 = 16.14.0
        return n;
    }
    // -1 / 0 / 1, NaN when either is no version.
    function cmpVersion(a, b) {
        const x = verNums(a), y = verNums(b);
        if (!x || !y) return NaN;
        for (let i = 0; i < Math.max(x.length, y.length); i++) {
            const p = i < x.length ? x[i] : -1, q = i < y.length ? y[i] : -1;
            if (p !== q) return p < q ? -1 : 1;
        }
        return 0;
    }
    const FLAG_RE = /\b(breaking|migrat[a-z]*|deprecat[a-z]*)\b/gi;
    function flagsOf(text) {
        const found = new Set();
        String(text || "").replace(FLAG_RE, (w) => {
            const l = w.toLowerCase();
            found.add(l.startsWith("migrat") ? "migration" : l.startsWith("deprecat") ? "deprecated" : "breaking");
            return w;
        });
        return ["breaking", "migration", "deprecated"].filter((w) => found.has(w));
    }
    // HTML elements release notes use, as they read once escaped; other text
    // in angle brackets (Vec<T>, <name>) stays.
    const NOTE_TAG_RE = /&lt;\/?(?:a|b|br|code|del|details|div|em|h[1-6]|hr|i|img|kbd|li|ol|p|picture|pre|source|span|strong|sub|summary|sup|table|tbody|td|th|thead|tr|u|ul|video)\b[^\n]*?&gt;/gi;
    // A release's Markdown as text: headings bold, list marks as bullets,
    // links as their text; images, HTML and comments left out. Escaped
    // first: what follows only shortens text that can no longer be HTML.
    function noteBody(text) {
        const plain = esc(String(text || "").replace(/\r\n?/g, "\n"))
            .replace(/&lt;!--[\s\S]*?--&gt;/g, "")
            .replace(/&lt;br\s*\/?&gt;/gi, "\n")
            .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
            .replace(/\[([^\]]+)\]\([^)\s]+\)/g, "$1")
            .replace(NOTE_TAG_RE, "")
            .replace(/\n{3,}/g, "\n\n").trim();
        return plain.split("\n").map((line) => {
            const h = /^#{1,6}\s+(.*)$/.exec(line);
            const html = (h ? h[1] : line.replace(/^(\s*)[*-]\s+/, "$1• "))
                .replace(FLAG_RE, '<mark class="dm-flag">$1</mark>');
            return h ? `<strong>${html}</strong>` : html;
        }).join("\n");
    }
    function notesHtml(doc, rec, from, to) {
        const note = (key, vars) => `<p class="dm-dialog-note">${rich(t(key, vars))}</p>`;
        if (!rec) return "";                    // the radar knows nothing of this image
        const src = rec.source && rec.source.repo;
        if (!src) return note("notes.noSource");
        const repo = src;
        if (!from) return note("notes.unknownInstalled", { repo });
        const e = doc && doc.repos && typeof doc.repos === "object" ? doc.repos[src] : null;
        if (!e) return note("notes.pending", { repo });
        if (e.status === "missing") return note("notes.missing", { repo });
        if (!Array.isArray(e.releases)) {
            return note(e.status === "limited" ? "notes.limited" : "notes.error", { repo, error: e.error || "?" });
        }
        // Without a target (a tag like latest that names no version): every
        // release since the installed version.
        const rels = e.releases.filter((r) => r && cmpVersion(r.version, from) > 0 && (!to || cmpVersion(r.version, to) <= 0));
        // Older releases than the oldest kept may be missing (more): only
        // worth saying when that one is newer than the installed version.
        const kept = e.releases[e.releases.length - 1];
        const more = e.more && kept && cmpVersion(kept.version, from) > 0 ? note("notes.more", { repo }) : "";
        if (!rels.length) return note(to ? "notes.none" : "notes.noneAfter", { repo, from, to }) + more;
        const items = rels.map((r) => {
            const flags = flagsOf(r.body);
            const url = typeof r.url === "string" && r.url.startsWith("https://github.com/") ? r.url : "";
            return `<details class="dm-release"><summary><span class="dm-release-tag">${esc(r.tag)}</span>` +
                (r.name ? ` <span class="dm-muted">${esc(r.name)}</span>` : "") +
                (r.published ? ` <span class="dm-muted">· ${esc(fmtDay(r.published))}</span>` : "") +
                (flags.length ? " " + label("warning", "", flags.join(", ")) : "") + `</summary>` +
                (r.body ? `<div class="dm-release-body">${noteBody(r.body)}</div>` : note("notes.empty")) +
                (r.cut ? note("notes.cut") : "") +
                (url ? `<a class="dm-release-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(t("notes.onGithub"))}</a>` : "") +
                `</details>`;
        });
        const flags = flagsOf(rels.map((r) => r.body).join("\n"));
        return `<details class="dm-notes"><summary><span class="dm-notes-title">` +
            `${esc(t(to ? "notes.title" : "notes.titleAfter", { n: rels.length, from, to }))}</span>` +
            (flags.length ? " " + label("warning", "exclamation-triangle", t("notes.flags", { words: flags.join(", ") }), t("notes.flagsTip")) : "") +
            `</summary><div class="dm-notes-list">${items.join("")}</div>` +
            note("notes.source", { repo, date: fmtDate(new Date(e.checked)) }) + more +
            `</details>`;
    }
    // Fills the slot of a plan (planBody) once releases.json is read; a plan
    // shown again in the meantime (another choice of version) has a new slot.
    function showNotes(slot, plan, name) {
        if (!slot) return;
        const from = plan.installed_version, to = plan.target_version;
        if (plan.jump === "same" || (from && to && !(cmpVersion(to, from) > 0))) return;    // nothing new to read
        const rec = P.rowRec.get(name) || null;
        Promise.resolve().then(() => releasesFile.read()).catch(() => null).then((doc) => {
            if (slot.isConnected) slot.innerHTML = notesHtml(doc, rec, from, to);
        });
    }

    Object.assign(P, { showNotes });
})();

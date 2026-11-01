// SPDX-License-Identifier: Apache-2.0
/* Image Update Monitor, a Cockpit add-on: "Change version" (stage 2): what the
 * selection field offers, the preview of the change and the change itself. Part
 * of the page, see common.js. */
(function () {
    "use strict";
    const P = window.ImageUpdatesPage;
    if (!P) return;     // common.js did not load: diun.js asks Cockpit for it again
    const { UPDATE_CMD, t, $, tagSplit, repoKey, esc, rich, human, spawnHelper, isNoAdmin, label, alertBox, engineName,
            LEVELS, dlg, confirmAndRun, ticket } = P;

    // What the selection field of "Change version" offers: the radar's
    // versions[installed].choices (since 0.11.0), in the field's order, as
    // [{ group, tag, text, disabled, major }]. A PostgreSQL major with a known
    // target is a database major (stage 5, `dbmajor`); a newer major of a
    // key-value store (since 0.27.0) is a tag change that saves the store's data
    // first; other database majors, and Nextcloud jumps over more than one
    // major, are listed but cannot be chosen; the helper refuses them as well
    // (db-major, major-skip). The configured tag is the field's first entry and
    // is left out here.
    const NEXTCLOUD_RE = /(^|\/)nextcloud$/;
    const PG_RE = /(^|\/)(postgres|postgresql|postgis|pgvector|timescaledb[a-z0-9-]*)$/;
    const MARIADB_RE = /(^|\/)mariadb$/;
    const STORE_RE = /(^|\/)(redis|valkey|keydb)$/;

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
        // PostgreSQL and (since 0.25.0) MariaDB; MySQL and the other databases stay locked.
        const engine = PG_RE.test(repoKey(ctn.image)) || MARIADB_RE.test(repoKey(ctn.image));
        const dbMajor = (target) => rec.db && engine && target != null && majorOf(target) > cur;
        const storeMajor = (target) => rec.db && STORE_RE.test(repoKey(ctn.image)) && target != null && majorOf(target) > cur;
        const blocked = (target) => {      // the version the tag brings; null: follows every release
            const m = target == null ? null : majorOf(target);
            if (rec.db && m !== cur && !dbMajor(target) && !storeMajor(target)) return "rt.optDbMajor";
            if (nextcloud && (m == null || m - cur > 1)) return "rt.optSkip";
            return null;
        };
        const out = [];
        const add = (group, tag, key, target, vars) => {
            if (!tag || tag === ctn.tag || out.some((o) => o.tag === tag)) return;
            const why = blocked(target);
            const major = !why && dbMajor(target) && majorOf(target) !== cur;
            const store = !why && storeMajor(target);
            out.push({ group, tag, disabled: !!why, major,
                       text: why ? t(why, { tag }) : major ? t("rt.optMajor", { tag })
                           : store ? t("rt.optStoreMajor", { tag }) : key ? t(key, { ...vars, tag }) : tag });
        };
        add("rt.grp.pin", ch.pin, "rt.optPin", inst.v);
        (ch.lines || []).forEach((l) => add("rt.grp.lines", l.tag, "rt.optLine", l.newest, { v: l.newest }));
        LEVELS.forEach(([lvl]) => (ch.newer || []).forEach((x) => {
            if (x.level === lvl) add("rt.grp." + lvl, x.tag, null, x.tag);
        }));
        (ch.floating || []).forEach((f) => add("rt.grp.floating", f, null, null));
        return out;
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
                  `<option value="${esc(o.tag)}"${o.disabled ? " disabled" : ""}${o.major ? ' data-major="1"' : ""}>${esc(o.text)}</option>`).join("") +
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

    // --- Database major (stage 5) --------------------------------------------
    // A PostgreSQL major is planned by `dbmajor … --dry-run`: the tag change
    // plus "major" (where the data lies, what stops meanwhile). It asks for two
    // tick boxes more and runs as `dbmajor … --expect <fingerprint>`.
    function majorReason(plan) {
        const mj = plan.major || {};
        if (!String(mj.reason || "").startsWith("major-")) return retagReason(plan);
        const [a, b, c] = String(mj.detail || "").split(" | ");
        const text = t("reason." + mj.reason, { detail: mj.detail || "", a: a || "", b: b || "", c: c || "" });
        return text.charAt(0).toUpperCase() + text.slice(1);
    }

    function majorBody(plan) {
        const mj = plan.major, data = mj.data || {};
        const li = (k, v) => `<li><span class="dm-kv-k">${esc(k)}</span><span class="dm-kv-v">${v}</span></li>`;
        const code = (s) => `<code>${esc(s)}</code>`;
        const stop = mj.stop.length ? mj.stop.map(code).join(" ") : esc(t("mj.none"));
        const vars = { engine: engineName(mj.engine), from: mj.from_major, to: mj.to_major };
        const mariadb = mj.engine === "mariadb";
        return `<div class="dm-gate-group">` +
            alertBox("warning", t("mj.title", vars), esc(t(mariadb ? "mj.bodyMariadb" : "mj.body", vars))) +
            `<label class="dm-check"><input type="checkbox" class="dm-gate" id="dm-mj-backup"> ${esc(t("mj.backupConfirm"))}</label>` +
            `<label class="dm-check"><input type="checkbox" class="dm-gate" id="dm-mj-downtime"> ${esc(t("mj.downtimeConfirm"))}</label>` +
            `</div><ul class="dm-kv">` +
            li(t("mj.data"), code(data.type === "volume" ? data.name : data.source) +
               ` <span class="dm-muted">${esc(t(data.type === "volume" ? "mj.volume" : "mj.bind"))}</span>`) +
            (mariadb ? li(t("mj.datadir"), code(mj.datadir)) : li(t("mj.pgdata"), code(mj.pgdata))) +
            li(t("mj.stop"), stop) +
            (mariadb ? "" : li(t("mj.ext"), mj.extensions.length ? mj.extensions.map(code).join(" ") : esc(t("mj.none")))) +
            (mj.data_bytes != null ? li(t("mj.size"), esc(t("mj.sizeFree", { size: human(mj.data_bytes), free: human(mj.data_free) }))) : "") +
            `</ul>`;
    }

    // --- Key-value store major (since 0.27.0) --------------------------------
    // Redis, Valkey, KeyDB: the plan's retag.store names the data directory the
    // helper packs after stopping the store; the rollback puts it back.
    function storeBody(st) {
        const saved = (st.items || []).filter((i) => i.source);
        const li = (k, v) => `<li><span class="dm-kv-k">${esc(k)}</span><span class="dm-kv-v">${v}</span></li>`;
        return alertBox("warning", t("st.title", { engine: st.name }), esc(t(saved.length ? "st.body" : "st.bodyNone", { engine: st.name }))) +
            (saved.length ? `<ul class="dm-kv">` + saved.map((i) => li(i.container, `<code>${esc(i.source)}</code>` +
                ` <span class="dm-muted">${esc(i.bytes == null ? "?" : human(i.bytes))}</span>`)).join("") + `</ul>` : "");
    }

    function majorOutcome(o, name) {
        const r = o.result || {};
        if (o.ok) {
            dlg.result(t("mj.ok", { from: r.from_version || "?", to: r.to_version || "?", where: r.old_data || "?" }), true);
            return true;
        }
        if (o.exit === 4 && r.op) {
            dlg.reason(r.reason);
            dlg.result(t("mj.failed"), false);
            P.offerRollback(r.op, name);
            return true;
        }
        if (o.exit === 5) { dlg.result(t("upd.aborted"), false); return true; }
        return false;
    }

    // preset: the tag of a clicked chip; "" opens the field on the configured tag.
    function startRetag(name, preset) {
        const info = P.retagInfo.get(name);
        if (!info) return;
        const superseded = ticket();
        const label = t("btn.retag");
        const shown = { tag: null, fp: null, major: false };    // the change the preview shows
        const ready = (on) => dlg.btn("dm-dialog-confirm", t(shown.major ? "btn.major" : "btn.retag"),
                                      { visible: true, disabled: !on });
        confirmAndRun({
            title: t("rt.title", { name }), desc: t("rt.desc"),
            body: retagField(info.ctn, info.inst, info.options),
            confirm: {
                label, disabled: true,
                argv: () => shown.major ? [UPDATE_CMD, "dbmajor", name, shown.tag, "--expect", shown.fp]
                    : [UPDATE_CMD, "retag", name, shown.tag, "--expect", shown.fp].concat(P.dumpArgs()),
            },
            onDone: (o) => shown.major ? majorOutcome(o, name) : retagOutcome(o, name, shown.tag),
        });
        const sel = $("dm-retag-select");
        const box = $("dm-retag-preview");
        let seq = 0;
        const preview = () => {
            const tag = sel.value;
            const mine = ++seq;
            // A plan that arrives after another choice, after the dialog was
            // closed or reopened, or after the run started, is dropped.
            const stale = () => mine !== seq || superseded() || !$("dm-dialog").hasAttribute("open") || dlg.running;
            const opt = sel.options[sel.selectedIndex];
            const major = !!(opt && opt.dataset.major);
            shown.tag = shown.fp = null;
            shown.major = major;
            ready(false);
            box.innerHTML = tag ? `<p class="dm-dialog-note">${esc(t("rt.checking"))}</p>` : "";
            if (!tag) return;
            Promise.resolve().then(() => spawnHelper([UPDATE_CMD, major ? "dbmajor" : "retag", name, tag, "--dry-run"], "try"))
                .then((out) => {
                    if (stale()) return;
                    let plan;
                    try { plan = JSON.parse(out); } catch (_) { throw new Error("bad plan output"); }
                    const rt = plan.retag || {};
                    if (major ? !(plan.major && plan.major.possible) : !rt.possible) {
                        box.innerHTML = alertBox("warning", t("upd.notPossibleTitle"), rich(major ? majorReason(plan) : retagReason(plan)));
                        return;
                    }
                    box.innerHTML = (major ? majorBody(plan) : "") + (rt.store ? storeBody(rt.store) : "") + retagDiff(rt) +
                        P.planBody(plan, null, rt);
                    // A major always dumps: the dump is what it restores.
                    const dump = major && box.querySelector("#dm-dump-check");
                    if (dump) dump.closest("label").remove();
                    P.showNotes(box.querySelector(".dm-notes-slot"), plan, name);
                    shown.tag = tag;
                    shown.fp = major ? plan.major.fingerprint : rt.fingerprint;
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
            P.offerRollback(r.op, name);
            return true;
        }
        if (o.exit === 5) { dlg.result(t("upd.aborted"), false); return true; }
        return false;
    }

    Object.assign(P, { retagOptions, startRetag });
})();

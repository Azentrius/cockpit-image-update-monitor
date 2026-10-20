// SPDX-License-Identifier: Apache-2.0
/* Image Update Monitor, a Cockpit add-on: the Backups tab: the update history
 * with its rollbacks and kept output, the dumps, the rollback images and the
 * cleanup. Part of the page, see common.js. */
(function () {
    "use strict";
    const P = window.ImageUpdatesPage;
    if (!P) return;     // common.js did not load: diun.js asks Cockpit for it again
    const { UPDATE_CMD, t, $, esc, rich, human, fmtDate, spawnHelper, isNoAdmin, label, alertInner,
            errorText, textButton, iconButton, iconSlot, dlg, confirmAndRun, openInstallDialog,
            reasonText, ticket } = P;

    // --- Backups tab -------------------------------------------------------
    let backups = null;

    let backupsError = "";

    function loadBackups() {
        return Promise.resolve().then(() => spawnHelper([UPDATE_CMD, "backups"], "try"))
            .then((out) => { backups = JSON.parse(out); backupsError = ""; renderBackups(); P.refreshFailedBuilds(backups.ops); })
            .catch((ex) => {
                backups = null;
                backupsError = isNoAdmin(ex) ? t("bk.needAdmin")
                    : t("bk.error", { msg: (ex && (ex.message || ex.problem)) || ex });
                renderBackups();
            });
    }

    // The rollback copy of a run that could be rolled back is gone (backups:
    // rollback_gone): deleted by the cleanup, in this tab or outside the add-on.
    function rollbackGone(o) {
        return o.rollback_gone
            ? `<span class="dm-row-note dm-rb-gone" title="${esc(t("rb.goneTip", { ref: o.rollback || "?" }))}">${esc(t("rb.gone"))}</span>`
            : "";
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
        const superseded = ticket();
        const stale = () => superseded() || !$("dm-dialog").hasAttribute("open");
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
        const deps = P.deps();
        const cl = deps && deps.root ? deps.cleanup : null;
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
              (o.kind === "dbmajor" ? `<span class="dm-row-note">${esc(t("op.dbmajor", { from: o.from_major, to: o.to_major }))}</span>` +
                  (o.kept ? `<span class="dm-row-note">${esc(t("op.kept", { major: o.kept.major, where: o.kept.where,
                                                                             size: o.kept.bytes == null ? "?" : human(o.kept.bytes) }))}</span>` : "")
                  : "") +
            `</span></td>` +
            `<td data-label="${L("col.version")}"><span class="dm-cell">${opVersions(o)}</span></td>` +
            `<td data-label="${L("col.result")}"><span class="dm-cell">${opResult(o)}${opReason(o)}${rollbackGone(o)}</span></td>` +
            `<td data-label="${L("col.dumps")}"><span class="dm-cell">${(o.dumps || []).length}</span></td>` +
            `<td class="az-cell-actions"><span class="dm-cell">` +
              (o.can_rollback ? textButton("history", t("rb.button"), `data-op="${i}"`) + iconSlot(logButton(o, i))
                              : logButton(o, i)) +
              (o.kept && o.kept.can_release ? iconButton("trash", t("btn.release"), `data-release="${i}"`) : "") +
            `</span></td></tr>`).join("");
        $("dm-ops-empty").hidden = b.ops.length > 0;
        $("dm-ops-table").hidden = b.ops.length === 0;
        ops.querySelectorAll("button[data-op]").forEach((btn) => {
            btn.addEventListener("click", () => P.startRollback(b.ops[Number(btn.dataset.op)]));
        });
        // The data a database major keeps: released for good (delete-olddata).
        ops.querySelectorAll("button[data-release]").forEach((btn) => {
            const o = b.ops[Number(btn.dataset.release)];
            btn.addEventListener("click", () => confirmAndRun({
                title: t("rel.title"),
                desc: t("rel.desc", { major: o.kept.major, where: o.kept.where,
                                      size: o.kept.bytes == null ? "?" : human(o.kept.bytes) }),
                confirm: { label: t("rel.confirm"), danger: true, argv: () => [UPDATE_CMD, "delete-olddata", o.id] },
                onDone: (r) => { if (r.ok) { dlg.result(t("rel.ok"), true); return true; } return false; },
            }));
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
        P.scrollToPlace();
    }

    Object.assign(P, { loadBackups, startCleanup,
                       backupsState: () => backups ? "ok" : backupsError ? "error" : null });
})();

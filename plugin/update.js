// SPDX-License-Identifier: Apache-2.0
/* Image Update Monitor, a Cockpit add-on: the update (stage 1): its plan, the
 * choice of an update in two steps, the rebuild of an image built on the
 * server, the outcome, and the rollback. Part of the page, see common.js. */
(function () {
    "use strict";
    const P = window.ImageUpdatesPage;
    if (!P) return;     // common.js did not load: diun.js asks Cockpit for it again
    const { UPDATE_CMD, t, $, tagSplit, esc, rich, human, fmtDate, ciJobText, spawnHelper, isNoAdmin,
            label, alertBox, errorText, LEVEL_NAMES, dlg, confirmAndRun, reasonText, ticket } = P;

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
        // The release notes between the two versions, once read (showNotes).
        html += `<div class="dm-notes-slot"></div>`;
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

    // The base images of a rebuild, as the radar checked them (rec.base).
    function basesItem(name) {
        const rec = P.rowRec.get(name);
        const bases = rec && Array.isArray(rec.base) ? rec.base : [];
        if (!bases.length) return "";
        const lines = bases.map((b) => `<code>${esc(b.image + ":" + b.tag)}</code> <span class="dm-muted">` +
            esc(b.pinned ? t("upd.basePinned") : b.error ? t("upd.baseUnchecked")
                : b.newer ? t("upd.baseNewer", { now: b.now || "?" }) : t("upd.baseCurrent")) + `</span>`);
        return `<ul class="dm-kv"><li><span class="dm-kv-k">${esc(t("upd.bases"))}</span>` +
               `<span class="dm-kv-v">${lines.join("<br>")}</span></li></ul>`;
    }

    // kind: "update", "pulled" (the image is here, roll it out) or "rebuild"
    // (an image built on this server, built again with fresh bases).
    function startUpdate(name, kind) {
        const rebuild = kind === "rebuild";
        const title = t(kind === "pulled" ? "upd.titlePulled" : rebuild ? "upd.titleRebuild" : "upd.title", { name });
        const superseded = ticket();
        // A plan that arrives after the dialog was closed (or after another
        // container's dialog was opened) must not pop the dialog up again.
        const stale = () => superseded() || !$("dm-dialog").hasAttribute("open");
        dlg.open({ title, desc: t("upd.planning") });
        spawnHelper(rebuild ? [UPDATE_CMD, "rebuild", name, "--dry-run"] : [UPDATE_CMD, "plan", name], "try")
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
                const desc = t(rebuild ? "upd.descRebuild" : kind === "pulled" || plan.pulled ? "upd.descPulled" : "upd.desc",
                               { image: plan.image });
                const failed = rebuild ? null : P.rowFailed.get(name) || null;
                // Update in two steps: only fetch the image now and roll it out
                // later. Not for an image that is here already: that is the roll-out.
                const twoSteps = !rebuild && kind !== "pulled" && !plan.pulled;
                const pullOnly = () => { const r = $("dm-dialog-body").querySelector('input[name="dm-step"]:checked'); return !!r && r.value === "pull"; };
                const label = () => t(rebuild ? "btn.rebuild" : kind === "pulled" ? "btn.rollout" : pullOnly() ? "btn.pullOnly" : "btn.update");
                confirmAndRun({
                    title, desc, body: (rebuild ? basesItem(name) : "") + planBody(plan, failed) + (twoSteps ? stepChoice() : ""),
                    confirm: {
                        label: label(),
                        disabled: !!plan.jump_unknown || !!failed || plan.jump === "major" || (plan.ci_jobs || []).length > 0,
                        argv: () => rebuild ? [UPDATE_CMD, "rebuild", name].concat(dumpArgs())
                            : pullOnly() ? [UPDATE_CMD, "pull", name] : [UPDATE_CMD, "update", name].concat(dumpArgs()),
                    },
                    onDone: (o) => updateOutcome(o, name, rebuild),
                });
                const gates = [...$("dm-dialog-body").querySelectorAll(".dm-gate")];
                const relabel = () => dlg.btn("dm-dialog-confirm", label(), { visible: true, disabled: !gates.every((x) => x.checked) });
                gates.forEach((g) => g.addEventListener("change", relabel));
                $("dm-dialog-body").querySelectorAll('input[name="dm-step"]').forEach((r) => r.addEventListener("change", () => {
                    const dump = $("dm-dump-check");
                    if (dump) dump.disabled = pullOnly();
                    relabel();
                }));
                // Release notes belong to a registry's image, not to a build.
                if (!rebuild) P.showNotes($("dm-dialog-body").querySelector(".dm-notes-slot"), plan, name);
            })
            .catch((ex) => {
                if (stale()) return;
                dlg.update({ title, desc: isNoAdmin(ex) ? t("dialog.noAdmin")
                                                       : t("dialog.error", { msg: (ex && (ex.message || ex.problem)) || ex }) });
            });
    }

    // The choice of an update in two steps, below the plan and its warnings.
    function stepChoice() {
        return `<fieldset class="dm-steps"><legend>${esc(t("upd.stepTitle"))}</legend>` +
            `<label class="dm-radio"><input type="radio" name="dm-step" value="now" checked> ${esc(t("upd.stepNow"))}</label>` +
            `<label class="dm-radio"><input type="radio" name="dm-step" value="pull"> ${esc(t("upd.stepPull"))}</label>` +
            `</fieldset>`;
    }

    function updateOutcome(o, name, rebuild) {
        const r = o.result || {};
        if (o.ok && r.status === "nochange") { dlg.result(t(rebuild ? "upd.nochangeRebuild" : "upd.nochange"), true); return true; }
        if (o.ok && r.status === "pulled") {
            dlg.result(t("upd.pulledOk", { v: r.to_version ? ` (${r.to_version})` : "" }), true);
            return true;
        }
        if (o.ok) {
            const vers = r.from_version && r.to_version && r.from_version !== r.to_version;
            dlg.result(t(rebuild ? (vers ? "upd.okRebuildVersions" : "upd.okRebuild") : vers ? "upd.okVersions" : "upd.ok",
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
                P.safeLoad();
            });
        };
    }

    function startRollback(op) {
        const vars = { image: op.image, rb: op.rollback, services: (op.services || []).join(", "),
                       files: [...new Set((op.changes || []).map((c) => c.file))].join(", ") };
        confirmAndRun({
            title: t("rb.title", { name: op.container }),
            desc: t(op.kind === "retag" ? "rb.descRetag" : op.kind === "dbmajor" ? "rb.descMajor" : "rb.desc",
                    { ...vars, from: op.from_major, to: op.to_major }),
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

    Object.assign(P, { planBody, dumpArgs, startUpdate, offerRollback, startRollback });
})();

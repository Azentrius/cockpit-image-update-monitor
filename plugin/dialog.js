// SPDX-License-Identifier: Apache-2.0
/* Image Update Monitor, a Cockpit add-on: the one action dialog (install a
 * requirement, update, version change, rollback, delete, cleanup, the kept
 * output), its run with the live output and the most likely cause of a
 * failure. Part of the page, see common.js. */
(function () {
    "use strict";
    const P = window.ImageUpdatesPage;
    const { DEPS_CMD, t, icon, $, esc, rich, spawnHelper, isNoAdmin, label } = P;

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
                : t(r.source === "error" || r.source === "signal" ? "upd.reasonErrorTitle" : "upd.reasonComposeTitle");
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
                P.safeLoad();   // refresh everything after any root action
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

    // Text of a failure reason (@@RESULT / history): the log line, otherwise
    // what the container's state tells.
    function reasonText(r) {
        if (r.line) return r.source === "health" ? t("upd.reasonHealth", { line: r.line }) : r.line;
        if (r.oom) return t("upd.reasonOom");
        if (r.state === "running" || r.state === "starting") return t("upd.reasonTimeout");
        if (r.exit_code) return t("upd.reasonExit", { code: r.exit_code });
        return t("upd.reasonNone");
    }

    // A ticket for an answer that comes later (a plan, a preview, a log): it is
    // stale once another dialog took a ticket.
    let planSeq = 0;
    function ticket() {
        const mine = ++planSeq;
        return () => mine !== planSeq;
    }

    Object.assign(P, { dlg, confirmAndRun, openInstallDialog, reasonText, ticket });
})();

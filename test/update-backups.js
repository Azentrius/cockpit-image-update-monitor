// SPDX-License-Identifier: Apache-2.0
// Update flow (plan -> confirm -> live log -> result / rollback), the
// warning for an unknown jump size, the most likely cause of a failure, a
// build that failed before, the kept output and the backups tab (history,
// dumps, rollback images, delete), against the real diun.js in jsdom with a
// scripted Cockpit stub.
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { JSDOM } = require("jsdom");

const PLUGIN = path.join(__dirname, "..", "plugin");
const FIX = path.join(__dirname, "fixtures");
const html = fs.readFileSync(path.join(PLUGIN, "index.html"), "utf8").replace(/<script[^>]*><\/script>/g, "");
// The page as index.html loads it: the texts (i18n.js), then the code (diun.js).
const js = ["i18n.js", "diun.js"].map((f) => fs.readFileSync(path.join(PLUGIN, f), "utf8")).join("\n");
const inventory = fs.readFileSync(path.join(FIX, "inventory3.jsonl"), "utf8");
const files = {
    "/var/lib/diun/events.jsonl": fs.readFileSync(path.join(FIX, "events.jsonl"), "utf8"),
    "/var/lib/diun-radar/versions.json": JSON.parse(fs.readFileSync(path.join(FIX, "versions2.json"), "utf8")),
    "/var/lib/diun/dismissed.json": null,
};
const DEPS_OK = JSON.stringify({ root: true, docker: { state: "ok", compose: true },
    diun: { state: "running", events: true }, skopeo: { state: "ok" }, radar: { state: "active" } });
const PLAN_OLLAMA = { container: "ollama", image: "ollama/ollama:latest", updatable: true, project: "actual",
    workdir: "/opt/actual", services: ["ollama"], containers: ["ollama"], pulled: false, config_changed: true,
    dbs: [{ name: "actual-db", kind: "postgres", running: true, size: 52428800 }], free_bytes: 40 * 2 ** 30 };
const PLAN_GLITCH = { container: "glitchtip-postgres", image: "postgres:16-alpine", updatable: true, project: "glitchtip",
    workdir: "/opt/glitchtip", services: ["postgres"], containers: ["glitchtip-postgres"], pulled: true, config_changed: false,
    dbs: [{ name: "glitchtip-postgres", kind: "postgres", running: true, size: 1.2e8 },
          { name: "glitchtip-redis", kind: "unsupported", running: true, size: null }], free_bytes: 40 * 2 ** 30 };
const PLAN_JUMP = { ...PLAN_OLLAMA, config_changed: false, installed_version: null, tag_digits: 0, jump_unknown: true };
const REASON = { container: "formbricks-app", state: "restarting", exit_code: 1, oom: false, source: "log",
    line: "Error: Invalid environment variables: - AUTHZED_ENABLED: Formbricks v6 requires AUTHZED_ENABLED=true" };
const BACKUPS = {
    dir: "/var/backups/image-updates", free_bytes: 40 * 2 ** 30, dump_bytes: 3 * 2 ** 20,
    dumps: [{ path: "glitchtip/20261012-031500_glitchtip-postgres.sql.gz", project: "glitchtip",
              container: "glitchtip-postgres", size: 3 * 2 ** 20, mtime: 1791768900 }],
    images: [{ ref: "postgres:rollback-20261012-031500", id: "sha256:old", size: 274993152, created: "2026-10-12T03:15:00+02:00", in_use: false },
             { ref: "ollama/ollama:rollback-20261011-120000", id: "sha256:x", size: 3650722201, created: "2026-10-11T12:00:00+02:00", in_use: true }],
    ops: [{ id: "20261012-031500-glitchtip-postgres", ts: "2026-10-12T01:15:00Z", kind: "update", container: "glitchtip-postgres",
            image: "postgres:16-alpine", services: ["postgres"], rollback: "postgres:rollback-20261012-031500",
            dumps: ["glitchtip/20261012-031500_glitchtip-postgres.sql.gz"], status: "ok", can_rollback: true },
          { id: "20261011-120000-ollama", ts: "2026-10-11T10:00:00Z", kind: "update", container: "ollama",
            image: "ollama/ollama:latest", services: ["ollama"], rollback: null, dumps: [], status: "nochange", can_rollback: false },
          { id: "20261012-232502-formbricks-app", ts: "2026-10-10T23:25:02Z", kind: "update", container: "formbricks-app",
            image: "ghcr.io/formbricks/formbricks:latest", services: ["formbricks"], rollback: "x", dumps: [], status: "failed",
            can_rollback: false, rolled_back: true, rollback_status: "ok", reason: REASON },
          { id: "20261009-120000-x", ts: "2026-10-09T10:00:00Z", kind: "update", container: "x", image: "x:latest",
            services: ["x"], rollback: "x", dumps: [], status: "failed", can_rollback: false,
            reason: { container: "x", state: "exited", exit_code: 137, oom: true, source: null, line: null } },
          { id: "20261008-120000-y", ts: "2026-10-08T10:00:00Z", kind: "update", container: "y", image: "y:latest",
            services: ["y"], rollback: "y", dumps: [], status: "ok", can_rollback: false, rolled_back: true,
            rollback_status: "failed",
            rollback_reason: { container: "y", state: "unhealthy", exit_code: 0, oom: false, source: "health",
                               line: "curl: (7) Failed to connect to localhost port 8000" } }],
};
// Answers of the stubbed helpers that have not arrived yet, for every page of this file.
const pending = new Set();
// Waits until every stubbed answer has arrived and the page has handled it (also the calls
// those answers lead to), instead of a fixed number of milliseconds. Fails after a bounded
// number of rounds, so a page that keeps calling cannot hang the test.
async function settle() {
    for (let round = 0; round < 500; round++) {
        if (pending.size) await Promise.all([...pending]);
        await new Promise((r) => setImmediate(r));
        if (!pending.size) return;
    }
    throw new Error("the page did not settle: helper calls keep coming");
}
const kvRows = (el) => [...el.querySelectorAll(".dm-kv li")].map((li) => [...li.children].map((x) => x.textContent).join(": "));

function boot(language, script) {
    const dom = new JSDOM(html, { runScripts: "outside-only" });
    const w = dom.window;
    w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
    w.HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
    w.Date.now = () => Date.parse("2026-10-12T02:50:00Z");
    const calls = [];
    w.cockpit = {
        language,
        file(p) { return { read: () => Promise.resolve(files[p] ?? null), watch: (cb) => cb(files[p], "t"), replace: () => Promise.resolve() }; },
        spawn(cmd, opts) {
            calls.push({ cmd: JSON.parse(JSON.stringify(cmd)), opts: JSON.parse(JSON.stringify(opts || {})) });
            const verb = cmd[0].split("/").pop() + " " + (cmd[1] || "");
            const s = script[verb.trim()] || script[cmd[0].split("/").pop()];
            if (!s) return Promise.reject(new Error("unscripted " + verb));
            const r = typeof s === "function" ? s(cmd) : s;
            let cb = null;
            const p = new Promise((resolve, reject) => setTimeout(() => {
                (r.chunks || []).forEach((c) => cb && cb(c));
                r.fail ? reject(r.fail) : resolve(r.out ?? "");
            }, 10));
            p.stream = (f) => { cb = f; return p; };
            const done = p.then(() => {}, () => {});
            pending.add(done);
            done.then(() => pending.delete(done));
            return p;
        },
        transport: { wait() {} },
    };
    w.eval(js);
    const d = w.document;
    return { w, d, calls, last: (verb) => [...calls].reverse().find((c) => c.cmd[1] === verb) };
}
const BASE_SCRIPT = {
    "diun-inventory.py": { out: inventory },
    "diun-deps.sh check": { out: DEPS_OK },
    "diun-update.py backups": { out: JSON.stringify(BACKUPS) },
};

(async () => {
    // 1. Rows: buttons per kind
    const a = boot("de", BASE_SCRIPT);
    await settle();
    const rowOf = (d, name) => [...d.querySelectorAll("#dm-rows tr")].find((tr) => tr.querySelector(".dm-name").textContent === name);
    const describe = (tr) => [...tr.querySelectorAll("td")].map((td) => td.textContent.trim()).filter(Boolean).join(" | ");
    for (const n of ["ollama", "glitchtip-postgres", "actual-ai", "calcom"]) console.log("row:", describe(rowOf(a.d, n)));
    assert(rowOf(a.d, "ollama").querySelector('button[data-update="ollama"]').textContent === "Update");
    assert(rowOf(a.d, "glitchtip-postgres").querySelector("button[data-update]").textContent === "Image ausrollen");
    assert(rowOf(a.d, "glitchtip-postgres").textContent.includes("Ausrollen ausstehend"));
    const together = (n) => { const x = rowOf(a.d, n).querySelector("td .dm-row-note"); return x && x.textContent; };
    console.log("together:", ["glitchtip-web", "hbbr", "nextcloud-family-cron-1", "ollama"].map((n) => `${n} -> ${together(n)}`).join(" | "));
    assert.strictEqual(together("glitchtip-web"), "zusammen mit glitchtip-worker");
    assert.strictEqual(together("hbbr"), "zusammen mit hbbs");
    assert.strictEqual(together("ollama"), null, "alone in its project with its image");
    // Same project and image, but started from another compose file (a
    // runner in docker-compose.emu.yml next to docker-compose.yml, 2026-10-16):
    // an update does not take it along, so it is not "together with".
    const split = inventory.trim().split("\n").map((l) => JSON.parse(l)).map((c) => c.project !== "rustdesk" ? c
        : { ...c, files: [c.name === "hbbr" ? "/opt/rustdesk/relay.yml" : "/opt/rustdesk/compose.yml"] });
    const sp = boot("de", { ...BASE_SCRIPT, "diun-inventory.py": { out: split.map((c) => JSON.stringify(c)).join("\n") + "\n" } });
    await settle();
    const togetherIn = (d, n) => { const x = rowOf(d, n).querySelector("td .dm-row-note"); return x && x.textContent; };
    assert.deepStrictEqual([togetherIn(sp.d, "hbbr"), togetherIn(sp.d, "hbbs"), togetherIn(sp.d, "glitchtip-web")],
                           [null, null, "zusammen mit glitchtip-worker"], "other compose files: not together");
    // Containers of CI jobs (Forgejo runner, a CI host, 2026-10-17) are not
    // listed: the meta line counts them, its tooltip names them.
    const JOB = { name: "FORGEJO-ACTIONS-TASK-2415_WORKFLOW-a545a355_JOB-unit", image: "example-ci", tag: "1", digest: "",
                  imgid: "sha256:ci", tagid: "", pulled: false, project: "", service: "", managed: false, files: [], ci_job: true };
    for (const [lang, word] of [["de", "1 CI-Job ausgeblendet"], ["en", "1 CI job hidden"]]) {
        const cj = boot(lang, { ...BASE_SCRIPT, "diun-inventory.py": { out: inventory + JSON.stringify(JOB) + "\n" } });
        await settle();
        const meta = cj.d.getElementById("dm-meta");
        console.log("CI jobs " + lang + ":", meta.textContent, "|", meta.title.replace(/\n/g, " / "));
        assert.strictEqual(rowOf(cj.d, JOB.name), undefined, "not a row");
        assert(meta.textContent.includes(word) && meta.textContent.includes(lang === "de" ? "31 Container" : "31 containers"), meta.textContent);
        assert(meta.title.endsWith("\nunit (2415)"), meta.title);
    }
    assert(!a.d.getElementById("dm-meta").textContent.includes("CI") && !a.d.getElementById("dm-meta").title, "no CI jobs: no word");
    console.log("meta:", a.d.getElementById("dm-meta").textContent);
    // The buttons line up from row to row: a missing icon button keeps its place,
    // invisible and as large as the button (2026-10-16: in rows without "Change
    // version" the Update button stood further right).
    const places = (tr) => [...tr.querySelector("td.az-cell-actions .dm-cell").children].map((x) =>
        x.classList.contains("dm-slot") ? "slot" : x.dataset.update ? "update" : x.dataset.retag ? "retag"
            : x.dataset.key ? "dismiss" : x.dataset.op ? "rollback" : x.dataset.log ? "log" : x.tagName);
    const PLACES = { "ollama": ["update", "retag", "dismiss"], "prod-mcp-1": ["update", "slot", "dismiss"],
                     "glitchtip-postgres": ["update", "retag", "slot"], "docuseal": ["retag", "slot"], "calcom": [] };
    console.log("places:", Object.keys(PLACES).map((n) => `${n} = ${places(rowOf(a.d, n))}`).join(" | "));
    for (const [n, want] of Object.entries(PLACES)) assert.deepStrictEqual(places(rowOf(a.d, n)), want, n);
    const slot = rowOf(a.d, "prod-mcp-1").querySelector(".dm-slot");
    assert(slot.getAttribute("aria-hidden") === "true" && slot.classList.contains("az-btn--plain") &&
           slot.classList.contains("az-btn--sm"), "hidden from screen readers, sized like an icon button");

    // 2. Update ollama: plan -> body -> confirm -> stream -> success
    const runOk = { chunks: ["Update ollama/ollama:latest …\n", "+ docker compose … pull\n",
                             '@@RESULT {"op":"20261012-032000-ollama","status":"ok","rollback":"ollama/ollama:rollback-20261012-032000"}\n'] };
    const b = boot("de", { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify(PLAN_OLLAMA) },
                                           "diun-update.py update": runOk });
    await settle();
    b.d.querySelector('button[data-update="ollama"]').click();
    await settle();
    const body = b.d.getElementById("dm-dialog-body").textContent.replace(/\s+/g, " ");
    console.log("\ndialog:", b.d.getElementById("dm-dialog-title").textContent, "|", b.d.getElementById("dm-dialog-desc").textContent);
    console.log("body  :", body);
    assert(b.d.getElementById("dm-dump-check").checked);
    assert(body.includes("Compose-Datei wurde seit dem Start"));
    assert(!b.d.getElementById("dm-jump-check") && !body.includes("Installierte Version"),
           "an older plan without version fields: no warning, no row");
    b.d.getElementById("dm-dialog-confirm").click();
    await settle();
    const up = b.last("update");
    console.log("spawn :", JSON.stringify(up.cmd), up.opts.superuser);
    assert.strictEqual(JSON.stringify(up.cmd), JSON.stringify(["/usr/lib/cockpit-image-updates/diun-update.py", "update", "ollama"]));
    const log = b.d.getElementById("dm-dialog-log").textContent;
    console.log("log   :", JSON.stringify(log));
    assert(!log.includes("@@RESULT"));
    console.log("result:", b.d.getElementById("dm-dialog-result").textContent);
    assert.strictEqual(b.d.getElementById("dm-dialog-result").textContent, "Update erfolgreich. Rollback-Kopie: ollama/ollama:rollback-20261012-032000");

    // 3. Without dump -> --no-dump; failure -> rollback offered -> rollback runs
    const c = boot("de", { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify(PLAN_GLITCH) },
        "diun-update.py update": { chunks: ["…\n", '@@RESULT {"op":"20261012-033000-glitchtip-postgres","status":"failed"}\n'], fail: { exit_status: 4 } },
        "diun-update.py rollback": { chunks: ["Rollback …\n", '@@RESULT {"op":"20261012-033000-glitchtip-postgres","status":"ok"}\n'] } });
    await settle();
    c.d.querySelector('button[data-update="glitchtip-postgres"]').click();
    await settle();
    console.log("\ndialog:", c.d.getElementById("dm-dialog-title").textContent, "|", c.d.getElementById("dm-dialog-desc").textContent);
    c.d.getElementById("dm-dump-check").checked = false;
    c.d.getElementById("dm-dialog-confirm").click();
    await settle();
    console.log("spawn :", JSON.stringify(c.last("update").cmd.slice(1)));
    assert.strictEqual(JSON.stringify(c.last("update").cmd.slice(1)), JSON.stringify(["update", "glitchtip-postgres", "--no-dump"]));
    const extra = c.d.getElementById("dm-dialog-extra");
    console.log("result:", c.d.getElementById("dm-dialog-result").textContent, "| extra:", !extra.hidden, extra.textContent.trim());
    assert(!extra.hidden && extra.textContent.trim() === "Update zurückrollen");
    extra.click();
    await settle();
    console.log("spawn :", JSON.stringify(c.last("rollback").cmd.slice(1)), "| result:", c.d.getElementById("dm-dialog-result").textContent);
    assert.strictEqual(c.d.getElementById("dm-dialog-result").textContent, "Rollback erfolgreich.");

    // 3a. No database to dump: no tick box (it stood there greyed out), and no
    // --no-dump either; the helper says itself that there is nothing to dump
    for (const dbs of [[], [{ name: "cache", kind: "unsupported", running: true, size: null }]]) {
        const nd = boot("de", { ...BASE_SCRIPT, "diun-update.py update": runOk,
            "diun-update.py plan": { out: JSON.stringify({ ...PLAN_OLLAMA, config_changed: false, dbs }) } });
        await settle();
        nd.d.querySelector('button[data-update="ollama"]').click();
        await settle();
        const ndBody = nd.d.getElementById("dm-dialog-body");
        console.log("no db :", kvRows(ndBody).find((r) => r.startsWith("Datenbank-Dump")), "| tick box:",
                    !!nd.d.getElementById("dm-dump-check"));
        assert(!nd.d.getElementById("dm-dump-check") && !ndBody.querySelector(".dm-check"));
        nd.d.getElementById("dm-dialog-confirm").click();
        await settle();
        assert.deepStrictEqual(nd.last("update").cmd.slice(1), ["update", "ollama"]);
    }

    // 3b. Dialog closed while the plan is still loading -> must stay closed
    const s = boot("de", { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify(PLAN_OLLAMA) } });
    await settle();
    s.d.querySelector('button[data-update="ollama"]').click();
    s.d.getElementById("dm-dialog-cancel").click();
    await settle();
    console.log("\nclosed during plan -> open:", s.d.getElementById("dm-dialog").hasAttribute("open"));
    assert(!s.d.getElementById("dm-dialog").hasAttribute("open"));

    // 4. Not updatable / no admin
    const e = boot("de", { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify({ ...PLAN_OLLAMA, updatable: false, reason: "not-compose" }) } });
    await settle();
    e.d.querySelector('button[data-update="ollama"]').click();
    await settle();
    console.log("\nnot updatable:", e.d.getElementById("dm-dialog-desc").textContent, "| confirm hidden:", e.d.getElementById("dm-dialog-confirm").hidden);
    assert(e.d.getElementById("dm-dialog-confirm").hidden);
    for (const [lang, want] of [["de", "Die Compose-Dateien, aus denen der Container gestartet wurde, enthalten runner-2 nicht (mehr)."],
                                ["en", "the compose files the container was started from do not define runner-2 (any more)."]]) {
        const m = boot(lang, { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify({ ...PLAN_OLLAMA, updatable: false,
            reason: "service-missing", missing: ["runner-2"] }) } });
        await settle();
        m.d.querySelector('button[data-update="ollama"]').click();
        await settle();
        const desc = m.d.getElementById("dm-dialog-desc").textContent;
        assert(desc.includes(want) && m.d.getElementById("dm-dialog-confirm").hidden, desc);
    }
    const f = boot("en", { ...BASE_SCRIPT, "diun-update.py plan": { fail: { problem: "access-denied" } } });
    await settle();
    f.d.querySelector('button[data-update="ollama"]').click();
    await settle();
    console.log("no admin     :", f.d.getElementById("dm-dialog-desc").textContent);
    assert(f.d.getElementById("dm-dialog-desc").textContent.startsWith("Denied"));

    // 5. Backups tab
    const g = boot("de", { ...BASE_SCRIPT,
        "diun-update.py delete-dump": { chunks: ["Gelöscht: …\n"] },
        "diun-update.py rollback": { chunks: ['@@RESULT {"status":"ok"}\n'] } });
    await settle();
    g.d.getElementById("dm-tab-btn-backups").click();
    await settle();
    assert(!g.d.getElementById("dm-tab-backups").hidden && g.d.getElementById("dm-tab-updates").hidden);
    console.log("\nbackups summary:", g.d.getElementById("dm-bk-summary").textContent);
    ["dm-ops", "dm-dumps", "dm-images"].forEach((id) =>
        [...g.d.querySelectorAll(`#${id} tr`)].forEach((tr) => console.log(`  ${id.slice(3).padEnd(6)}`, describe(tr))));
    const opPlaces = [...g.d.querySelectorAll("#dm-ops tr")].map(places);
    assert.deepStrictEqual(opPlaces[0], ["rollback", "slot"], "no kept output: its button's place stays");
    assert.deepStrictEqual(opPlaces[1], [], "nothing to do: no places");
    const inUseBtn = g.d.querySelector('#dm-images button[data-img="1"]');
    assert(inUseBtn.disabled, "in-use image cannot be deleted");
    const sizes = [...g.d.querySelectorAll('#dm-images td[data-label="Größe"]')].map((x) => x.textContent);
    assert.deepStrictEqual(sizes, ["262,25 MiB", "3,40 GiB"], "bytes from the helper, on a 1024 base");
    assert.deepStrictEqual([g.d.getElementById("dm-bk-error").hidden, g.d.getElementById("dm-bk-summary").hidden], [true, false]);
    g.d.querySelector('#dm-dumps button[data-dump="0"]').click();
    console.log("delete dialog:", g.d.getElementById("dm-dialog-title").textContent, "|", g.d.getElementById("dm-dialog-desc").textContent);
    assert.deepStrictEqual([g.d.querySelector("#dm-dialog-confirm .dm-btn-label").textContent,
                            g.d.querySelector("#dm-dialog-desc code").textContent],
                           ["Dump löschen", "glitchtip/20261012-031500_glitchtip-postgres.sql.gz"]);
    g.d.getElementById("dm-dialog-confirm").click();
    await settle();
    console.log("spawn :", JSON.stringify(g.last("delete-dump").cmd.slice(1)), g.last("delete-dump").opts.superuser,
                "| result:", g.d.getElementById("dm-dialog-result").textContent);
    assert.strictEqual(g.last("delete-dump").cmd[2], "glitchtip/20261012-031500_glitchtip-postgres.sql.gz");
    g.d.getElementById("dm-dialog-cancel").click();
    g.d.querySelector('#dm-ops button[data-op="0"]').click();
    console.log("rollback dialog:", g.d.getElementById("dm-dialog-title").textContent, "|", g.d.getElementById("dm-dialog-desc").textContent);
    g.d.getElementById("dm-dialog-confirm").click();
    await settle();
    assert.strictEqual(JSON.stringify(g.last("rollback").cmd.slice(1)), JSON.stringify(["rollback", "20261012-031500-glitchtip-postgres"]));
    console.log("result:", g.d.getElementById("dm-dialog-result").textContent);
    const causes = [...g.d.querySelectorAll("#dm-ops .dm-op-reason")].map((x) => x.textContent);
    console.log("causes:", causes);
    assert.deepStrictEqual(causes, ["formbricks-app: " + REASON.line, "x: Wegen Speichermangels beendet.",
                                    "y: Health-Check: curl: (7) Failed to connect to localhost port 8000"]);
    assert.strictEqual(g.d.querySelector("#dm-ops .dm-op-reason").title, "formbricks-app: " + REASON.line);
    // the backups cannot be loaded: an error alert above the cards instead of the summary
    for (const [fail, body] of [[{ problem: "access-denied" }, "Die Backup-Übersicht braucht administrativen Zugriff: Schalten Sie ihn oben in Cockpit ein."],
                                [{ message: "docker: not found" }, "Prüfen Sie, ob Docker auf diesem Server läuft, und wählen Sie dann „Aktualisieren“. Details: docker: not found"]]) {
        const bf = boot("de", { ...BASE_SCRIPT, "diun-update.py backups": { fail } });
        await settle();
        const err = bf.d.getElementById("dm-bk-error");
        console.log("backups fail:", err.textContent);
        assert.deepStrictEqual([err.hidden, err.getAttribute("role"), err.classList.contains("az-alert--danger"),
                                err.querySelector(".az-alert__title").textContent, err.querySelector(".az-alert__body").textContent,
                                bf.d.getElementById("dm-bk-summary").hidden],
                               [false, "alert", true, "Die Backups konnten nicht geladen werden", body, true]);
        assert(!bf.d.getElementById("dm-ops-empty").closest(".az-card:not([hidden])"), "no empty states that would claim there is nothing");
    }
    assert(!g.d.getElementById("dm-ops-title").closest(".az-card").hidden, "with data the cards show");

    // 7. Unknown jump size: the warning comes first, Update waits for the tick box
    const j = boot("de", { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify(PLAN_JUMP) },
                                           "diun-update.py update": runOk });
    await settle();
    j.d.querySelector('button[data-update="ollama"]').click();
    await settle();
    const jbody = j.d.getElementById("dm-dialog-body");
    console.log("\njump  :", jbody.textContent.replace(/\s+/g, " ").slice(0, 150), "…");
    const first = jbody.firstElementChild;
    assert(first.classList.contains("dm-gate-group"), "warning first, together with its tick box");
    const warnAlert = first.firstElementChild;
    assert(warnAlert.classList.contains("az-alert") && warnAlert.classList.contains("az-alert--warning"), "a warning alert");
    assert.strictEqual(warnAlert.querySelector(".az-alert__title").textContent, "Größe des Sprungs unbekannt");
    assert(warnAlert.querySelector(".az-alert__body").textContent.startsWith("Die installierte Version ist unbekannt, und der Tag latest folgt"));
    assert(first.querySelector(".dm-check #dm-jump-check"), "the tick box in the same group");
    assert(kvRows(jbody).includes("Installierte Version: unbekannt"));
    const jc = j.d.getElementById("dm-dialog-confirm");
    const tick = j.d.getElementById("dm-jump-check");
    const toggle = (on) => { tick.checked = on; tick.dispatchEvent(new j.w.Event("change")); };
    assert(jc.disabled && !tick.checked, "Update waits for the tick box");
    toggle(true);
    assert(!jc.disabled && jc.textContent.trim() === "Update");
    toggle(false);
    assert(jc.disabled, "untick: disabled again");
    toggle(true);
    jc.click();
    await settle();
    assert.strictEqual(j.last("update").cmd[2], "ollama");
    console.log("jump  : confirmed after the tick box ->", JSON.stringify(j.last("update").cmd.slice(1)));
    const kn = boot("de", { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify(
        { ...PLAN_OLLAMA, installed_version: "0.30.11", tag_digits: 0, jump_unknown: false }) } });
    await settle();
    kn.d.querySelector('button[data-update="ollama"]').click();
    await settle();
    assert(!kn.d.getElementById("dm-jump-check") && !kn.d.getElementById("dm-dialog-confirm").disabled);
    assert(kvRows(kn.d.getElementById("dm-dialog-body")).includes("Installierte Version: 0.30.11"));

    // 8. Failed update: the most likely cause above the log, the rollback next to it
    const k = boot("de", { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify(PLAN_GLITCH) },
        "diun-update.py update": { chunks: ["…\n", "@@RESULT " + JSON.stringify(
            { op: "20261012-232502-formbricks-app", status: "failed", reason: REASON }) + "\n"], fail: { exit_status: 4 } },
        "diun-update.py rollback": { chunks: ["Rollback …\n", '@@RESULT {"op":"20261012-232502-formbricks-app","status":"ok"}\n'] } });
    await settle();
    k.d.querySelector('button[data-update="glitchtip-postgres"]').click();
    await settle();
    const box = k.d.getElementById("dm-dialog-reason");
    assert(box.hidden, "no cause before the run");
    k.d.getElementById("dm-dialog-confirm").click();
    await settle();
    console.log("\ncause :", box.textContent.replace(/\s+/g, " ").trim());
    assert(!box.hidden && box.classList.contains("az-alert--danger"));
    assert.strictEqual(box.querySelector(".az-alert__title").textContent, "Vermutliche Ursache (formbricks-app)");
    assert.strictEqual(box.querySelector(".dm-reason-line").textContent, REASON.line);
    assert(box.compareDocumentPosition(k.d.getElementById("dm-dialog-log")) & k.w.Node.DOCUMENT_POSITION_FOLLOWING,
           "the cause stands above the log");
    assert(!k.d.getElementById("dm-dialog-extra").hidden, "rollback offered");
    k.d.getElementById("dm-dialog-extra").click();
    await settle();
    assert(box.hidden, "a new run hides the old cause");
    assert.strictEqual(k.d.getElementById("dm-dialog-result").textContent, "Rollback erfolgreich.");
    // a failed rollback names its own cause; without a log line the state speaks
    const m = boot("de", { ...BASE_SCRIPT,
        "diun-update.py rollback": { chunks: ["…\n", "@@RESULT " + JSON.stringify({ op: "x", status: "failed",
            reason: { container: "glitchtip-postgres", state: "starting", exit_code: 0, oom: false, source: null, line: null } }) + "\n"],
            fail: { exit_status: 4 } } });
    await settle();
    m.d.getElementById("dm-tab-btn-backups").click();
    await settle();
    m.d.querySelector('#dm-ops button[data-op="0"]').click();
    m.d.getElementById("dm-dialog-confirm").click();
    await settle();
    console.log("rollback cause:", m.d.getElementById("dm-dialog-reason").textContent.replace(/\s+/g, " ").trim());
    assert.strictEqual(m.d.querySelector("#dm-dialog-reason .dm-reason-line").textContent,
                       "In der Wartezeit nicht bereit geworden; im Log steht keine Fehlerzeile.");
    // A failed rollback stays "rollback failed" and is offered again (up to 0.18.0 the
    // helper marked it rolled_back). An update that ended without its last line
    // ("running") shows as aborted, with its rollback; one whose process still runs
    // as running, without. The helper's own failure has a title of its own.
    const RB = (id) => id + ":rollback-20261012-060000";
    const RETRY_OPS = [
        { id: "20261012-060000-a", ts: "2026-10-12T04:00:00Z", kind: "update", container: "a", image: "a:latest",
          services: ["a"], rollback: RB("a"), dumps: [], status: "ok", can_rollback: true, rolled_back: false,
          rollback_status: "failed", rollback_reason: { container: "a", state: null, exit_code: null, oom: false,
              source: "image", line: "läuft auf elsewhere statt auf der Rollback-Kopie " + RB("a") + " (old)" } },
        { id: "20261012-050000-b", ts: "2026-10-12T03:00:00Z", kind: "update", container: "b", image: "b:latest",
          services: ["b"], rollback: RB("b"), dumps: [], status: "running", active: false, can_rollback: true },
        { id: "20261012-040000-c", ts: "2026-10-12T02:00:00Z", kind: "update", container: "c", image: "c:latest",
          services: ["c"], rollback: RB("c"), dumps: [], status: "running", active: true, can_rollback: false },
        { id: "20261012-030000-d", ts: "2026-10-12T01:00:00Z", kind: "update", container: "d", image: "d:latest",
          services: ["d"], rollback: RB("d"), dumps: [], status: "failed", can_rollback: true,
          reason: { container: null, state: null, exit_code: null, oom: false, source: "error",
                    line: "Prüfung nach dem Neustart abgebrochen: Cannot connect to the Docker daemon" } }];
    const rt = boot("de", { ...BASE_SCRIPT, "diun-update.py backups": { out: JSON.stringify({ ...BACKUPS, ops: RETRY_OPS }) },
        "diun-update.py rollback": { chunks: ["…\n", "@@RESULT " + JSON.stringify({ op: "20261012-030000-d", status: "failed",
            reason: RETRY_OPS[3].reason }) + "\n"], fail: { exit_status: 4 } } });
    await settle();
    rt.d.getElementById("dm-tab-btn-backups").click();
    await settle();
    const rtRows = [...rt.d.querySelectorAll("#dm-ops tr")].map((tr) =>
        [tr.querySelector('td[data-label="Ergebnis"] .az-label').textContent, !!tr.querySelector("button[data-op]"),
         (tr.querySelector(".dm-op-reason") || {}).textContent || null]);
    console.log("\nretry rows:", JSON.stringify(rtRows));
    assert.deepStrictEqual(rtRows, [
        ["Rollback fehlgeschlagen", true, "a: " + RETRY_OPS[0].rollback_reason.line],
        ["abgebrochen", true, null],
        ["läuft", false, null],
        ["fehlgeschlagen", true, RETRY_OPS[3].reason.line]]);
    rt.d.querySelector('#dm-ops button[data-op="3"]').click();
    rt.d.getElementById("dm-dialog-confirm").click();
    await settle();
    assert.strictEqual(rt.d.querySelector("#dm-dialog-reason .az-alert__title").textContent, "Ursache (Update-Helfer)");
    // A signal to the helper (source "signal") is the helper's cause too, not docker compose's.
    const SIG_LINE = "diun-update.py got SIGTERM and stopped the restart or the check after it; Docker may still be " +
                     "changing the containers – check with docker ps.";
    const sg = boot("en", { ...BASE_SCRIPT, "diun-update.py backups": { out: JSON.stringify({ ...BACKUPS, ops: RETRY_OPS }) },
        "diun-update.py rollback": { chunks: ["…\n", "@@RESULT " + JSON.stringify({ op: "20261012-030000-d", status: "failed",
            reason: { ...RETRY_OPS[3].reason, source: "signal", line: SIG_LINE } }) + "\n"], fail: { exit_status: 4 } } });
    await settle();
    sg.d.getElementById("dm-tab-btn-backups").click();
    await settle();
    sg.d.querySelector('#dm-ops button[data-op="3"]').click();
    sg.d.getElementById("dm-dialog-confirm").click();
    await settle();
    assert.deepStrictEqual([sg.d.querySelector("#dm-dialog-reason .az-alert__title").textContent,
                            sg.d.querySelector("#dm-dialog-reason .dm-reason-line").textContent],
                           ["Cause (update helper)", SIG_LINE]);

    // 9. The kept output: a button per history entry, the dialog shows it
    const withLog = { ...BACKUPS, ops: BACKUPS.ops.map((o, i) => ({ ...o, log: i === 0, rollback_log: false })) };
    const lg = boot("de", { ...BASE_SCRIPT, "diun-update.py backups": { out: JSON.stringify(withLog) },
        "diun-update.py log": (cmd) => cmd[2] === withLog.ops[0].id
            ? { out: "Updating postgres:16-alpine …\n--- Rollback ---\nRolling …\n" } : { fail: { exit_status: 2 } } });
    await settle();
    lg.d.getElementById("dm-tab-btn-backups").click();
    await settle();
    const logBtns = [...lg.d.querySelectorAll("#dm-ops button[data-log]")];
    assert.strictEqual(logBtns.length, 1, "a button only where output is kept");
    assert.deepStrictEqual(places(lg.d.querySelector("#dm-ops tr")), ["rollback", "log"]);
    assert.strictEqual(logBtns[0].getAttribute("aria-label"), "Ausgabe anzeigen");
    logBtns[0].click();
    await settle();
    console.log("\nlog   :", lg.d.getElementById("dm-dialog-title").textContent, "|",
                JSON.stringify(lg.d.getElementById("dm-dialog-log").textContent));
    assert.strictEqual(JSON.stringify(lg.last("log").cmd.slice(1)), JSON.stringify(["log", withLog.ops[0].id]));
    assert.strictEqual(lg.last("log").opts.superuser, "try");
    assert.strictEqual(lg.d.getElementById("dm-dialog-title").textContent, "Ausgabe: glitchtip-postgres");
    assert(!lg.d.getElementById("dm-dialog-log").hidden &&
           lg.d.getElementById("dm-dialog-log").textContent.includes("--- Rollback ---"));
    assert(lg.d.getElementById("dm-dialog-confirm").hidden, "nothing to confirm");

    // 10. The registry offers again exactly the build whose update failed
    const OLLAMA_DIGEST = "sha256:adca95a12c2b386a8f9a995e7fa0600e187e3323b5e5522f5a6c3b1ec73eff99";
    const failedOp = (digest, extra) => ({ id: "20261012-040000-ollama", ts: "2026-10-12T02:00:00Z", kind: "update",
        container: "ollama", image: "ollama/ollama:latest", services: ["ollama"], rollback: "x", dumps: [],
        status: "failed", can_rollback: false, rolled_back: true, rollback_status: "ok", new_digests: [digest],
        reason: REASON, ...extra });
    const withFailed = (ops) => ({ ...BASE_SCRIPT, "diun-update.py backups": { out: JSON.stringify({ ...BACKUPS, ops }) },
                                   "diun-update.py plan": { out: JSON.stringify(PLAN_JUMP) } });
    // the plugin formats dates in the browser's time zone, the test does the same
    const FB_DATE = new Date("2026-10-12T02:00:00Z").toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" });
    const noteOf = (d) => { const n = rowOf(d, "ollama").querySelector(".dm-row-note"); return n && n.textContent; };
    const fb = boot("de", withFailed([failedOp(OLLAMA_DIGEST), ...BACKUPS.ops]));
    await settle();
    console.log("\nfailed build row:", describe(rowOf(fb.d, "ollama")));
    assert.strictEqual(noteOf(fb.d), `Das Update auf diesen Build ist am ${FB_DATE} gescheitert.`);
    fb.d.querySelector('button[data-update="ollama"]').click();
    await settle();
    const fbBody = fb.d.getElementById("dm-dialog-body");
    const warns = [...fbBody.querySelectorAll(".az-alert--warning .az-alert__body")].map((x) => x.textContent);
    console.log("failed build dialog:", warns[1]);
    assert(warns[1].startsWith(`Das Update auf genau diesen Build ist am ${FB_DATE} gescheitert.`) && warns[1].includes(REASON.line));
    const fc = fb.d.getElementById("dm-dialog-confirm");
    const tickBox = (id, on) => { const x = fb.d.getElementById(id); x.checked = on; x.dispatchEvent(new fb.w.Event("change")); };
    assert(fc.disabled);
    tickBox("dm-retry-check", true);
    assert(fc.disabled, "the jump tick box is still open");
    tickBox("dm-jump-check", true);
    assert(!fc.disabled, "both tick boxes set");
    tickBox("dm-retry-check", false);
    assert(fc.disabled);
    // a different build, or a later successful update, clears it
    const nb = boot("de", withFailed([failedOp("sha256:another-build"), ...BACKUPS.ops]));
    await settle();
    assert.strictEqual(noteOf(nb.d), null, "a new build is not marked");
    nb.d.querySelector('button[data-update="ollama"]').click();
    await settle();
    assert(!nb.d.getElementById("dm-retry-check"));
    const ok = boot("de", withFailed([{ ...failedOp(OLLAMA_DIGEST), id: "20261012-050000-ollama", status: "ok",
                                         rolled_back: false, reason: null }, failedOp(OLLAMA_DIGEST), ...BACKUPS.ops]));
    await settle();
    assert.strictEqual(noteOf(ok.d), null, "a later successful update clears the mark");

    // 11. What else the update touches: groups, dependencies, warnings
    const PLAN_REL = { ...PLAN_OLLAMA, config_changed: false, container: "glitchtip-web", image: "glitchtip/glitchtip:latest",
        project: "glitchtip", services: ["glitchtip-migrate", "glitchtip-web", "glitchtip-worker"],
        containers: ["glitchtip-migrate", "glitchtip-web", "glitchtip-worker"], jobs: ["glitchtip-migrate"],
        depends_on: [{ service: "glitchtip-postgres", condition: "service_started" },
                     { service: "glitchtip-redis", condition: "service_healthy" }],
        dependents: [{ service: "side", containers: ["glitchtip-side"] }],
        job_order: [{ job: "glitchtip-migrate", services: ["glitchtip-web", "glitchtip-worker"] }],
        shared_networks: [{ network: "mailrelay", projects: ["calcom", "smtp-relay"] }],
        installed_version: "6.2.6", tag_digits: 0, jump_unknown: false };
    const rl = boot("de", { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify(PLAN_REL) } });
    await settle();
    rl.d.querySelector('button[data-update="ollama"]').click();
    await settle();
    const rlBody = rl.d.getElementById("dm-dialog-body");
    const rows11 = kvRows(rlBody);
    console.log("\nrelations:", rows11.join(" | "));
    ["Neu erstellt (gleiches Image): glitchtip-web glitchtip-worker",
     "Einmal-Jobs (laufen erneut): glitchtip-migrate",
     "Wartet auf: glitchtip-postgres gestartet, glitchtip-redis gesund",
     "Hängen davon ab (werden danach beobachtet): glitchtip-side"].forEach((want) => assert(rows11.includes(want), want));
    const alerts = [...rlBody.querySelectorAll(".az-alert")].map((x) =>
        [x.className, x.querySelector(".az-alert__title").textContent, x.querySelector(".az-alert__body").textContent]);
    alerts.forEach((x) => console.log("  alert:", x.join(" | ")));
    assert.deepStrictEqual(alerts, [
        ["az-alert az-alert--warning", "Startreihenfolge nicht gesichert",
         "glitchtip-web, glitchtip-worker warten nicht auf glitchtip-migrate: Sie können starten, bevor der Job " +
         "fertig ist. In der Compose-Datei sorgt depends_on mit condition: service_completed_successfully dafür, dass sie warten."],
        ["az-alert", "Gemeinsames Netzwerk",
         "Ebenfalls im Netzwerk mailrelay: calcom, smtp-relay. Sie nutzen dieses Projekt womöglich; das Update unterbricht es kurz."],
        ["az-alert", "Ein Rollback stellt das Image wieder her, nicht die Daten",
         "Migriert die neue Version ihre Datenbank, hilft nur der Dump. Die Container sind beim Neuerstellen kurz nicht erreichbar."]]);
    assert.deepStrictEqual([...rlBody.querySelectorAll(".az-alert--warning code")].map((x) => x.textContent),
                           ["depends_on", "condition: service_completed_successfully"]);
    assert(!rlBody.querySelector(".dm-callout"), "no stripes at the edge any more");
    assert(!rl.d.getElementById("dm-dialog-confirm").disabled, "these are hints, no tick box");

    // 12. What the update brings; a new major version needs a tick box
    const mj = boot("de", { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify(
        { ...PLAN_OLLAMA, config_changed: false, installed_version: "5.0.2", tag_digits: 0, jump_unknown: false,
          target_version: "6.0.2", jump: "major" }) }, "diun-update.py update": runOk });
    await settle();
    mj.d.querySelector('button[data-update="ollama"]').click();
    await settle();
    const mjBody = mj.d.getElementById("dm-dialog-body");
    console.log("major :", kvRows(mjBody).filter((r) => r.startsWith("Bringt") || r.startsWith("Installierte")).join(" | "));
    assert(kvRows(mjBody).includes("Bringt: 6.0.2 Major"));
    assert.strictEqual(mjBody.querySelector(".az-alert--warning .az-alert__title").textContent, "Neue Hauptversion");
    assert.strictEqual(mjBody.querySelector(".az-alert--warning .az-alert__body").textContent,
        "Dieses Update bringt eine neue Hauptversion: 5.0.2 → 6.0.2. Neue Hauptversionen brauchen oft andere Einstellungen " +
        "oder zusätzliche Dienste; prüfen Sie vorher die Release-Notes.");
    const mc = mj.d.getElementById("dm-dialog-confirm");
    assert(mc.disabled, "Update waits for the tick box");
    const mbox = mj.d.getElementById("dm-major-check");
    assert.strictEqual(mbox.parentElement.textContent.trim(), "Ich habe die Release-Notes zu 6.0.2 geprüft.");
    mbox.checked = true;
    mbox.dispatchEvent(new mj.w.Event("change"));
    assert(!mc.disabled);
    // a minor step: the row, no warning; the same version: said so
    for (const [plan, row, gate] of [[{ target_version: "5.4.5", jump: "minor" }, "Bringt: 5.4.5 Minor", false],
                                     [{ target_version: "5.0.2", jump: "same" }, "Bringt: 5.0.2 (die Version, die jetzt läuft)", false],
                                     [{ target_version: "6.0.2", jump: null, installed_version: null, jump_unknown: true }, "Bringt: 6.0.2", true]]) {
        const x = boot("de", { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify(
            { ...PLAN_OLLAMA, config_changed: false, installed_version: "5.0.2", tag_digits: 0, jump_unknown: false, ...plan }) } });
        await settle();
        x.d.querySelector('button[data-update="ollama"]').click();
        await settle();
        const xb = x.d.getElementById("dm-dialog-body");
        assert(kvRows(xb).includes(row), row);
        assert.strictEqual(!!x.d.getElementById("dm-major-check"), false);
        assert.strictEqual(x.d.getElementById("dm-dialog-confirm").disabled, gate);
        if (plan.jump_unknown) assert(xb.querySelector(".az-alert--warning .az-alert__body").textContent.endsWith("latest zeigt gerade auf 6.0.2."));
    }
    // A CI runner whose jobs run now: a warning with a tick box (a CI host,
    // 2026-10-17: two jobs kept running without a runner after its update).
    const started = "2026-10-12T02:29:00Z";
    const ci = boot("de", { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify({ ...PLAN_OLLAMA, config_changed: false,
        ci_jobs: [{ name: "FORGEJO-ACTIONS-TASK-2415_WORKFLOW-a545a355_JOB-unit", image: "example-ci:1", started },
                  { name: "FORGEJO-ACTIONS-TASK-2417_WORKFLOW-9cbae90a_JOB-gitleaks", image: "example-seccan:1", started }] }) } });
    await settle();
    ci.d.querySelector('button[data-update="ollama"]').click();
    await settle();
    const ciAlert = [...ci.d.querySelectorAll("#dm-dialog-body .az-alert--warning")]
        .find((x) => x.querySelector(".az-alert__title").textContent === "CI-Jobs laufen");
    console.log("CI jobs warning:", ciAlert && ciAlert.querySelector(".az-alert__body").textContent);
    assert(ciAlert.querySelector(".az-alert__body").textContent.includes(
        ": unit (Task 2415, gestartet vor 21 min), gitleaks (Task 2417, gestartet vor 21 min)."));
    const cc = ci.d.getElementById("dm-dialog-confirm");
    assert(cc.disabled, "Update waits for the tick box");
    const cbox = ci.d.getElementById("dm-ci-check");
    assert.strictEqual(cbox.parentElement.textContent.trim(), "Laufende Jobs dürfen abbrechen.");
    cbox.checked = true;
    cbox.dispatchEvent(new ci.w.Event("change"));
    assert(!cc.disabled);
    // a database that does not run: an error in the plan, with its icon (.dm-error), not colour alone
    const ds = boot("de", { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify(
        { ...PLAN_OLLAMA, config_changed: false, dbs: [{ name: "actual-db", kind: "postgres", running: false, size: null }] }) } });
    await settle();
    ds.d.querySelector('button[data-update="ollama"]').click();
    await settle();
    const stopped = ds.d.querySelector("#dm-dialog-body .dm-error");
    assert(stopped && stopped.textContent === "actual-db läuft nicht – das Update würde abbrechen");

    // 6. English: no German left in rows, dialog or backups tab
    const h = boot("en", { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify(
        { ...PLAN_GLITCH, installed_version: null, tag_digits: 0, jump_unknown: true, target_version: "16.15", jump: null,
          jobs: ["glitchtip-migrate"],
          depends_on: [{ service: "glitchtip-redis", condition: "service_healthy" }],
          dependents: [{ service: "side", containers: ["glitchtip-side"] }],
          job_order: [{ job: "glitchtip-migrate", services: ["glitchtip-web"] }],
          shared_networks: [{ network: "mailrelay", projects: ["calcom"] }] }) } });
    await settle();
    h.d.querySelector('button[data-update="glitchtip-postgres"]').click();
    await settle();
    h.d.getElementById("dm-tab-btn-backups").click();
    await settle();
    const all = h.d.documentElement.outerHTML;
    const german = ["Aktualisieren", "Lädt", "Übersicht", "Installierte", "Ausrollen", "ausstehend", "Erledigt", "Zurückrollen",
                    "Löschen", "Datenbank", "Betroffene", "Rollback-Kopie", "Erstellt", "Größe", "Zeitpunkt", "Ergebnis",
                    "erfolgreich", "keine Änderung", "in Benutzung", "Projekt", "Datei", "nur manuell", "Abbrechen",
                    "unbekannt", "Hauptversion", "Vermutliche", "Ursache", "Speichermangel", "Wartezeit",
                    "Ausgabe", "gescheitert", "trotzdem", "zusammen mit", "Neu erstellt", "Einmal-Jobs", "Wartet auf",
                    "Hängen davon ab", "gesund", "Ebenfalls", "Bringt", "zeigt gerade", "Hauptversion"];
    assert(all.includes("16-alpine points to 16.15 right now."));
    assert(all.includes("The installed version is unknown and the tag 16-alpine follows every new release"));
    const found = german.filter((w) => all.includes(w));
    console.log("\nEN German leftovers:", found.length ? found : "none");
    assert.deepStrictEqual(found, []);

    // A rollback copy that is gone (deleted by the cleanup, in this tab or outside the add-on):
    // the history says so, with the image in the tooltip, instead of only leaving the button out.
    const GONE = [{ id: "20261012-050000-g", ts: "2026-10-12T03:00:00Z", kind: "update", container: "g", image: "g:latest",
                    services: ["g"], rollback: "g:rollback-20261012-050000", dumps: [], status: "ok", can_rollback: false,
                    rollback_gone: true },
                  { ...BACKUPS.ops[0] }];
    for (const [lang, word, tip] of [
        ["de", "Rollback-Kopie fehlt", "Das Rollback-Image g:rollback-20261012-050000 gibt es nicht mehr"],
        ["en", "Rollback copy gone", "The rollback image g:rollback-20261012-050000 no longer exists"]]) {
        const gp = boot(lang, { ...BASE_SCRIPT, "diun-update.py backups": { out: JSON.stringify({ ...BACKUPS, ops: GONE }) } });
        await settle();
        gp.d.getElementById("dm-tab-btn-backups").click();
        await settle();
        const rows = [...gp.d.querySelectorAll("#dm-ops tr")];
        const note = rows[0].querySelector(".dm-rb-gone");
        console.log("rollback copy gone (" + lang + "):", note ? note.textContent : "no note");
        assert(note && note.textContent === word, lang + ": " + rows[0].textContent);
        assert(note.title.startsWith(tip) && note.title.includes("prune"), note.title);
        assert(!rows[0].querySelector("button[data-op]"), "no rollback button where the copy is gone");
        assert(!rows[1].querySelector(".dm-rb-gone") && rows[1].querySelector("button[data-op]"),
               "a copy that is there: the button, no note");
    }

    console.log("\nALL CHECKS PASSED");
})().catch((e) => { console.error("FEHLGESCHLAGEN:", e.stack || e.message); process.exit(1); });

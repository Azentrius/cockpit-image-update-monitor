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
const js = fs.readFileSync(path.join(PLUGIN, "diun.js"), "utf8");
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
    images: [{ ref: "postgres:rollback-20261012-031500", id: "sha256:old", size: "275MB", created: "2026-10-12 03:15:00 +0200 CEST", in_use: false },
             { ref: "ollama/ollama:rollback-20261011-120000", id: "sha256:x", size: "3.4GB", created: "2026-10-11 12:00:00 +0200 CEST", in_use: true }],
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
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
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
    await sleep(80);
    const rowOf = (d, name) => [...d.querySelectorAll("#dm-rows tr")].find((tr) => tr.querySelector("td").textContent === name);
    const describe = (tr) => [...tr.querySelectorAll("td")].map((td) => td.textContent.trim()).filter(Boolean).join(" | ");
    for (const n of ["ollama", "glitchtip-postgres", "actual-ai", "calcom"]) console.log("row:", describe(rowOf(a.d, n)));
    assert(rowOf(a.d, "ollama").querySelector('button[data-update="ollama"]').textContent === "Update");
    assert(rowOf(a.d, "glitchtip-postgres").querySelector("button[data-update]").textContent === "Ausrollen");
    assert(rowOf(a.d, "glitchtip-postgres").textContent.includes("Ausrollen ausstehend"));
    console.log("meta:", a.d.getElementById("dm-meta").textContent);

    // 2. Update ollama: plan -> body -> confirm -> stream -> success
    const runOk = { chunks: ["Update ollama/ollama:latest …\n", "+ docker compose … pull\n",
                             '@@RESULT {"op":"20261012-032000-ollama","status":"ok","rollback":"ollama/ollama:rollback-20261012-032000"}\n'] };
    const b = boot("de", { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify(PLAN_OLLAMA) },
                                           "diun-update.py update": runOk });
    await sleep(80);
    b.d.querySelector('button[data-update="ollama"]').click();
    await sleep(40);
    const body = b.d.getElementById("dm-dialog-body").textContent.replace(/\s+/g, " ");
    console.log("\ndialog:", b.d.getElementById("dm-dialog-title").textContent, "|", b.d.getElementById("dm-dialog-desc").textContent);
    console.log("body  :", body);
    assert(b.d.getElementById("dm-dump-check").checked);
    assert(body.includes("Compose-Datei wurde seit dem Start"));
    assert(!b.d.getElementById("dm-jump-check") && !body.includes("Installierte Version"),
           "an older plan without version fields: no warning, no row");
    b.d.getElementById("dm-dialog-confirm").click();
    await sleep(60);
    const up = b.last("update");
    console.log("spawn :", JSON.stringify(up.cmd), up.opts.superuser);
    assert.strictEqual(JSON.stringify(up.cmd), JSON.stringify(["/usr/local/lib/diun-radar/diun-update.py", "update", "ollama"]));
    const log = b.d.getElementById("dm-dialog-log").textContent;
    console.log("log   :", JSON.stringify(log));
    assert(!log.includes("@@RESULT"));
    console.log("result:", b.d.getElementById("dm-dialog-result").textContent);
    assert.strictEqual(b.d.getElementById("dm-dialog-result").textContent, "Update erfolgreich. Rollback-Kopie: ollama/ollama:rollback-20261012-032000");

    // 3. Without dump -> --no-dump; failure -> rollback offered -> rollback runs
    const c = boot("de", { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify(PLAN_GLITCH) },
        "diun-update.py update": { chunks: ["…\n", '@@RESULT {"op":"20261012-033000-glitchtip-postgres","status":"failed"}\n'], fail: { exit_status: 4 } },
        "diun-update.py rollback": { chunks: ["Rollback …\n", '@@RESULT {"op":"20261012-033000-glitchtip-postgres","status":"ok"}\n'] } });
    await sleep(80);
    c.d.querySelector('button[data-update="glitchtip-postgres"]').click();
    await sleep(40);
    console.log("\ndialog:", c.d.getElementById("dm-dialog-title").textContent, "|", c.d.getElementById("dm-dialog-desc").textContent);
    c.d.getElementById("dm-dump-check").checked = false;
    c.d.getElementById("dm-dialog-confirm").click();
    await sleep(60);
    console.log("spawn :", JSON.stringify(c.last("update").cmd.slice(1)));
    assert.strictEqual(JSON.stringify(c.last("update").cmd.slice(1)), JSON.stringify(["update", "glitchtip-postgres", "--no-dump"]));
    const extra = c.d.getElementById("dm-dialog-extra");
    console.log("result:", c.d.getElementById("dm-dialog-result").textContent, "| extra:", !extra.hidden, extra.textContent.trim());
    assert(!extra.hidden && extra.textContent.trim() === "Zurückrollen");
    extra.click();
    await sleep(60);
    console.log("spawn :", JSON.stringify(c.last("rollback").cmd.slice(1)), "| result:", c.d.getElementById("dm-dialog-result").textContent);
    assert.strictEqual(c.d.getElementById("dm-dialog-result").textContent, "Rollback erfolgreich.");

    // 3b. Dialog closed while the plan is still loading -> must stay closed
    const s = boot("de", { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify(PLAN_OLLAMA) } });
    await sleep(80);
    s.d.querySelector('button[data-update="ollama"]').click();
    s.d.getElementById("dm-dialog-cancel").click();
    await sleep(40);
    console.log("\nclosed during plan -> open:", s.d.getElementById("dm-dialog").hasAttribute("open"));
    assert(!s.d.getElementById("dm-dialog").hasAttribute("open"));

    // 4. Not updatable / no admin
    const e = boot("de", { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify({ ...PLAN_OLLAMA, updatable: false, reason: "not-compose" }) } });
    await sleep(80);
    e.d.querySelector('button[data-update="ollama"]').click();
    await sleep(40);
    console.log("\nnot updatable:", e.d.getElementById("dm-dialog-desc").textContent, "| confirm hidden:", e.d.getElementById("dm-dialog-confirm").hidden);
    assert(e.d.getElementById("dm-dialog-confirm").hidden);
    const f = boot("en", { ...BASE_SCRIPT, "diun-update.py plan": { fail: { problem: "access-denied" } } });
    await sleep(80);
    f.d.querySelector('button[data-update="ollama"]').click();
    await sleep(40);
    console.log("no admin     :", f.d.getElementById("dm-dialog-desc").textContent);
    assert(f.d.getElementById("dm-dialog-desc").textContent.startsWith("Denied"));

    // 5. Backups tab
    const g = boot("de", { ...BASE_SCRIPT,
        "diun-update.py delete-dump": { chunks: ["Gelöscht: …\n"] },
        "diun-update.py rollback": { chunks: ['@@RESULT {"status":"ok"}\n'] } });
    await sleep(80);
    g.d.getElementById("dm-tab-btn-backups").click();
    await sleep(80);
    assert(!g.d.getElementById("dm-tab-backups").hidden && g.d.getElementById("dm-tab-updates").hidden);
    console.log("\nbackups summary:", g.d.getElementById("dm-bk-summary").textContent);
    ["dm-ops", "dm-dumps", "dm-images"].forEach((id) =>
        [...g.d.querySelectorAll(`#${id} tr`)].forEach((tr) => console.log(`  ${id.slice(3).padEnd(6)}`, describe(tr))));
    const inUseBtn = g.d.querySelector('#dm-images button[data-img="1"]');
    assert(inUseBtn.disabled, "in-use image cannot be deleted");
    g.d.querySelector('#dm-dumps button[data-dump="0"]').click();
    console.log("delete dialog:", g.d.getElementById("dm-dialog-title").textContent, "|", g.d.getElementById("dm-dialog-desc").textContent);
    g.d.getElementById("dm-dialog-confirm").click();
    await sleep(60);
    console.log("spawn :", JSON.stringify(g.last("delete-dump").cmd.slice(1)), g.last("delete-dump").opts.superuser,
                "| result:", g.d.getElementById("dm-dialog-result").textContent);
    assert.strictEqual(g.last("delete-dump").cmd[2], "glitchtip/20261012-031500_glitchtip-postgres.sql.gz");
    g.d.getElementById("dm-dialog-cancel").click();
    g.d.querySelector('#dm-ops button[data-op="0"]').click();
    console.log("rollback dialog:", g.d.getElementById("dm-dialog-title").textContent, "|", g.d.getElementById("dm-dialog-desc").textContent);
    g.d.getElementById("dm-dialog-confirm").click();
    await sleep(60);
    assert.strictEqual(JSON.stringify(g.last("rollback").cmd.slice(1)), JSON.stringify(["rollback", "20261012-031500-glitchtip-postgres"]));
    console.log("result:", g.d.getElementById("dm-dialog-result").textContent);
    const causes = [...g.d.querySelectorAll("#dm-ops .dm-op-reason")].map((x) => x.textContent);
    console.log("causes:", causes);
    assert.deepStrictEqual(causes, ["formbricks-app: " + REASON.line, "x: Wegen Speichermangels beendet.",
                                    "y: Health-Check: curl: (7) Failed to connect to localhost port 8000"]);
    assert.strictEqual(g.d.querySelector("#dm-ops .dm-op-reason").title, "formbricks-app: " + REASON.line);

    // 7. Unknown jump size: the warning comes first, Update waits for the tick box
    const j = boot("de", { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify(PLAN_JUMP) },
                                           "diun-update.py update": runOk });
    await sleep(80);
    j.d.querySelector('button[data-update="ollama"]').click();
    await sleep(40);
    const jbody = j.d.getElementById("dm-dialog-body");
    console.log("\njump  :", jbody.textContent.replace(/\s+/g, " ").slice(0, 150), "…");
    assert(jbody.firstElementChild.classList.contains("dm-callout-warn"), "warning first");
    assert(jbody.firstElementChild.textContent.startsWith("Die installierte Version ist unbekannt, und der Tag latest folgt"));
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
    await sleep(60);
    assert.strictEqual(j.last("update").cmd[2], "ollama");
    console.log("jump  : confirmed after the tick box ->", JSON.stringify(j.last("update").cmd.slice(1)));
    const kn = boot("de", { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify(
        { ...PLAN_OLLAMA, installed_version: "0.30.11", tag_digits: 0, jump_unknown: false }) } });
    await sleep(80);
    kn.d.querySelector('button[data-update="ollama"]').click();
    await sleep(40);
    assert(!kn.d.getElementById("dm-jump-check") && !kn.d.getElementById("dm-dialog-confirm").disabled);
    assert(kvRows(kn.d.getElementById("dm-dialog-body")).includes("Installierte Version: 0.30.11"));

    // 8. Failed update: the most likely cause above the log, the rollback next to it
    const k = boot("de", { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify(PLAN_GLITCH) },
        "diun-update.py update": { chunks: ["…\n", "@@RESULT " + JSON.stringify(
            { op: "20261012-232502-formbricks-app", status: "failed", reason: REASON }) + "\n"], fail: { exit_status: 4 } },
        "diun-update.py rollback": { chunks: ["Rollback …\n", '@@RESULT {"op":"20261012-232502-formbricks-app","status":"ok"}\n'] } });
    await sleep(80);
    k.d.querySelector('button[data-update="glitchtip-postgres"]').click();
    await sleep(40);
    const box = k.d.getElementById("dm-dialog-reason");
    assert(box.hidden, "no cause before the run");
    k.d.getElementById("dm-dialog-confirm").click();
    await sleep(60);
    console.log("\ncause :", box.textContent.replace(/\s+/g, " ").trim());
    assert(!box.hidden && box.classList.contains("az-alert--danger"));
    assert.strictEqual(box.querySelector(".az-alert__title").textContent, "Vermutliche Ursache (formbricks-app)");
    assert.strictEqual(box.querySelector(".dm-reason-line").textContent, REASON.line);
    assert(box.compareDocumentPosition(k.d.getElementById("dm-dialog-log")) & k.w.Node.DOCUMENT_POSITION_FOLLOWING,
           "the cause stands above the log");
    assert(!k.d.getElementById("dm-dialog-extra").hidden, "rollback offered");
    k.d.getElementById("dm-dialog-extra").click();
    await sleep(60);
    assert(box.hidden, "a new run hides the old cause");
    assert.strictEqual(k.d.getElementById("dm-dialog-result").textContent, "Rollback erfolgreich.");
    // a failed rollback names its own cause; without a log line the state speaks
    const m = boot("de", { ...BASE_SCRIPT,
        "diun-update.py rollback": { chunks: ["…\n", "@@RESULT " + JSON.stringify({ op: "x", status: "failed",
            reason: { container: "glitchtip-postgres", state: "starting", exit_code: 0, oom: false, source: null, line: null } }) + "\n"],
            fail: { exit_status: 4 } } });
    await sleep(80);
    m.d.getElementById("dm-tab-btn-backups").click();
    await sleep(40);
    m.d.querySelector('#dm-ops button[data-op="0"]').click();
    m.d.getElementById("dm-dialog-confirm").click();
    await sleep(60);
    console.log("rollback cause:", m.d.getElementById("dm-dialog-reason").textContent.replace(/\s+/g, " ").trim());
    assert.strictEqual(m.d.querySelector("#dm-dialog-reason .dm-reason-line").textContent,
                       "In der Wartezeit nicht bereit geworden; im Log steht keine Fehlerzeile.");

    // 9. The kept output: a button per history entry, the dialog shows it
    const withLog = { ...BACKUPS, ops: BACKUPS.ops.map((o, i) => ({ ...o, log: i === 0, rollback_log: false })) };
    const lg = boot("de", { ...BASE_SCRIPT, "diun-update.py backups": { out: JSON.stringify(withLog) },
        "diun-update.py log": (cmd) => cmd[2] === withLog.ops[0].id
            ? { out: "Updating postgres:16-alpine …\n--- Rollback ---\nRolling …\n" } : { fail: { exit_status: 2 } } });
    await sleep(80);
    lg.d.getElementById("dm-tab-btn-backups").click();
    await sleep(40);
    const logBtns = [...lg.d.querySelectorAll("#dm-ops button[data-log]")];
    assert.strictEqual(logBtns.length, 1, "a button only where output is kept");
    assert.strictEqual(logBtns[0].getAttribute("aria-label"), "Ausgabe anzeigen");
    logBtns[0].click();
    await sleep(40);
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
    await sleep(120);
    console.log("\nfailed build row:", describe(rowOf(fb.d, "ollama")));
    assert.strictEqual(noteOf(fb.d), `Das Update auf diesen Build ist am ${FB_DATE} gescheitert.`);
    fb.d.querySelector('button[data-update="ollama"]').click();
    await sleep(40);
    const fbBody = fb.d.getElementById("dm-dialog-body");
    const warns = [...fbBody.querySelectorAll(".dm-callout-warn")].map((x) => x.textContent);
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
    await sleep(120);
    assert.strictEqual(noteOf(nb.d), null, "a new build is not marked");
    nb.d.querySelector('button[data-update="ollama"]').click();
    await sleep(40);
    assert(!nb.d.getElementById("dm-retry-check"));
    const ok = boot("de", withFailed([{ ...failedOp(OLLAMA_DIGEST), id: "20261012-050000-ollama", status: "ok",
                                         rolled_back: false, reason: null }, failedOp(OLLAMA_DIGEST), ...BACKUPS.ops]));
    await sleep(120);
    assert.strictEqual(noteOf(ok.d), null, "a later successful update clears the mark");

    // 6. English: no German left in rows, dialog or backups tab
    const h = boot("en", { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify(
        { ...PLAN_GLITCH, installed_version: null, tag_digits: 0, jump_unknown: true }) } });
    await sleep(80);
    h.d.querySelector('button[data-update="glitchtip-postgres"]').click();
    await sleep(40);
    h.d.getElementById("dm-tab-btn-backups").click();
    await sleep(80);
    const all = h.d.documentElement.outerHTML;
    const german = ["Aktualisieren", "Lädt", "Übersicht", "Installierte", "Ausrollen", "ausstehend", "Erledigt", "Zurückrollen",
                    "Löschen", "Datenbank", "Betroffene", "Rollback-Kopie", "Erstellt", "Größe", "Zeitpunkt", "Ergebnis",
                    "erfolgreich", "keine Änderung", "in Benutzung", "Projekt", "Datei", "nur manuell", "Abbrechen",
                    "unbekannt", "Hauptversion", "Vermutliche", "Ursache", "Speichermangel", "Wartezeit",
                    "Ausgabe", "gescheitert", "trotzdem"];
    assert(all.includes("The installed version is unknown and the tag 16-alpine follows every new release"));
    const found = german.filter((w) => all.includes(w));
    console.log("\nEN German leftovers:", found.length ? found : "none");
    assert.deepStrictEqual(found, []);

    console.log("\nALL CHECKS PASSED");
})().catch((e) => { console.error("FEHLGESCHLAGEN:", e.stack || e.message); process.exit(1); });

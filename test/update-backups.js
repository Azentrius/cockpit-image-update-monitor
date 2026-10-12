// Update flow (plan -> confirm -> live log -> result / rollback) and the
// backups tab (history, dumps, rollback images, delete), against the real
// diun.js in jsdom with a scripted Cockpit stub.
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
            image: "ollama/ollama:latest", services: ["ollama"], rollback: null, dumps: [], status: "nochange", can_rollback: false }],
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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

    // 6. English: no German left in rows, dialog or backups tab
    const h = boot("en", { ...BASE_SCRIPT, "diun-update.py plan": { out: JSON.stringify(PLAN_GLITCH) } });
    await sleep(80);
    h.d.querySelector('button[data-update="glitchtip-postgres"]').click();
    await sleep(40);
    h.d.getElementById("dm-tab-btn-backups").click();
    await sleep(80);
    const all = h.d.documentElement.outerHTML;
    const german = ["Aktualisieren", "Lädt", "Übersicht", "Installierte", "Ausrollen", "ausstehend", "Erledigt", "Zurückrollen",
                    "Löschen", "Datenbank", "Betroffene", "Rollback-Kopie", "Erstellt", "Größe", "Zeitpunkt", "Ergebnis",
                    "erfolgreich", "keine Änderung", "in Benutzung", "Projekt", "Datei", "nur manuell", "Abbrechen"];
    const found = german.filter((w) => all.includes(w));
    console.log("\nEN German leftovers:", found.length ? found : "none");
    assert.deepStrictEqual(found, []);

    console.log("\nALLE CHECKS OK");
})().catch((e) => { console.error("FEHLGESCHLAGEN:", e.stack || e.message); process.exit(1); });

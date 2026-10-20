// SPDX-License-Identifier: Apache-2.0
// "Run radar now", the cleanup box of the backups tab (rules, timer, what is
// to be deleted, "Clean up now"), versions in the history and in the update
// result - against the real diun.js in jsdom with a scripted Cockpit stub.
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { JSDOM } = require("jsdom");

const PLUGIN = path.join(__dirname, "..", "plugin");
const FIX = path.join(__dirname, "fixtures");
const html = fs.readFileSync(path.join(PLUGIN, "index.html"), "utf8");
// The page's scripts as index.html loads them (test/page-scripts.js).
const js = require("./page-scripts").pageScripts();
const inventory = fs.readFileSync(path.join(FIX, "inventory3.jsonl"), "utf8");
const files = {
    "/var/lib/diun/events.jsonl": fs.readFileSync(path.join(FIX, "events.jsonl"), "utf8"),
    "/var/lib/diun-radar/versions.json": JSON.parse(fs.readFileSync(path.join(FIX, "versions2.json"), "utf8")),
    "/var/lib/diun/dismissed.json": null,
};
const UPDATE = "/usr/lib/cockpit-image-updates/diun-update.py";
const deps = (over) => JSON.stringify(Object.assign({
    root: true, docker: { state: "ok", compose: true }, diun: { state: "running", events: true },
    skopeo: { state: "ok" }, radar: { state: "active", running: false, can_install: true },
    cleanup: { state: "missing", can_install: true } }, over || {}));
const BACKUPS = {
    dir: "/var/backups/image-updates", free_bytes: 40 * 2 ** 30, dump_bytes: 6 * 2 ** 20,
    dumps: [{ path: "glitchtip/20261012-031500_glitchtip-postgres.sql.gz", project: "glitchtip",
              container: "glitchtip-postgres", size: 3 * 2 ** 20, mtime: 1791768900 },
            { path: "glitchtip/20260901-031500_glitchtip-postgres.sql.gz", project: "glitchtip",
              container: "glitchtip-postgres", size: 3 * 2 ** 20, mtime: 1788232500 }],
    // the first as an older helper sent it (Docker's text), the second in bytes
    images: [{ ref: "postgres:rollback-20260901-031500", id: "sha256:a", size: "275MB", created: "2026-09-01 03:15:00 +0200 CEST", in_use: false },
             { ref: "postgres:rollback-20261012-031500", id: "sha256:b", size: 275 * 2 ** 20, created: "2026-10-12T03:15:00+02:00", in_use: false }],
    ops: [{ id: "20261012-031500-glitchtip-postgres", ts: "2026-10-12T01:15:00Z", kind: "update", container: "glitchtip-postgres",
            image: "postgres:16-alpine", services: ["postgres"], rollback: "postgres:rollback-20261012-031500",
            dumps: ["glitchtip/20261012-031500_glitchtip-postgres.sql.gz"], status: "ok", can_rollback: true,
            from_version: "16.14", to_version: "16.15" },
          { id: "20260901-031500-glitchtip-postgres", ts: "2026-09-01T01:15:00Z", kind: "update", container: "glitchtip-postgres",
            image: "postgres:16-alpine", services: ["postgres"], rollback: null, dumps: [], status: "ok", can_rollback: false }],
    cleanup: { keep_dumps: 1, image_days: 14, file: null, error: null,
               dumps: ["glitchtip/20260901-031500_glitchtip-postgres.sql.gz"], images: ["postgres:rollback-20260901-031500"],
               bytes: 3 * 2 ** 20 },
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

function boot(language, script) {
    // outside-only: the page's own <script> tags never run; diun.js is evaluated below.
    const dom = new JSDOM(html, { runScripts: "outside-only" });
    const w = dom.window;
    w.document.querySelectorAll("script").forEach((s) => s.remove());
    w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
    w.HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
    w.Date.now = () => Date.parse("2026-10-12T02:50:00Z");
    const calls = [];
    w.cockpit = {
        language,
        file(p) { return { read: () => Promise.resolve(files[p] ?? null), watch: (cb) => cb(files[p], "t"), replace: () => Promise.resolve() }; },
        spawn(cmd, opts) {
            calls.push({ cmd: JSON.parse(JSON.stringify(cmd)), opts: JSON.parse(JSON.stringify(opts || {})) });
            const verb = (cmd[0].split("/").pop() + " " + (cmd[1] || "")).trim();
            const s = script[verb] || script[cmd[0].split("/").pop()];
            if (!s) return Promise.reject(new Error("unscripted " + verb));
            const r = typeof s === "function" ? s(cmd) : s;
            if (r instanceof Promise) { r.stream = () => r; return r; }
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
    const count = (verb) => calls.filter((c) => (c.cmd[0].split("/").pop() + " " + (c.cmd[1] || "")).trim() === verb).length;
    return { w, d, calls, count, text: (id) => d.getElementById(id).textContent.replace(/\s+/g, " ").trim() };
}
const BASE = {
    "diun-inventory.py": { out: inventory },
    "diun-deps.sh check": { out: deps() },
    "diun-update.py backups": { out: JSON.stringify(BACKUPS) },
};

(async () => {
    // 1. Run radar now: one run, button busy until it ends, double click ignored, then reload
    let finish;
    const a = boot("de", { ...BASE, "diun-update.py radar": () => new Promise((r) => { finish = r; }) });
    await settle();
    const btn = a.d.getElementById("dm-radar");
    console.log("radar button:", !btn.hidden, "|", a.text("dm-radar"), "|", btn.title);
    assert(!btn.hidden && !btn.disabled && a.text("dm-radar") === "Radar jetzt starten");
    btn.click();
    btn.click();
    await settle();
    console.log("while running:", a.text("dm-radar"), "| disabled:", btn.disabled, "| runs:", a.count("diun-update.py radar"));
    assert(btn.disabled && a.text("dm-radar") === "Radar läuft …" && a.count("diun-update.py radar") === 1);
    const call = a.calls.find((c) => c.cmd[1] === "radar");
    assert.strictEqual(JSON.stringify(call.cmd), JSON.stringify([UPDATE, "radar"]));
    assert.strictEqual(call.opts.superuser, "require");
    const loadsBefore = a.count("diun-inventory.py");
    finish("");
    await settle();
    console.log("after the run:", a.text("dm-radar"), "| reloaded:", a.count("diun-inventory.py") > loadsBefore);
    assert(!btn.disabled && a.text("dm-radar") === "Radar jetzt starten" && a.count("diun-inventory.py") > loadsBefore);

    // 2. Radar not set up -> no button; a run in progress (timer, after an update) -> busy
    const b = boot("de", { ...BASE, "diun-deps.sh check": { out: deps({ radar: { state: "missing", can_install: true } }) } });
    await settle();
    assert(b.d.getElementById("dm-radar").hidden, "hidden without radar");
    const c = boot("en", { ...BASE, "diun-deps.sh check": { out: deps({ radar: { state: "active", running: true } }) } });
    await settle();
    console.log("run in progress:", c.text("dm-radar"), "| disabled:", c.d.getElementById("dm-radar").disabled);
    assert(c.d.getElementById("dm-radar").disabled && c.text("dm-radar") === "Radar running …");

    // 3. Failures stay visible: an error alert with a title, the fix below, commands as code
    for (const [fail, title, body, code] of [
        [{ exit_status: 1 }, "The radar run failed", "The journal shows why: journalctl -u diun-radar.service", "journalctl -u diun-radar.service"],
        [{ exit_status: 2 }, "The version radar is not set up", "Set it up under “Missing requirements”.", undefined],
        [{ problem: "access-denied" }, "Administrative access needed", "Denied: this needs administrative access. Switch it on at the top of Cockpit.", undefined]]) {
        const e = boot("en", { ...BASE, "diun-update.py radar": { fail } });
        await settle();
        e.d.getElementById("dm-radar").click();
        await settle();
        const st = e.d.getElementById("dm-status");
        const got = [st.querySelector(".az-alert__title").textContent, st.querySelector(".az-alert__body").textContent];
        console.log("radar fails", JSON.stringify(fail).padEnd(28), "->", got.join(" / "));
        assert.deepStrictEqual(got, [title, body]);
        assert.deepStrictEqual([st.hidden, st.getAttribute("role"), st.classList.contains("az-alert--danger")], [false, "alert", true]);
        assert.strictEqual((st.querySelector("code") || {}).textContent, code);
        assert(!e.d.getElementById("dm-radar").disabled);
    }

    // 4. Backups tab: rules, timer, preview, marks, versions in the history
    const g = boot("de", { ...BASE, "diun-update.py cleanup": { chunks: ["Regeln (Standard): …\n", "Dump gelöscht: …\n",
        '@@RESULT {"status":"ok","dumps":["glitchtip/20260901-031500_glitchtip-postgres.sql.gz"],"images":["postgres:rollback-20260901-031500"],"bytes":3145728,"failed":[]}\n'] },
        "diun-deps.sh install": { chunks: ["+ systemctl enable --now diun-cleanup.timer\n"] } });
    await settle();
    g.d.getElementById("dm-tab-btn-backups").click();
    await settle();
    console.log("\nrules  :", g.text("dm-cleanup-rules"));
    console.log("timer  :", g.text("dm-cleanup-timer"));
    console.log("preview:", g.text("dm-cleanup-preview"), "| button:", !g.d.getElementById("dm-cleanup-btn").hidden);
    assert(g.text("dm-cleanup-rules").startsWith("Regeln: behält den neuesten Dump je Datenbank-Container, entfernt ungenutzte Rollback-Images nach 14 Tagen. Standardwerte"));
    assert.deepStrictEqual([...g.d.querySelectorAll("#dm-cleanup-rules code")].map((x) => x.textContent),
                           ["/etc/image-updates.conf", "KEEP_DUMPS", "ROLLBACK_IMAGE_DAYS"]);
    assert.strictEqual(g.text("dm-cleanup-preview"), "Jetzt zu löschen: 1 Dump, 1 Rollback-Image (3,00 MiB).");
    assert(!g.d.getElementById("dm-cleanup-btn").hidden);
    const rows = (id) => [...g.d.querySelectorAll(`#${id} tr`)].map((tr) => tr.textContent.replace(/\s+/g, " ").trim());
    rows("dm-dumps").forEach((r) => console.log("  dump  ", r));
    rows("dm-images").forEach((r) => console.log("  image ", r));
    rows("dm-ops").forEach((r) => console.log("  op    ", r));
    assert.deepStrictEqual(rows("dm-dumps").map((r) => r.includes("wird gelöscht")), [false, true]);
    assert.deepStrictEqual(rows("dm-images").map((r) => r.includes("wird gelöscht")), [true, false]);
    assert.deepStrictEqual(rows("dm-images").map((r) => r.includes("275MB") || r.includes("275,00 MiB")), [true, true],
                           "sizes in bytes on a 1024 base; Docker's text from an older helper as it is");
    const day = (iso) => new Date(iso).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" });
    assert.deepStrictEqual(rows("dm-images").map((r) => r.includes(day("2026-09-01T03:15:00+02:00")) || r.includes(day("2026-10-12T03:15:00+02:00"))),
                           [true, true], "the creation time as a date, also in Docker's text");
    assert(rows("dm-ops")[0].includes("16.14 → 16.15") && rows("dm-ops")[1].includes("—"));

    // 5. Set up the daily run from the timer line
    const setup = g.d.querySelector("#dm-cleanup-timer button");
    assert(setup && setup.textContent === "Täglichen Lauf einrichten");
    setup.click();
    console.log("setup dialog:", g.text("dm-dialog-title"));
    assert.strictEqual(g.text("dm-dialog-title"), "Tägliches Aufräumen einrichten?");
    g.d.getElementById("dm-dialog-confirm").click();
    await settle();
    assert.strictEqual(JSON.stringify(g.calls.find((x) => x.cmd[1] === "install").cmd.slice(1)), JSON.stringify(["install", "cleanup"]));
    g.d.getElementById("dm-dialog-cancel").click();
    await settle();

    // 6. Clean up dumps and images: dialog lists what goes, runs `cleanup`, result from @@RESULT
    assert.strictEqual(g.text("dm-cleanup-btn"), "Dumps und Images aufräumen");
    g.d.getElementById("dm-cleanup-btn").click();
    console.log("\ncleanup dialog:", g.text("dm-dialog-title"), "|", g.text("dm-dialog-body"));
    assert.deepStrictEqual([g.text("dm-dialog-title"), g.d.querySelector("#dm-dialog-confirm .dm-btn-label").textContent],
                           ["Dumps und Images aufräumen?", "Dumps und Images aufräumen"]);
    assert(g.text("dm-dialog-body").includes("glitchtip/20260901-031500_glitchtip-postgres.sql.gz"));
    assert(g.text("dm-dialog-body").includes("postgres:rollback-20260901-031500"));
    g.d.getElementById("dm-dialog-confirm").click();
    await settle();
    const cl = g.calls.find((x) => x.cmd[1] === "cleanup");
    console.log("spawn :", JSON.stringify(cl.cmd.slice(1)), cl.opts.superuser, "| result:", g.text("dm-dialog-result"));
    assert.strictEqual(JSON.stringify(cl.cmd), JSON.stringify([UPDATE, "cleanup"]));
    assert.strictEqual(g.text("dm-dialog-result"), "Aufgeräumt: 1 Dump, 1 Rollback-Image (3,00 MiB frei geworden).");

    // 7. Rules not understood -> the message, no button; timer active -> no setup button
    const broken = { ...BACKUPS, cleanup: { ...BACKUPS.cleanup, error: "/etc/image-updates.conf, line 2 not understood: KEEP=1 – nothing deleted.",
                                            dumps: [], images: [], keep_dumps: null, image_days: null } };
    const h = boot("en", { ...BASE, "diun-update.py backups": { out: JSON.stringify(broken) },
                           "diun-deps.sh check": { out: deps({ cleanup: { state: "active", can_install: true } }) } });
    await settle();
    h.d.getElementById("dm-tab-btn-backups").click();
    await settle();
    console.log("\nbroken rules:", h.text("dm-cleanup-rules"), "| timer:", h.text("dm-cleanup-timer"));
    assert(h.text("dm-cleanup-rules").startsWith("Cleanup rules: /etc/image-updates.conf, line 2"));
    assert(h.d.querySelector("#dm-cleanup-rules .dm-error"), "an error with its icon, not colour alone");
    assert(h.d.getElementById("dm-cleanup-btn").hidden && !h.d.querySelector("#dm-cleanup-timer button"));
    assert(h.text("dm-cleanup-timer").includes("daily via diun-cleanup.timer"));

    // 8. Helper older than the plugin (no "cleanup" in backups) -> no box
    const old = { ...BACKUPS };
    delete old.cleanup;
    const k = boot("en", { ...BASE, "diun-update.py backups": { out: JSON.stringify(old) } });
    await settle();
    k.d.getElementById("dm-tab-btn-backups").click();
    await settle();
    assert(k.d.getElementById("dm-cleanup").hidden);

    // 9. Update result names the versions
    const PLAN = { container: "glitchtip-postgres", image: "postgres:16-alpine", updatable: true, project: "glitchtip",
        workdir: "/opt/glitchtip", services: ["postgres"], containers: ["glitchtip-postgres"], pulled: true, config_changed: false,
        dbs: [], free_bytes: 40 * 2 ** 30 };
    const m = boot("de", { ...BASE, "diun-update.py plan": { out: JSON.stringify(PLAN) },
        "diun-update.py update": { chunks: ['@@RESULT {"op":"x","status":"ok","rollback":"postgres:rollback-20261012-040000","from_version":"16.14","to_version":"16.15"}\n'] } });
    await settle();
    m.d.querySelector('button[data-update="glitchtip-postgres"]').click();
    await settle();
    m.d.getElementById("dm-dialog-confirm").click();
    await settle();
    console.log("\nupdate result:", m.text("dm-dialog-result"));
    assert.strictEqual(m.text("dm-dialog-result"), "Update erfolgreich: 16.14 → 16.15. Rollback-Kopie: postgres:rollback-20261012-040000");

    // 10. English: no German in the header, the backups tab and the cleanup dialog
    const n = boot("en", BASE);
    await settle();
    n.d.getElementById("dm-tab-btn-backups").click();
    await settle();
    n.d.getElementById("dm-cleanup-btn").click();
    const all = n.d.documentElement.outerHTML;
    const german = ["Radar jetzt", "läuft", "Aufräumen", "aufräumen", "Regeln", "behält", "Datenbank", "Tagen", "Standardwerte",
                    "wird gelöscht", "Jetzt zu löschen", "Einrichten", "täglich", "Projekt", "Rollback-Kopie", "Abbrechen"];
    const found = german.filter((w) => all.includes(w));
    console.log("\nEN German leftovers:", found.length ? found : "none");
    assert.deepStrictEqual(found, []);

    console.log("\nALL CHECKS PASSED");
})().catch((e) => { console.error("FAILED:", e.stack || e.message); process.exit(1); });

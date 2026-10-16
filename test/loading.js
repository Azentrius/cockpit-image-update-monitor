// SPDX-License-Identifier: Apache-2.0
// Loading-indicator checks: runs the real diun.js in jsdom with a slow,
// failing or throwing Cockpit stub and asserts the visible state over time.
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { JSDOM } = require("jsdom");

const PLUGIN = path.join(__dirname, "..", "plugin");
const FIX = path.join(__dirname, "fixtures");
const html = fs.readFileSync(path.join(PLUGIN, "index.html"), "utf8")
    .replace(/<script[^>]*><\/script>/g, "");
const js = fs.readFileSync(path.join(PLUGIN, "diun.js"), "utf8");
const inventory = fs.readFileSync(path.join(FIX, "inventory3.jsonl"), "utf8");
const files = {
    "/var/lib/diun/events.jsonl": fs.readFileSync(path.join(FIX, "events.jsonl"), "utf8"),
    "/var/lib/diun-radar/versions.json": JSON.parse(fs.readFileSync(path.join(FIX, "versions2.json"), "utf8")),
    "/var/lib/diun/dismissed.json": null,
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// Every load also lists the backups (both tabs load together).
const BACKUPS_EMPTY = JSON.stringify({ dumps: [], images: [], ops: [], dump_bytes: 0, free_bytes: 0, dir: "/var/backups/image-updates" });

function boot(spawnImpl) {
    const dom = new JSDOM(html, { runScripts: "outside-only" });
    const w = dom.window;
    let spawns = 0;
    w.Date.now = () => Date.parse("2026-10-12T02:50:00Z");
    w.cockpit = {
        language: "de",
        file(p) {
            return {
                read() { return Promise.resolve(files[p] === undefined ? null : files[p]); },
                watch(cb) { cb(files[p], "t1"); },
                replace() { return Promise.resolve(); },
            };
        },
        spawn(cmd) {
            if (cmd[0].endsWith("diun-update.py")) return Promise.resolve(BACKUPS_EMPTY);
            if (cmd[0].endsWith("diun-deps.sh")) return Promise.resolve('{"root":true,"docker":{"state":"ok","version":"29.5.2","compose":true},"diun":{"state":"running","managed":true,"events":true,"can_install":false,"can_start":false},"skopeo":{"state":"ok","version":"1.13.3","can_install":true},"radar":{"state":"active","can_install":true}}');
            spawns++; return spawnImpl();
        },
        transport: { wait() {} },
    };
    const d = w.document;
    const state = () => ({
        loader: !d.getElementById("dm-loading").hidden,
        btnDisabled: d.getElementById("dm-refresh").disabled,
        btnLabel: d.querySelector("#dm-refresh .dm-btn-label").textContent,
        btnBusy: d.getElementById("dm-refresh").classList.contains("is-busy"),
        tableVisible: !d.getElementById("dm-table").hidden,
        wrapBusy: d.getElementById("dm-table-wrap").classList.contains("is-busy"),
        ariaBusy: d.getElementById("dm-table-wrap").getAttribute("aria-busy"),
        rows: d.querySelectorAll("#dm-rows tr").length,
        status: d.getElementById("dm-status").hidden ? "" : d.getElementById("dm-status").textContent,
    });
    // Before diun.js runs: static HTML alone must already show the loader.
    const before = state();
    w.eval(js);
    return { w, d, state, before, spawns: () => spawns };
}

(async () => {
    const slow = () => new Promise((r) => setTimeout(() => r(inventory), 300));

    // 1. First load, slow inventory
    const a = boot(slow);
    console.log("vor JS        :", JSON.stringify(a.before));
    assert(a.before.loader && a.before.btnDisabled && !a.before.tableVisible, "loader must show in static HTML");
    await sleep(100);
    const during = a.state();
    console.log("while loading :", JSON.stringify(during));
    assert(during.loader && during.btnDisabled && during.btnBusy && !during.tableVisible && during.ariaBusy === "true");
    await sleep(400);
    const after = a.state();
    console.log("nach Laden    :", JSON.stringify(after));
    assert(!after.loader && !after.btnDisabled && !after.btnBusy && after.tableVisible && after.rows === 31 && after.ariaBusy === "false");
    assert.strictEqual(after.btnLabel, "Aktualisieren");

    // 2. Refresh click: table stays, dimmed; first-load block stays hidden
    a.d.getElementById("dm-refresh").click();
    await sleep(100);
    const refreshing = a.state();
    console.log("Aktualisieren :", JSON.stringify(refreshing));
    assert(!refreshing.loader && refreshing.btnDisabled && refreshing.wrapBusy && refreshing.tableVisible && refreshing.rows === 31);
    assert.strictEqual(refreshing.btnLabel, "Lädt …");
    await sleep(400);
    const refreshed = a.state();
    console.log("danach        :", JSON.stringify(refreshed));
    assert(!refreshed.btnDisabled && !refreshed.wrapBusy && refreshed.rows === 31);
    assert.strictEqual(a.spawns(), 2, "exactly one extra inventory run");

    // 3. Inventory fails -> loader gone, fallback banner, button usable again
    const b = boot(() => Promise.reject(new Error("not permitted")));
    await sleep(100);
    const failed = b.state();
    console.log("inventory error:", JSON.stringify(failed));
    assert(!failed.loader && !failed.btnDisabled && failed.status.includes("Inventar nicht verfügbar"));

    // 4. spawn throws synchronously -> indicator must not get stuck
    const c = boot(() => { throw new Error("boom"); });
    await sleep(100);
    const thrown = c.state();
    console.log("spawn wirft   :", JSON.stringify(thrown));
    assert(!thrown.loader && !thrown.btnDisabled && thrown.status.startsWith("Die Übersicht konnte nicht geladen werden") &&
           thrown.status.endsWith("Details: boom"));

    console.log("\nALL CHECKS PASSED");
})().catch((e) => { console.error("FEHLGESCHLAGEN:", e.message); process.exit(1); });

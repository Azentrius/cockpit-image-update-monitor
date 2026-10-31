// SPDX-License-Identifier: Apache-2.0
// The status for the Status Board (lib/diunstatus.py) counts the rows the
// page shows: with the fixtures, the page marks the same
// containers "Update available", "Roll-out pending" and "New version" as
// test/fixtures/status.json names. test/status_test.py checks that the helper
// writes exactly that file for the same fixtures.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const PLUGIN = path.join(__dirname, "..", "plugin");
const FIX = path.join(__dirname, "fixtures");
const html = fs.readFileSync(path.join(PLUGIN, "index.html"), "utf8");
// The page's scripts as index.html loads them (test/page-scripts.js).
const js = require("./page-scripts").pageScripts();
const files = {
    "/var/lib/diun/events.jsonl": fs.readFileSync(path.join(FIX, "events.jsonl"), "utf8"),
    "/var/lib/diun-radar/versions.json": JSON.parse(fs.readFileSync(path.join(FIX, "versions2.json"), "utf8")),
    "/var/lib/diun/dismissed.json": null,
};
const inventory = fs.readFileSync(path.join(FIX, "inventory3.jsonl"), "utf8");
const status = JSON.parse(fs.readFileSync(path.join(FIX, "status.json"), "utf8"));
const DEPS = '{"root":true,"docker":{"state":"ok","version":"29.5.2","compose":true},' +
    '"diun":{"state":"running","managed":true,"events":true,"can_install":false,"can_start":false},' +
    '"skopeo":{"state":"ok","version":"1.13.3","can_install":true},"radar":{"state":"active","can_install":true}}';

(async () => {
    const dom = new JSDOM(html, { runScripts: "outside-only" });
    const w = dom.window;
    w.document.querySelectorAll("script").forEach((s) => s.remove());   // the page's own tags never run
    w.Date.now = () => Date.parse(status.checked_at);          // the time the fixture was made for
    w.cockpit = {
        language: "en",
        file(p) {
            return {
                read: () => Promise.resolve(files[p] === undefined ? null : files[p]),
                watch(cb) { cb(files[p], "tag-1"); },
                replace(obj) { files[p] = obj; return Promise.resolve(); },
            };
        },
        spawn(cmd) {
            if (cmd[0].endsWith("diun-deps.sh")) return Promise.resolve(DEPS);
            if (cmd[0].endsWith("diun-update.py")) return Promise.resolve('{"ops": [], "dumps": [], "images": []}');
            return Promise.resolve(inventory);
        },
        transport: { wait() {} },
    };
    w.eval(js);
    await new Promise((r) => setTimeout(r, 100));

    const KIND = { "Update available": "update", "Roll-out pending": "pulled", "New version": "newver" };
    const page = { update: [], pulled: [], newver: [] };
    const rows = [...w.document.querySelectorAll("#dm-rows tr")];
    rows.forEach((tr) => {
        const label = tr.querySelector('td[data-label="Status"] .az-label');
        const kind = KIND[label ? label.textContent.trim() : ""];
        if (kind) page[kind].push(tr.querySelector(".dm-name").textContent);
    });
    Object.values(page).forEach((names) => names.sort());
    const helper = { update: status.facts.updates.names, pulled: status.facts.rollout.names,
                     newver: status.facts["new-versions"].names };
    console.log(`page: ${rows.length} rows; update ${page.update.length}, roll-out ${page.pulled.length}, ` +
                `new version ${page.newver.length}`);
    console.log("helper:", status.summary);
    assert(rows.length > 20, "the page drew the table");
    assert.deepStrictEqual(page, helper);
    const count = (id) => status.items.find((it) => it.id === id).value;
    assert.deepStrictEqual([count("updates"), count("rollout"), count("new-versions")],
                           [page.update, page.pulled, page.newver].map((x) => String(x.length)));
    console.log("\nALL CHECKS PASSED: the helper's status counts the page's rows");
})().catch((e) => { console.error(e); process.exit(1); });

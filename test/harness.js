// SPDX-License-Identifier: Apache-2.0
// Runs the real diun.js against a stubbed Cockpit API inside jsdom and
// prints the rendered table as text. Scenarios cover the normal case, a
// missing radar file, an unavailable inventory, and the watch/reload loop.
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const PLUGIN = path.join(__dirname, "..", "plugin");
const FIX = path.join(__dirname, "fixtures");
const html = fs.readFileSync(path.join(PLUGIN, "index.html"), "utf8")
    .replace(/<script[^>]*><\/script>/g, "");          // we inject scripts ourselves
// The page as index.html loads it: the texts (i18n.js), then the code (diun.js).
const js = ["i18n.js", "diun.js"].map((f) => fs.readFileSync(path.join(PLUGIN, f), "utf8")).join("\n");
const fixtures = {
    "/var/lib/diun/events.jsonl": fs.readFileSync(path.join(FIX, "events.jsonl"), "utf8"),
    "/var/lib/diun-radar/versions.json": JSON.parse(fs.readFileSync(path.join(FIX, "versions2.json"), "utf8")),
    "/var/lib/diun/dismissed.json": null,
};
const inventory = fs.readFileSync(path.join(FIX, "inventory3.jsonl"), "utf8");

async function scenario(name, { files = fixtures, spawnOk = true, now = "2026-10-12T02:50:00Z" } = {}) {
    const dom = new JSDOM(html, { runScripts: "outside-only" });
    const w = dom.window;
    const calls = { spawn: [], reads: 0 };
    const watchers = [];
    const tags = {};           // current file tag per path, like Cockpit's
    w.__setTag = (p, t) => { tags[p] = t; };
    w.Date.now = () => Date.parse(now);
    w.cockpit = {
        language: "de",
        file(p) {
            return {
                read() {
                    calls.reads++;
                    // Real Cockpit re-fires watch callbacks with the same tag on read.
                    watchers.filter((x) => x.p === p).forEach((x) => x.cb(files[p], tags[p] || "tag-1"));
                    return Promise.resolve(files[p] === undefined ? null : files[p]);
                },
                watch(cb) { watchers.push({ p, cb }); cb(files[p], tags[p] || "tag-1"); },
                replace(obj) { files[p] = obj; return Promise.resolve(); },
            };
        },
        spawn(cmd) {
            if (cmd[0].endsWith("diun-deps.sh")) return Promise.resolve('{"root":true,"docker":{"state":"ok","version":"29.5.2","compose":true},"diun":{"state":"running","managed":true,"events":true,"can_install":false,"can_start":false},"skopeo":{"state":"ok","version":"1.13.3","can_install":true},"radar":{"state":"active","can_install":true}}');
            calls.spawn.push(cmd.join(" "));
            return spawnOk ? Promise.resolve(inventory) : Promise.reject(new Error("not permitted"));
        },
        transport: { wait() {} },
    };
    w.eval(js);
    await new Promise((r) => setTimeout(r, 50));

    const d = w.document;
    console.log(`\n=== ${name} ===`);
    console.log("meta  :", d.getElementById("dm-meta").textContent);
    const st = d.getElementById("dm-status");
    if (!st.hidden) console.log("status:", st.textContent);
    console.log("spawn :", calls.spawn.length, "x", [...new Set(calls.spawn)].join(", "));
    const heads = [...d.querySelectorAll("thead th")].map((t) => t.textContent);
    console.log("cols  :", heads.join(" | "));
    [...d.querySelectorAll("#dm-rows tr")].forEach((tr) => {
        const c = [...tr.querySelectorAll("td")];
        const levels = [...c[4].querySelectorAll(".dm-level")].map((x) => `${x.dataset.reach}:${x.textContent}`);
        const verText = levels.length ? levels.join(", ") : c[4].textContent.trim();
        const inst = c[3].querySelector("[title]");
        console.log(
            c[0].textContent.padEnd(24),
            c[2].textContent.padEnd(18),
            c[3].textContent.trim().padEnd(15),
            (inst ? inst.title.split("\n")[0].slice(0, 34) : "").padEnd(35),
            verText.padEnd(62),
            c[5].textContent.padEnd(20),
            c[6].textContent.trim()
        );
    });
    return { w, calls, watchers };
}

(async () => {
    await scenario("A: normal (real inventory + radar)");
    await scenario("B: radar file missing", { files: { ...fixtures, "/var/lib/diun-radar/versions.json": null } });
    await scenario("C: inventory unavailable", { spawnOk: false });
    await scenario("D: radar 30 h old", { now: "2026-10-13T06:40:54Z" });

    // E: a changed versions.json tag must trigger exactly one reload, same tag none.
    const { w, calls, watchers } = await scenario("E: watch behaviour");
    const inventoryRuns = () => calls.spawn.filter((c) => c.includes("diun-inventory.py")).length;
    const before = inventoryRuns();
    const vw = watchers.find((x) => x.p.endsWith("versions.json"));
    vw.cb(fixtures["/var/lib/diun-radar/versions.json"], "tag-1");   // same tag
    await new Promise((r) => setTimeout(r, 50));
    const same = inventoryRuns() - before;
    w.__setTag("/var/lib/diun-radar/versions.json", "tag-2");        // file changed on disk
    vw.cb(fixtures["/var/lib/diun-radar/versions.json"], "tag-2");
    await new Promise((r) => setTimeout(r, 50));
    console.log(`reloads after same tag: ${same}, after new tag: ${inventoryRuns() - before - same}`);
})();

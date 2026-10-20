// SPDX-License-Identifier: Apache-2.0
// A page that did not load all its scripts. After an update of the add-on in a
// running Cockpit session, Cockpit's bridge may still serve the list of files
// it read first, and a script new in this version answers 404 (Cockpit 314,
// CI, 2026-10-20). The page then asks the bridge to read the packages again
// (cockpit.Packages.Reload on the internal bus) and loads itself once more;
// when a part is still missing after that, it says so instead of loading in a
// loop. Every script runs on its own, as in the browser, so one that fails
// cannot hide another; none may throw. Against the real scripts in jsdom.
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { JSDOM, VirtualConsole } = require("jsdom");
const { pageScriptList } = require("./page-scripts");

const html = fs.readFileSync(path.join(__dirname, "..", "plugin", "index.html"), "utf8");
const SCRIPTS = pageScriptList();
const RELOADED = "image-updates:reloaded:" + /const VERSION = "([^"]+)"/.exec(SCRIPTS.find(([f]) => f === "diun.js")[1])[1];

// without: the scripts Cockpit did not deliver; reloadAnswer: how the bridge answers Reload.
async function boot({ without = [], language = "en", reloaded = false, reloadAnswer = "ok" } = {}) {
    const events = { reloads: 0, calls: [], spawns: [], thrown: [] };
    const vc = new VirtualConsole();
    vc.on("jsdomError", (e) => {
        if (/Not implemented: navigation/.test(e.message)) events.reloads++;     // location.reload()
        else events.thrown.push(e.message);
    });
    const dom = new JSDOM(html, { url: "https://localhost:9090/cockpit/@localhost/image-updates/index.html",
                                  runScripts: "outside-only", virtualConsole: vc });
    const w = dom.window;
    w.document.querySelectorAll("script").forEach((s) => s.remove());   // the page's own tags never run
    if (reloaded) w.sessionStorage.setItem(RELOADED, "1");
    w.cockpit = {
        language,
        file() { return { read: () => Promise.resolve(null), watch: () => {}, replace: () => Promise.resolve() }; },
        spawn(cmd) {
            events.spawns.push(cmd[0].split("/").pop());
            const p = Promise.reject(new Error("not permitted"));
            p.stream = () => p;
            return p;
        },
        dbus(name, opts) {
            return {
                call(...args) {
                    events.calls.push([name, opts, ...args]);
                    return reloadAnswer === "ok" ? Promise.resolve([]) : Promise.reject(new Error("no such method"));
                },
            };
        },
        transport: { wait() {} },
    };
    for (const [name, code] of SCRIPTS) {
        if (without.includes(name)) continue;
        try { w.eval(code); } catch (e) { events.thrown.push(`${name}: ${e.message}`); }
    }
    await new Promise((r) => setTimeout(r, 30));
    return { w, d: w.document, events };
}

(async () => {
    // 1. Everything there: the page starts, no reload, the mark of an earlier reload gone.
    const ok = await boot({ reloaded: true });
    assert.deepStrictEqual([ok.events.calls.length, ok.events.reloads, ok.events.thrown], [0, 0, []]);
    assert(ok.events.spawns.includes("diun-inventory.py"), "the page started: " + ok.events.spawns);
    assert.strictEqual(ok.w.sessionStorage.getItem(RELOADED), null, "the mark is gone once all parts loaded");

    // 2. Scripts new in this version missing: read the packages again, load once more; nothing thrown.
    for (const without of [["dialog.js", "notes.js"], ["common.js"], ["common.js", "dialog.js", "notes.js", "update.js",
                                                                          "retag.js", "overview.js", "backups.js"], ["backups.js"]]) {
        const x = await boot({ without });
        console.log(`without ${without.join(", ")}: calls ${JSON.stringify(x.events.calls)}, reloads ${x.events.reloads}, thrown ${x.events.thrown.length}`);
        assert.strictEqual(JSON.stringify(x.events.calls),      // objects of the page's window: compared as JSON
                           JSON.stringify([[null, { bus: "internal" }, "/packages", "cockpit.Packages", "Reload", []]]));
        assert.strictEqual(x.events.reloads, 1, "loads itself once more");
        assert.deepStrictEqual(x.events.thrown, [], "no script throws");
        assert.deepStrictEqual(x.events.spawns, [], "nothing runs on the server");
        assert.strictEqual(x.w.sessionStorage.getItem(RELOADED), "1");
        assert(x.d.getElementById("dm-status").hidden, "no message yet");
    }
    // The bridge does not know Reload (or the call fails): load once more anyway.
    const old = await boot({ without: ["notes.js"], reloadAnswer: "fail" });
    assert.deepStrictEqual([old.events.calls.length, old.events.reloads, old.events.thrown], [1, 1, []]);

    // 3. Still missing after that load: a message, no loop, the loading block gone.
    for (const [language, text] of [
        ["en", "The page did not load completely. Cockpit did not deliver dialog.js, notes.js of this version of the add-on, not even after reading its packages again. Log out of Cockpit and in again."],
        ["de", "Die Seite wurde nicht vollständig geladen. Cockpit hat dialog.js, notes.js dieser Version des Add-ons auch nach erneutem Einlesen der Pakete nicht ausgeliefert. Melden Sie sich bei Cockpit ab und wieder an."]]) {
        const x = await boot({ without: ["dialog.js", "notes.js"], language, reloaded: true });
        const st = x.d.getElementById("dm-status");
        console.log(`again (${language}): ${st.textContent}`);
        assert.deepStrictEqual([x.events.calls.length, x.events.reloads, x.events.thrown], [0, 0, []]);
        assert.deepStrictEqual([st.hidden, st.getAttribute("role"), st.classList.contains("az-alert--danger"), st.textContent],
                               [false, "alert", true, text]);
        assert(x.d.getElementById("dm-loading").hidden, "the loading block is gone");
    }
    // Without common.js the texts still come from i18n.js.
    const bare = await boot({ without: ["common.js"], language: "de", reloaded: true });
    assert(bare.d.getElementById("dm-status").textContent.startsWith("Die Seite wurde nicht vollständig geladen."));
    console.log("partial load: all checks passed");
})().catch((e) => { console.error(e); process.exit(1); });

// SPDX-License-Identifier: Apache-2.0
// Where the Status Board's links lead: the places of the page (PLACES in
// plugin/diun.js) with the real i18n.js, menu-status.js and diun.js in jsdom,
// and the links of the status (LINKS in lib/diunstatus.py, as in
// test/fixtures/status.json):
//   1. opened at a place: its tab at once, then its first row of that status
//      once the rows are there, or the update history once it is there;
//   2. the page stays loaded and Cockpit only changes its location
//      (locationchanged): every new place again, a tab chosen on the page goes
//      into the location (so a link to the place shown before is a change),
//      a later reload scrolls nowhere;
//   3. no place: the first tab; a path that is no place changes nothing; a
//      status of no row there: only the tab;
//   4. preloaded hidden: nothing scrolls before the page is shown and loaded,
//      whether the location or the shell's hint comes first;
//   5. every link of the status matches the Status Board's rule and leads to
//      a place with the rows that item counts.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const PLUGIN = path.join(__dirname, "..", "plugin");
const FIX = path.join(__dirname, "fixtures");
const html = fs.readFileSync(path.join(PLUGIN, "index.html"), "utf8");
// The page as index.html loads it: the texts, the menu's status, the code.
// The page's scripts as index.html loads them, menu-status.js too (test/page-scripts.js).
const js = require("./page-scripts").pageScripts({ menu: true });
const STATUS = "/var/lib/diun-radar/status.json";
const statusText = fs.readFileSync(path.join(FIX, "status.json"), "utf8");
const status = JSON.parse(statusText);
const inventory = fs.readFileSync(path.join(FIX, "inventory3.jsonl"), "utf8");
const preview = JSON.parse(fs.readFileSync(path.join(FIX, "preview.json"), "utf8"));
const files = {
    "/var/lib/diun/events.jsonl": fs.readFileSync(path.join(FIX, "events.jsonl"), "utf8"),
    "/var/lib/diun-radar/versions.json": JSON.parse(fs.readFileSync(path.join(FIX, "versions2.json"), "utf8")),
    "/var/lib/diun/dismissed.json": null,
    "/etc/hostname": "web1",
};
const DEPS = '{"root":true,"docker":{"state":"ok","version":"29.5.2","compose":true},' +
    '"diun":{"state":"running","managed":true,"events":true,"can_install":false,"can_start":false},' +
    '"skopeo":{"state":"ok","version":"1.13.3","can_install":true},"radar":{"state":"active","can_install":true}}';
// The Status Board takes a link only when it matches (LINK_RE in its src/lib/status.js).
const LINK_RE = /^#\/[A-Za-z0-9/_-]{0,80}$/;
const NOW = Date.parse(status.checked_at);
// Also the frame's hashchange after a location the page set (a timer of 0 ms in the stub below).
const flush = async () => {
    await new Promise((r) => setTimeout(r, 5));
    for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
};

// at: the location the frame is loaded with ("#/backups/history" is the path ["backups", "history"]).
function boot({ at = "", hidden = false, holdBackups = false } = {}) {
    const dom = new JSDOM(html, { runScripts: "outside-only" });
    const w = dom.window;
    w.document.querySelectorAll("script").forEach((s) => s.remove());   // the page's own tags never run
    w.Date.now = () => NOW;
    // Timers of a second and more (the menu's check when the radar turns outdated) never run here.
    const setTimeout = w.setTimeout.bind(w);
    w.setTimeout = (fn, ms) => (ms >= 1000 ? 0 : setTimeout(fn, ms));
    const s = { w, scrolled: [], went: [], spawns: [], statusWatch: [] };
    // What scrollIntoView was called on: a row as "<kind> <container>", a card by its title.
    w.Element.prototype.scrollIntoView = function () {
        const name = this.querySelector(".dm-name");
        const title = this.querySelector(".az-card__title");
        s.scrolled.push(name ? `${this.dataset.kind} ${name.textContent}` : title ? title.id : this.id);
    };
    const listeners = { locationchanged: new Set(), visibilitychange: new Set() };
    const fire = (type) => [...listeners[type]].forEach((fn) => fn());
    let release = null;
    const location = (p) => ({
        path: p ? p.replace(/^#\//, "").split("/").filter(Boolean) : [],
        // cockpit.js changes the frame's hash; the frame's hashchange comes later.
        go(next) {
            s.went.push(next.join("/"));
            w.cockpit.location = location("#/" + next.join("/"));
            w.setTimeout(() => fire("locationchanged"), 0);
        },
    });
    w.cockpit = {
        language: "en",
        hidden,
        location: location(at),
        addEventListener(type, fn) { if (listeners[type]) listeners[type].add(fn); },
        removeEventListener(type, fn) { if (listeners[type]) listeners[type].delete(fn); },
        file(p) {
            return {
                read() { return Promise.resolve(files[p] === undefined ? null : files[p]); },
                watch(cb) { if (p === STATUS) s.statusWatch.push(cb); else cb(files[p], "t1"); },
                replace() { return Promise.resolve(); },
            };
        },
        spawn(cmd) {
            const verb = (cmd[0].split("/").pop() + " " + (cmd[1] || "")).trim();
            s.spawns.push(verb);
            let p;
            if (verb === "diun-deps.sh check") p = Promise.resolve(DEPS);
            else if (verb === "diun-update.py backups") {
                const answer = JSON.stringify(preview.backups);
                p = holdBackups ? new Promise((r) => { release = () => r(answer); }) : Promise.resolve(answer);
            } else if (verb === "diun-inventory.py") p = Promise.resolve(inventory);
            else p = Promise.reject(new Error("unscripted " + verb));
            p.stream = () => p;
            return p;
        },
        transport: { wait() {}, control() {} },
    };
    // The shell: a jump to a place of this page, its hint, the answer about status.json.
    s.jump = (p) => { w.cockpit.location = location(p); fire("locationchanged"); };
    s.hint = (h) => { w.cockpit.hidden = h; fire("visibilitychange"); };
    s.answer = () => s.statusWatch.forEach((cb) => cb(statusText, "t1"));
    s.release = () => release && release();
    s.tab = () => w.document.querySelector('.az-tabs [aria-selected="true"]').dataset.tab;
    s.loaded = () => s.spawns.includes("diun-inventory.py");
    s.$ = (id) => w.document.getElementById(id);
    w.eval(js);
    return s;
}

(async () => {
    // 1. Opened at a place (the frame is new, its location is the link).
    for (const [link, tab, first] of [
        ["#/updates/update", "updates", "update inference"],
        ["#/updates/rollout", "updates", "pulled bugs-postgres"],
        ["#/updates/new-versions", "updates", "newver booking-db"],
        ["#/backups/history", "backups", "dm-ops-title"],
    ]) {
        const a = boot({ at: link, holdBackups: true });
        assert.strictEqual(a.tab(), tab, `${link}: the tab at once`);
        assert.deepStrictEqual(a.scrolled, [], `${link}: nothing to scroll to before the answer`);
        a.answer();
        await flush();
        assert(a.loaded(), `${link}: the page loads`);
        const beforeHistory = a.scrolled.slice();
        a.release();
        await flush();
        console.log(`opened at ${link.padEnd(24)} tab ${a.tab().padEnd(8)} scrolled to ${a.scrolled.join(", ")}`);
        assert.deepStrictEqual(a.scrolled, [first], `${link}: once, to ${first}`);
        assert.deepStrictEqual(beforeHistory, tab === "backups" ? [] : [first],
                               `${link}: the rows when they are there, the history when it is there`);
        assert.deepStrictEqual(a.went, [], `${link}: the page itself changes no location`);
        assert.strictEqual(a.$("dm-tab-" + (tab === "updates" ? "backups" : "updates")).hidden, true, "the other tab hidden");
    }

    // 2. The page stays loaded; Cockpit changes its location only.
    const b = boot();
    b.answer();
    await flush();
    assert.deepStrictEqual([b.tab(), b.scrolled], ["updates", []], "no place: the first tab, no scrolling");
    b.jump("#/backups/history");
    await flush();
    assert.deepStrictEqual([b.tab(), b.scrolled], ["backups", ["dm-ops-title"]], "a jump to the history");
    b.jump("#/updates/rollout");
    await flush();
    assert.deepStrictEqual([b.tab(), b.scrolled.slice(1)], ["updates", ["pulled bugs-postgres"]], "a jump to the roll-outs");
    // Chosen on the page: the tab goes into the location, then the link to the history changes it again.
    b.$("dm-tab-btn-backups").click();
    await flush();
    assert.deepStrictEqual([b.tab(), b.went, b.w.cockpit.location.path], ["backups", ["backups"], ["backups"]],
                           "a tab chosen on the page goes into the location");
    b.$("dm-tab-btn-updates").click();
    await flush();
    b.jump("#/backups/history");
    await flush();
    assert.deepStrictEqual([b.tab(), b.scrolled.slice(2)], ["backups", ["dm-ops-title"]],
                           "after a tab chosen on the page, the link to the history leads there again");
    // The keyboard chooses too; the tab already in the location is not set again.
    b.$("dm-tab-btn-backups").focus();
    b.$("dm-tab-btn-backups").dispatchEvent(new b.w.KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
    b.$("dm-tab-btn-updates").click();
    await flush();
    assert.deepStrictEqual([b.tab(), b.went], ["updates", ["backups", "updates", "updates"]],
                           "keyboard: into the location; the same tab again: not set twice");
    // A reload (Refresh, a new radar run) scrolls nowhere: the link was followed already.
    const scrolls = b.scrolled.length;
    b.$("dm-refresh").click();
    await flush();
    assert.strictEqual(b.scrolled.length, scrolls, "a reload scrolls nowhere");
    console.log("stays loaded: jumps", b.scrolled.join(", "), "| location set by the page:", b.went.join(", "));

    // 3. No place, no such place, no row of that status.
    b.jump("#/backups/history");
    await flush();
    b.jump("");
    await flush();
    assert.strictEqual(b.tab(), "updates", "no place: the first tab");
    b.jump("#/backups");
    await flush();
    const n = b.scrolled.length;
    b.jump("#/nowhere/at-all");
    await flush();
    b.jump("#/constructor");
    await flush();
    b.jump("#/updates/toString");
    await flush();
    assert.deepStrictEqual([b.tab(), b.scrolled.length], ["updates", n],
                           "a path that is no place: nothing changes; a part that is none: only the tab");
    const quiet = boot({ at: "#/updates/update" });
    files["/var/lib/diun/events.jsonl"] = "";     // no Diun event: no row "Update available"
    quiet.answer();
    await flush();
    files["/var/lib/diun/events.jsonl"] = fs.readFileSync(path.join(FIX, "events.jsonl"), "utf8");
    assert.deepStrictEqual([quiet.tab(), quiet.scrolled], ["updates", []], "no row of that status: the tab only");

    // 4. Preloaded hidden: the location first, then the hint; and the other way round.
    const c = boot({ hidden: true });
    c.hint(true);
    c.answer();
    await flush();
    c.jump("#/updates/new-versions");
    await flush();
    assert.deepStrictEqual([c.tab(), c.scrolled, c.loaded()], ["updates", [], false], "hidden: the tab, nothing else");
    c.hint(false);
    await flush();
    assert.deepStrictEqual(c.scrolled, ["newver booking-db"], "shown: loaded, then scrolled once");
    const d = boot({ hidden: true });
    d.hint(true);
    d.answer();
    await flush();
    d.hint(false);
    d.jump("#/backups/history");
    await flush();
    assert.deepStrictEqual([d.tab(), d.scrolled], ["backups", ["dm-ops-title"]], "shown first, then the location");
    const e = boot({ hidden: true, at: "#/backups/history" });
    e.hint(true);
    e.answer();
    await flush();
    assert.deepStrictEqual([e.tab(), e.scrolled, e.loaded()], ["backups", [], false], "loaded hidden at a place");
    e.hint(false);
    await flush();
    assert.deepStrictEqual(e.scrolled, ["dm-ops-title"], "shown: the place it was loaded at");
    console.log("preloaded   :", c.scrolled.join(", "), "|", d.scrolled.join(", "), "|", e.scrolled.join(", "));

    // 5. The links of the status: the Status Board's rule, a place, the rows the item counts.
    const kinds = { updates: "update", rollout: "pulled", "new-versions": "newver" };
    for (const it of status.items) {
        assert(LINK_RE.test(it.link || ""), `${it.id}: link ${it.link} as the Status Board takes it`);
        const f = boot({ at: it.link });
        f.answer();
        await flush();
        const rows = [...f.w.document.querySelectorAll("#dm-rows tr")];
        const marked = rows.filter((tr) => tr.dataset.kind === kinds[it.id]).map((tr) => tr.querySelector(".dm-name").textContent);
        console.log(`${it.id.padEnd(13)} ${it.link.padEnd(24)} tab ${f.tab().padEnd(8)} ${f.scrolled.join(", ") || "(top)"}`);
        if (kinds[it.id]) {
            assert.strictEqual(f.tab(), "updates");
            assert.strictEqual(String(marked.length), it.value, `${it.id}: the rows it counts are the rows of that place`);
            assert.deepStrictEqual(f.scrolled, [`${kinds[it.id]} ${marked[0]}`], `${it.id}: the first of them`);
        } else if (it.id === "failed") {
            assert.deepStrictEqual([f.tab(), f.scrolled], ["backups", ["dm-ops-title"]], "failed: the update history");
        } else {
            assert.deepStrictEqual([it.id, f.tab(), f.scrolled], ["radar", "updates", []], "radar: the updates tab, its header");
        }
    }

    console.log("\nALL CHECKS PASSED");
})().catch((err) => { console.error("FAILED:", err.stack || err.message); process.exit(1); });

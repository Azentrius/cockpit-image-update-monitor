// SPDX-License-Identifier: Apache-2.0
// The status icon in Cockpit's menu (plugin/menu-status.js) and the start of
// the page it guards, with the real i18n.js, menu-status.js and diun.js in
// jsdom. The Cockpit stub answers status.json only when the test says so, the
// way the bridge answers after the shell's hint has arrived:
//   1. preloaded, hidden: the icon from status.json, nothing else runs or
//      reads on the server; shown: the page loads;
//   2. opened directly (no hint, not hidden): the page loads at once;
//   3. German, a changed file, a missing file, the radar turning outdated;
//   4. no answer at all: the page still starts after 3 seconds;
//   5. the state and the tooltip equal the state and summary of
//      `diun-update.py status` (lib/diunstatus.py) for the same facts.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");
const { JSDOM } = require("jsdom");

const PLUGIN = path.join(__dirname, "..", "plugin");
const FIX = path.join(__dirname, "fixtures");
const html = fs.readFileSync(path.join(PLUGIN, "index.html"), "utf8")
    .replace(/<script[^>]*><\/script>/g, "");
// The page as index.html loads it: the texts, the menu's status, the code.
const js = ["i18n.js", "menu-status.js", "diun.js"].map((f) => fs.readFileSync(path.join(PLUGIN, f), "utf8")).join("\n");
const STATUS = "/var/lib/diun-radar/status.json";
const statusText = fs.readFileSync(path.join(FIX, "status.json"), "utf8");
const status = JSON.parse(statusText);
const inventory = fs.readFileSync(path.join(FIX, "inventory3.jsonl"), "utf8");
const files = {
    "/var/lib/diun/events.jsonl": fs.readFileSync(path.join(FIX, "events.jsonl"), "utf8"),
    "/var/lib/diun-radar/versions.json": JSON.parse(fs.readFileSync(path.join(FIX, "versions2.json"), "utf8")),
    "/var/lib/diun/dismissed.json": null,
    "/etc/hostname": "docker-host",
};
const DEPS = '{"root":true,"docker":{"state":"ok","version":"29.5.2","compose":true},' +
    '"diun":{"state":"running","managed":true,"events":true,"can_install":false,"can_start":false},' +
    '"skopeo":{"state":"ok","version":"1.13.3","can_install":true},"radar":{"state":"active","can_install":true}}';
const VERSION = /const VERSION = "([^"]+)"/.exec(fs.readFileSync(path.join(PLUGIN, "diun.js"), "utf8"))[1];
const NOW = Date.parse(status.checked_at);      // the time the fixture was made for
const H = 3600 * 1000;
const flush = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };
const plain = (o) => JSON.parse(JSON.stringify(o));     // objects of the jsdom realm, comparable

function boot({ language = "en" } = {}) {
    const dom = new JSDOM(html, { runScripts: "outside-only" });
    const w = dom.window;
    const clock = { now: NOW };
    w.Date.now = () => clock.now;
    // Timers of a second and more wait for the test to run them; shorter ones run.
    const later = [];
    const setTimeout = w.setTimeout.bind(w);
    const clearTimeout = w.clearTimeout.bind(w);
    w.setTimeout = (fn, ms) => {
        if (ms < 1000) return setTimeout(fn, ms);
        later.push({ fn, ms, id: 1e6 + later.length, live: true });
        return later[later.length - 1].id;
    };
    w.clearTimeout = (id) => {
        const t = later.find((x) => x.id === id);
        if (t) t.live = false;
        else clearTimeout(id);
    };
    const s = {
        w, clock, later,
        notes: [],          // what the page sent to the shell
        spawns: [],
        reads: [],          // files read or watched, except status.json
        statusWatch: [],    // status.json watchers, answered by the test
        waits: [],
    };
    const listeners = new Set();
    w.cockpit = {
        language,
        hidden: false,
        addEventListener(type, fn) { if (type === "visibilitychange") listeners.add(fn); },
        removeEventListener(type, fn) { listeners.delete(fn); },
        file(p, opts) {
            return {
                read() { s.reads.push(p); return Promise.resolve(files[p] === undefined ? null : files[p]); },
                watch(cb) {
                    if (p === STATUS) {
                        s.statusOpts = plain(opts);
                        s.statusWatch.push(cb);
                        return;
                    }
                    s.reads.push(p);
                    cb(files[p], "t1");
                },
                replace() { return Promise.resolve(); },
            };
        },
        spawn(cmd) {
            s.spawns.push(cmd.join(" "));
            if (cmd[0].endsWith("diun-deps.sh")) return Promise.resolve(DEPS);
            if (cmd[0].endsWith("diun-update.py")) return Promise.resolve('{"ops": [], "dumps": [], "images": []}');
            return Promise.resolve(inventory);
        },
        transport: {
            wait(cb) { s.waits.push(cb); },
            control(command, opts) { s.notes.push([command, plain(opts)]); },
        },
    };
    // The shell's hint: cockpit.js changes cockpit.hidden and tells its listeners.
    s.hint = (hidden) => {
        if (w.cockpit.hidden === hidden) return;
        w.cockpit.hidden = hidden;
        [...listeners].forEach((fn) => fn());
    };
    s.answer = (content) => s.statusWatch.forEach((cb) => cb(content, "t-" + s.notes.length));
    s.last = () => (s.notes.length ? s.notes[s.notes.length - 1][1].page_status : undefined);
    s.started = () => s.spawns.some((c) => c.includes("diun-inventory.py"));
    s.$ = (id) => w.document.getElementById(id);
    w.eval(js);
    return s;
}

const INFO_EN = { type: "info", title: "2 updates available, 3 roll-outs pending, 17 new versions" };

(async () => {
    // 1. Preloaded: the shell's hint (hidden) comes before the answer.
    const a = boot();
    assert.deepStrictEqual(a.statusOpts, { superuser: "try" }, "status.json with superuser try");
    a.hint(true);
    a.answer(statusText);
    await flush();
    console.log("preloaded :", JSON.stringify(a.notes));
    assert.deepStrictEqual(a.notes, [["notify", { page_status: INFO_EN }]]);
    assert.deepStrictEqual([a.spawns, a.reads], [[], []], "hidden: no helper, no other file");
    assert.strictEqual(a.$("dm-sig-version").textContent, "Image Update Monitor", "hidden: not started");
    assert(!a.$("dm-loading").hidden, "hidden: the first paint's loader stays");
    a.hint(false);
    await flush();
    const rows = a.w.document.querySelectorAll("#dm-rows tr").length;
    console.log("shown     :", a.spawns.length, "helper calls,", rows, "rows, read:", a.reads.join(" "));
    assert(a.started() && rows > 20 && a.$("dm-loading").hidden, "shown: the page loads");
    assert(a.reads.includes("/var/lib/diun/events.jsonl") && a.reads.includes("/var/lib/diun-radar/versions.json"));
    assert.strictEqual(a.$("dm-sig-version").textContent, "Image Update Monitor " + VERSION, "shown: started");
    a.hint(true);
    a.hint(false);
    await flush();
    assert.strictEqual(a.spawns.filter((c) => c.includes("diun-inventory.py")).length, 1, "shown again: no second start");

    // 2. Opened directly: no hint, the answer comes, the page starts.
    const b = boot();
    await flush();
    assert(!b.started(), "nothing before the first answer");
    b.answer(statusText);
    await flush();
    console.log("direct    :", b.spawns.length, "helper calls");
    assert(b.started());

    // 3. German; a changed file; the same again; a missing file.
    const c = boot({ language: "de" });
    c.answer(statusText);
    await flush();
    console.log("German    :", c.last().title);
    assert.deepStrictEqual(c.last(), { type: "info", title: "2 Updates verfügbar, 3 Container auszurollen, 17 neue Versionen" });
    const failed = plain(status);
    failed.facts.failed.ops = [{ name: "glitchtip-web", how: "failed" }];
    failed.facts.radar.generated = "2026-10-10T02:00:00Z";
    c.answer(JSON.stringify(failed));
    await flush();
    console.log("changed   :", JSON.stringify(c.last()));
    assert.deepStrictEqual(c.last(), { type: "error", title: "1 fehlgeschlagenes Update, Versions-Radar veraltet, " +
                                       "2 Updates verfügbar, 3 Container auszurollen, 17 neue Versionen" });
    const sent = c.notes.length;
    c.answer(JSON.stringify(failed));
    await flush();
    assert.strictEqual(c.notes.length, sent, "the same status is not sent again");
    c.answer(null);
    await flush();
    assert.strictEqual(c.last(), null, "no file: no icon");
    c.answer("{not json");
    await flush();
    assert.strictEqual(c.notes.length, sent + 1, "unreadable: still no icon, nothing new sent");
    const missing = boot();
    missing.answer(null);
    await flush();
    assert.deepStrictEqual(missing.notes, [["notify", { page_status: null }]], "the first status is always sent");

    // The radar turns outdated while the session stays open.
    const d = boot();
    const fresh = plain(status);
    fresh.facts.radar.generated = new Date(NOW - 23 * H).toISOString().replace(/\.\d+Z$/, "Z");
    d.answer(JSON.stringify(fresh));
    await flush();
    assert.deepStrictEqual(d.last(), INFO_EN);
    const due = d.later.filter((t) => t.live);
    console.log("radar due :", due.map((t) => t.ms / 1000 + " s").join(", "));
    assert.deepStrictEqual(due.map((t) => t.ms), [(3600 + 1) * 1000], "checked again when it is 24 hours old");
    d.clock.now += due[0].ms;
    due[0].fn();
    console.log("outdated  :", JSON.stringify(d.last()));
    assert.deepStrictEqual(d.last(), { type: "warning",
                                       title: "Version radar outdated, 2 updates available, 3 roll-outs pending, 17 new versions" });
    assert.deepStrictEqual(d.later.filter((t) => t.live).length, 0, "outdated: no further check");

    // 4. No answer ever: after 3 seconds the shell's hint has arrived; the page starts.
    const e = boot();
    e.waits.forEach((cb) => cb());
    await flush();
    assert(!e.started(), "not before the 3 seconds");
    const fallback = e.later.filter((t) => t.live);
    assert.deepStrictEqual(fallback.map((t) => t.ms), [3000]);
    fallback[0].fn();
    await flush();
    console.log("no answer :", e.started() ? "started after 3 s" : "not started");
    assert(e.started() && e.notes.length === 0);

    // 5. The same state and summary as `diun-update.py status` for the same facts.
    const at = (h) => new Date(NOW - h * H).toISOString().replace(/\.\d+Z$/, "Z");
    const base = plain(status.facts);
    const cases = [
        ["the fixture", base],
        ["failed, radar 30 h", { ...base, failed: { ops: [{ name: "a", how: "failed" }, { name: "b", how: "rollback" }] },
                                 radar: { found: true, generated: at(30) } }],
        ["one of each", { ...base, updates: { names: ["a"] }, failed: { ops: [{ name: "b", how: "aborted" }] },
                          rollout: { names: ["c"] }, "new-versions": { names: ["d"] },
                          radar: { found: true, generated: at(25) } }],
        ["nothing to do", { ...base, updates: { names: [] }, rollout: { names: [] }, "new-versions": { names: [] } }],
        ["no Diun, no radar", { ...base, updates: { names: null }, rollout: { names: [] }, "new-versions": { names: null },
                                radar: { found: false, generated: null } }],
        ["radar time unreadable", { ...base, radar: { found: true, generated: "yesterday" } }],
        ["radar time with offset", { ...base, radar: { found: true, generated: at(23).replace("T", " ").replace("Z", " +0200") } }],
        ["odd values", { ...base, failed: { ops: "x" }, updates: { names: {} }, rollout: 3 }],
    ];
    const code = [
        "import json, sys",
        "from datetime import datetime, timezone",
        "sys.path.insert(0, sys.argv[1])",
        "import diunstatus as S",
        "now = datetime.fromtimestamp(float(sys.argv[2]), timezone.utc)",
        "docs = [{lang: S.document(f, lang, now, 'x') for lang in ('en', 'de')} for f in json.load(sys.stdin)]",
        "print(json.dumps(docs, ensure_ascii=False))",
    ].join("\n");
    const python = JSON.parse(execFileSync("python3", ["-c", code, path.join(__dirname, "..", "lib"), String(NOW / 1000)],
                                           { input: JSON.stringify(cases.map((x) => x[1])), encoding: "utf8" }));
    const TYPE = { danger: "error", warning: "warning", info: "info" };
    for (const lang of ["en", "de"]) {
        const p = boot({ language: lang });
        cases.forEach(([name, facts], i) => {
            const doc = python[i][lang];
            const want = TYPE[doc.state] ? { type: TYPE[doc.state], title: doc.summary } : null;
            const got = plain(p.w.ImageUpdatesMenuStatus.statusOf({ ...status, facts }, NOW / 1000));
            console.log(`${lang} ${name.padEnd(22)} ${doc.state.padEnd(7)} ${got ? got.title : "no icon"}`);
            assert.deepStrictEqual(got, want, `${lang}: ${name}`);
        });
        assert.strictEqual(p.w.ImageUpdatesMenuStatus.statusOf({ ...status, facts: undefined }, NOW / 1000), null,
                           "without facts: unreadable, no icon (status: none)");
    }

    console.log("\nALL CHECKS PASSED");
})().catch((e) => { console.error("FAILED:", e.stack || e.message); process.exit(1); });

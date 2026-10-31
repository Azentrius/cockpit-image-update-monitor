// SPDX-License-Identifier: Apache-2.0
// Release notes in the dialogs (stage 3): the releases between the installed
// and the target version from the radar's releases.json, in the update dialog
// and in the preview of a version change; the text as text (no HTML, no
// images, links as their text), the words that call for care marked, and a
// note instead of an error when anything is missing. Against the real diun.js
// in jsdom with a scripted Cockpit stub.
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { JSDOM } = require("jsdom");

const PLUGIN = path.join(__dirname, "..", "plugin");
const FIX = path.join(__dirname, "fixtures");
const html = fs.readFileSync(path.join(PLUGIN, "index.html"), "utf8");
// The page's scripts as index.html loads them (test/page-scripts.js).
const js = require("./page-scripts").pageScripts();
const icons = fs.readFileSync(path.join(PLUGIN, "icons.js"), "utf8");
const inventory = fs.readFileSync(path.join(FIX, "inventory3.jsonl"), "utf8");
const RELEASES = JSON.parse(fs.readFileSync(path.join(FIX, "releases.json"), "utf8"));
const FILES = {
    "/var/lib/diun/events.jsonl": fs.readFileSync(path.join(FIX, "events.jsonl"), "utf8"),
    "/var/lib/diun-radar/versions.json": JSON.parse(fs.readFileSync(path.join(FIX, "versions2.json"), "utf8")),
    "/var/lib/diun/dismissed.json": null,
    "/var/lib/diun-radar/releases.json": RELEASES,
};
const DEPS_OK = JSON.stringify({ root: true, docker: { state: "ok", compose: true },
    diun: { state: "running", events: true }, skopeo: { state: "ok" }, radar: { state: "active" } });
const PLAN_OLLAMA = { container: "inference", image: "ollama/ollama:latest", updatable: true, project: "budget",
    workdir: "/opt/budget", services: ["inference"], containers: ["inference"], pulled: false, config_changed: false,
    dbs: [{ name: "budget-db", kind: "postgres", running: true, size: 52428800 }], free_bytes: 40 * 2 ** 30,
    installed_version: "0.30.11", tag_digits: 0, jump_unknown: false, target_version: "0.40.3", jump: "minor" };
const PLAN_GLITCH = { container: "bugs-postgres", image: "postgres:16-alpine", updatable: true, project: "bugs",
    workdir: "/opt/bugs", services: ["postgres"], containers: ["bugs-postgres"], pulled: true, config_changed: false,
    dbs: [{ name: "bugs-postgres", kind: "postgres", running: true, size: 1.2e8 }], free_bytes: 40 * 2 ** 30,
    installed_version: "16.14", tag_digits: 1, jump_unknown: false, target_version: "16.15", jump: "minor" };
const REPO = "ghcr.io/plausible/community-edition";
const PLAN_PLAUSIBLE = { container: "metrics", image: REPO + ":v2.1.4", updatable: true, project: "metrics",
    workdir: "/opt/metrics", services: ["metrics"], containers: ["metrics"], dbs: [], free_bytes: 40 * 2 ** 30,
    config_changed: false, pulled: false, installed_version: "2.1.4", tag_digits: 3, jump_unknown: false };
const TARGET = { "v2.1.5": ["2.1.5", "patch"], "v3.2.1": ["3.2.1", "major"] };
const dryRun = (tag) => ({ ...PLAN_PLAUSIBLE, target_version: TARGET[tag][0], jump: TARGET[tag][1],
    retag: { tag, from_tag: "v2.1.4", image: REPO + ":" + tag, choice: "newer", level: TARGET[tag][1], possible: true,
             changes: [{ file: "/opt/metrics/compose.yml", line: 12, old: "    image: " + REPO + ":v2.1.4",
                         new: "    image: " + REPO + ":" + tag, service: "metrics", sha256: "e".repeat(64) }],
             fingerprint: "a".repeat(64) } });
// Answers of the stubbed helpers that have not arrived yet, for every page of this file.
const pending = new Set();
const track = (p) => {
    const done = p.then(() => {}, () => {});
    pending.add(done);
    done.then(() => pending.delete(done));
};
async function settle() {
    for (let round = 0; round < 500; round++) {
        if (pending.size) await Promise.all([...pending]);
        await new Promise((r) => setImmediate(r));
        if (!pending.size) return;
    }
    throw new Error("the page did not settle: helper calls keep coming");
}

function boot(language, script, files) {
    const dom = new JSDOM(html, { runScripts: "outside-only" });
    const w = dom.window;
    w.document.querySelectorAll("script").forEach((s) => s.remove());   // the page's own tags never run
    w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
    w.HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
    w.Date.now = () => Date.parse("2026-10-12T02:50:00Z");
    const reads = [];
    const all = { ...FILES, ...(files || {}) };
    w.cockpit = {
        language,
        file(p) {
            return { read: () => { reads.push(p); return Promise.resolve(all[p] ?? null); },
                     watch: (cb) => cb(all[p], "t"), replace: () => Promise.resolve() };
        },
        spawn(cmd) {
            const tool = cmd[0].split("/").pop();
            const s = script[(tool + " " + (cmd[1] || "")).trim()] || script[tool];
            if (!s) return Promise.reject(new Error("unscripted " + cmd.join(" ")));
            const r = typeof s === "function" ? s(cmd) : s;
            const p = new Promise((resolve) => track(new Promise((sent) => setTimeout(() => { resolve(r.out ?? ""); sent(); }, 10))));
            p.stream = () => p;
            return p;
        },
        transport: { wait() {} },
    };
    w.eval(icons);
    w.eval(js);
    const d = w.document;
    return { w, d, $: (id) => d.getElementById(id), reads };
}
const BASE = {
    "diun-inventory.py": { out: inventory },
    "diun-deps.sh check": { out: DEPS_OK },
    "diun-update.py backups": { out: JSON.stringify({ dir: "/x", dumps: [], images: [], ops: [] }) },
};
const rowOf = (d, name) => [...d.querySelectorAll("#dm-rows tr")].find((tr) => tr.querySelector(".dm-name").textContent === name);
// The update dialog of a row with the given plan; returns the dialog body.
async function openUpdate(language, plan, files, name = "inference") {
    const x = boot(language, { ...BASE, "diun-update.py plan": { out: JSON.stringify(plan) } }, files);
    await settle();
    rowOf(x.d, name).querySelector("button[data-update]").click();
    await settle();
    return { ...x, body: x.$("dm-dialog-body") };
}
// Dates as the page writes them, in the time zone this test runs in.
const stamp = (iso, locale) => new Date(iso).toLocaleString(locale, { dateStyle: "short", timeStyle: "short" });
const day = (iso, locale) => new Date(iso).toLocaleDateString(locale, { dateStyle: "medium" });
const noteText = (body) => [...body.querySelectorAll(".dm-notes-slot .dm-dialog-note")].map((p) => p.textContent);

(async () => {
    // 1. The releases between installed and target, newest first, closed.
    const de = await openUpdate("de", PLAN_OLLAMA);
    const notes = de.body.querySelector(".dm-notes");
    assert(notes, "release notes in the update dialog");
    assert(!notes.open, "closed at first: the plan stays short");
    const summary = notes.querySelector(":scope > summary");
    console.log("summary de:", summary.textContent.trim());
    assert.strictEqual(summary.querySelector(".dm-notes-title").textContent, "Release Notes 0.30.11 → 0.40.3: 4 Releases");
    const flag = summary.querySelector(".az-label--warning");
    assert.strictEqual(flag.textContent, "erwähnt breaking, migration, deprecated");
    assert(flag.title.startsWith("Die Release Notes erwähnen inkompatible Änderungen"), flag.title);
    const rels = [...notes.querySelectorAll(".dm-release")];
    console.log("releases:", rels.map((r) => r.querySelector("summary").textContent.trim()).join(" | "));
    assert.deepStrictEqual(rels.map((r) => r.querySelector(".dm-release-tag").textContent), ["v0.40.3", "v0.40.0", "v0.31.0", "v0.30.12"],
                           "only (installed, target], newest first");
    assert(rels.every((r) => !r.open), "each release closed");
    assert.strictEqual(rels[1].querySelector("summary").textContent.trim(), `v0.40.0 New engine · ${day("2026-10-01T09:00:00Z", "de-DE")} breaking, migration`);
    // 2. The text as text: links as their text, images and HTML left out,
    //    angle brackets that are no HTML kept, nothing runs, care words marked.
    const b0 = rels[0].querySelector(".dm-release-body");
    console.log("body:", JSON.stringify(b0.textContent));
    assert.strictEqual(b0.textContent,
        "What's Changed\n• Fix a crash when loading large models\n• `Vec<T>` and <name> stay as text\n\n<script>alert(1)</script>\nLine\nbreak");
    assert.strictEqual(b0.querySelectorAll("*").length, 1, "one element: the heading");
    assert.strictEqual(b0.querySelector("strong").textContent, "What's Changed");
    assert.strictEqual(de.d.querySelectorAll("#dm-dialog img, #dm-dialog script").length, 0, "no image, no script in the dialog");
    const b1 = rels[1].querySelector(".dm-release-body");
    assert.strictEqual(b1.textContent, "BREAKING CHANGES\n• The API moved to /api/v2. A migration of stored models runs on the first start.");
    assert.deepStrictEqual([...b1.querySelectorAll("mark.dm-flag")].map((m) => m.textContent), ["BREAKING", "migration"]);
    assert.deepStrictEqual([...rels[3].querySelectorAll("mark.dm-flag")].map((m) => m.textContent), ["deprecated"]);
    // Nested and split markup (what a filter by text replacement could miss):
    // the text is escaped before anything is left out, so none of it is HTML.
    const TRICKY = "<!<!-- -->-- x --><img src=x onerror=alert(1)>\n<scr<script>ipt>alert(1)</script>\n" +
                   "<<b>img src=x onerror=alert(2)>\n<a href=\"javascript:alert(3)\">click</a>\n<iframe src=\"https://example.com\"></iframe>";
    const tr = await openUpdate("en", PLAN_OLLAMA, { "/var/lib/diun-radar/releases.json": { repos: { "ollama/ollama":
        { ...RELEASES.repos["ollama/ollama"], releases: [{ tag: "v0.40.3", version: "0.40.3", body: TRICKY }] } } } });
    const tb = tr.body.querySelector(".dm-release-body");
    console.log("tricky:", JSON.stringify(tb.textContent));
    assert.strictEqual(tb.querySelectorAll("*").length, 0, "no element from the text");
    assert.strictEqual(tr.d.querySelectorAll("#dm-dialog img, #dm-dialog script, #dm-dialog iframe, #dm-dialog-body a[href^='javascript']").length, 0);
    // 3. Empty text, a shortened one, links only to GitHub, new tab without opener.
    assert.strictEqual(rels[2].querySelector(".dm-dialog-note").textContent, "Kein Text.");
    assert.strictEqual(rels[2].querySelector("a"), null, "a link outside github.com is left out");
    assert.strictEqual(rels[3].querySelector(".dm-dialog-note").textContent, "Gekürzt; der ganze Text steht auf GitHub.");
    const a = rels[0].querySelector("a.dm-release-link");
    assert.deepStrictEqual([a.getAttribute("href"), a.target, a.rel, a.textContent],
        ["https://github.com/ollama/ollama/releases/tag/v0.40.3", "_blank", "noopener noreferrer", "Auf GitHub"]);
    assert.strictEqual(notes.querySelector(":scope > .dm-dialog-note").textContent, `Aus github.com/ollama/ollama, Stand ${stamp("2026-10-12T02:18:05Z", "de-DE")}.`);
    // 4. Its place: after the plan and the dump, before the warnings and the choice of steps.
    const kids = [...de.body.children];
    const at = (sel) => kids.findIndex((k) => k.matches(sel));
    console.log("order:", kids.map((k) => k.className || k.tagName).join(" > "));
    assert(at(".dm-kv") < at(".dm-notes-slot") && at(".dm-check") < at(".dm-notes-slot") && at(".dm-notes-slot") < at(".dm-steps"));
    assert(de.reads.includes("/var/lib/diun-radar/releases.json"), "read when the dialog opens");

    // 5. English, with no German in it.
    const en = await openUpdate("en", PLAN_OLLAMA);
    const enText = en.body.querySelector(".dm-notes").textContent;
    console.log("summary en:", en.body.querySelector(".dm-notes > summary").textContent.trim());
    assert.strictEqual(en.body.querySelector(".dm-notes-title").textContent, "Release notes 0.30.11 → 0.40.3: 4 releases");
    assert.strictEqual(en.body.querySelector(".dm-notes > summary .az-label--warning").textContent, "mentions breaking, migration, deprecated");
    for (const word of ["Kein", "Gekürzt", "Aus github", "Stand", "Auf GitHub", "erwähnt"]) assert(!enText.includes(word), "German in English: " + word);
    assert(enText.includes("From github.com/ollama/ollama, as of") && enText.includes("On GitHub") && enText.includes("Shortened; the whole text is on GitHub."));

    // 6. One release: the singular. No target version (a tag like latest that
    //    names none): every release since the installed version.
    const one = await openUpdate("en", { ...PLAN_OLLAMA, installed_version: "0.40.0" });
    assert.strictEqual(one.body.querySelector(".dm-notes-title").textContent, "Release notes 0.40.0 → 0.40.3: 1 release");
    const after = await openUpdate("de", { ...PLAN_OLLAMA, target_version: null, jump: null });
    assert.strictEqual(after.body.querySelector(".dm-notes-title").textContent, "Release Notes nach 0.30.11: 5 Releases");
    assert.deepStrictEqual([...after.body.querySelectorAll(".dm-release-tag")].map((x) => x.textContent),
                           ["v0.40.4", "v0.40.3", "v0.40.0", "v0.31.0", "v0.30.12"]);

    // 7. A note instead of an error when anything is missing (German and English).
    const states = [
        ["no source (postgres)", "de", PLAN_GLITCH, null, "bugs-postgres",
         "Keine Release Notes: Das Image nennt keine Quelle auf GitHub (Label org.opencontainers.image.source), und es ist keine bekannt."],
        ["not fetched yet", "de", PLAN_OLLAMA, { "/var/lib/diun-radar/releases.json": { generated: "x", repos: {} } }, "inference",
         "Die Release Notes von github.com/ollama/ollama sind noch nicht geholt; der Versions-Radar holt sie bei seinem nächsten Lauf."],
        ["no releases.json (older radar)", "en", PLAN_OLLAMA, { "/var/lib/diun-radar/releases.json": null }, "inference",
         "The release notes from github.com/ollama/ollama are not fetched yet; the version radar fetches them on its next run."],
        ["a repository without releases", "en", PLAN_OLLAMA,
         { "/var/lib/diun-radar/releases.json": { repos: { "ollama/ollama": { status: "missing", checked: "x" } } } }, "inference",
         "No release notes: github.com/ollama/ollama has no releases, or it is not public."],
        ["GitHub's limit", "de", PLAN_OLLAMA,
         { "/var/lib/diun-radar/releases.json": { repos: { "ollama/ollama": { status: "limited", error: "HTTP 403" } } } }, "inference",
         "Die Release Notes von github.com/ollama/ollama konnten nicht geholt werden: Das Limit von GitHub für Anfragen ohne Anmeldung war erreicht. Der Versions-Radar versucht es bei einem späteren Lauf wieder."],
        ["an error", "en", PLAN_OLLAMA,
         { "/var/lib/diun-radar/releases.json": { repos: { "ollama/ollama": { status: "error", error: "timed out" } } } }, "inference",
         "The release notes from github.com/ollama/ollama could not be fetched (timed out). The version radar tries again on its next run."],
        ["nothing in the range", "en", { ...PLAN_OLLAMA, installed_version: "0.40.3", target_version: "0.40.3.1" }, null, "inference",
         "No release on github.com/ollama/ollama between 0.40.3 and 0.40.3.1."],
        ["the installed version unknown", "de", { ...PLAN_OLLAMA, installed_version: null, jump: null }, null, "inference",
         "Keine Release Notes von github.com/ollama/ollama: Die installierte Version ist unbekannt und damit auch die Releases seither."],
        ["no target, nothing after the installed version", "en", { ...PLAN_OLLAMA, installed_version: "0.40.4", target_version: null, jump: null }, null, "inference",
         "No release on github.com/ollama/ollama after 0.40.4."],
        ["a broken releases.json", "en", PLAN_OLLAMA, { "/var/lib/diun-radar/releases.json": { repos: "x" } }, "inference",
         "The release notes from github.com/ollama/ollama are not fetched yet; the version radar fetches them on its next run."],
    ];
    for (const [label, lang, plan, files, name, want] of states) {
        const x = await openUpdate(lang, plan, files, name);
        console.log(`${label}: ${noteText(x.body).join(" / ")}`);
        assert.deepStrictEqual(noteText(x.body), [want], label);
        assert.strictEqual(x.body.querySelector(".dm-notes"), null, label + ": no list");
    }
    // A limit with releases kept from before: those, with their time.
    const kept = await openUpdate("de", PLAN_OLLAMA, { "/var/lib/diun-radar/releases.json": { repos: { "ollama/ollama":
        { ...RELEASES.repos["ollama/ollama"], status: "limited", error: "HTTP 403", tried: "2026-10-12T08:00:00Z" } } } });
    assert.strictEqual(kept.body.querySelectorAll(".dm-release").length, 4);
    assert.strictEqual(kept.body.querySelector(".dm-notes > .dm-dialog-note").textContent, `Aus github.com/ollama/ollama, Stand ${stamp("2026-10-12T02:18:05Z", "de-DE")}.`);
    // The same version (a new build of it): nothing to read, no note.
    const same = await openUpdate("de", { ...PLAN_OLLAMA, target_version: "0.30.11", jump: "same" });
    assert.strictEqual(same.body.querySelector(".dm-notes-slot").innerHTML, "", "same version: the slot stays empty");
    assert(!same.reads.includes("/var/lib/diun-radar/releases.json"), "and releases.json is not read");
    const same2 = await openUpdate("de", { ...PLAN_OLLAMA, target_version: "0.30.11.0", jump: null });
    assert.strictEqual(same2.body.querySelector(".dm-notes-slot").innerHTML, "", "0.30.11.0 is 0.30.11");

    // 8. The preview of a version change (stage 2): per choice, the old slot dropped.
    const r = boot("de", { ...BASE, "diun-update.py retag": (cmd) => ({ out: JSON.stringify(dryRun(cmd[3])) }) });
    await settle();
    rowOf(r.d, "metrics").querySelector("td.az-cell-actions button[data-retag]").click();
    await settle();
    const choose = (tag) => { const sel = r.$("dm-retag-select"); sel.value = tag; sel.dispatchEvent(new r.w.Event("change")); };
    choose("v3.2.1");
    await settle();
    const pv = r.$("dm-retag-preview");
    console.log("retag v3.2.1:", pv.querySelector(".dm-notes > summary").textContent.trim(), "|",
                [...pv.querySelectorAll(".dm-notes > .dm-dialog-note")].map((p) => p.textContent).join(" / "));
    assert.strictEqual(pv.querySelector(".dm-notes-title").textContent, "Release Notes 2.1.4 → 3.2.1: 3 Releases");
    assert.deepStrictEqual([...pv.querySelectorAll(".dm-release-tag")].map((x) => x.textContent), ["v3.2.1", "v3.0.0", "v2.1.6"]);
    assert.strictEqual(pv.querySelector(".dm-notes > summary .az-label--warning").textContent, "erwähnt migration");
    assert.strictEqual(pv.querySelectorAll(".dm-notes > .dm-dialog-note")[1].textContent,
        "Ältere Releases in diesem Bereich sind nicht geladen; github.com/plausible/analytics listet alle.");
    choose("v2.1.5");
    await settle();
    console.log("retag v2.1.5:", noteText(pv).join(" / "));
    assert.deepStrictEqual(noteText(pv), ["Kein Release auf github.com/plausible/analytics zwischen 2.1.4 und 2.1.5.",
        "Ältere Releases in diesem Bereich sind nicht geladen; github.com/plausible/analytics listet alle."]);
    assert.strictEqual(pv.querySelectorAll(".dm-notes-slot").length, 1, "one plan, one slot");

    console.log("release notes: all checks passed");
})().catch((e) => { console.error(e); process.exit(1); });

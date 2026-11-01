// SPDX-License-Identifier: Apache-2.0
// "Change version" (stage 2): the row's button and the outlined chips, the
// selection field from the radar's choices (database majors and Nextcloud
// jumps listed but locked), the preview of `retag --dry-run` (changed lines,
// plan, tick boxes, refusals), the change with the preview's fingerprint,
// failure and rollback, and the history entry, against the real diun.js in
// jsdom with a scripted Cockpit stub.
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
const files = {
    "/var/lib/diun/events.jsonl": fs.readFileSync(path.join(FIX, "events.jsonl"), "utf8"),
    "/var/lib/diun-radar/versions.json": JSON.parse(fs.readFileSync(path.join(FIX, "versions2.json"), "utf8")),
    "/var/lib/diun/dismissed.json": null,
};
const DEPS_OK = JSON.stringify({ root: true, docker: { state: "ok", compose: true },
    diun: { state: "running", events: true }, skopeo: { state: "ok" }, radar: { state: "active" } });
const REPO = "ghcr.io/plausible/community-edition";
const PLAN = { container: "metrics", image: REPO + ":v2.1.4", updatable: true, reason: null,
    project: "metrics", workdir: "/opt/metrics", services: ["metrics"], containers: ["metrics"],
    dbs: [{ name: "metrics_db", kind: "postgres", running: true, size: 52428800 },
          { name: "metrics_events_db", kind: "unsupported", running: true, size: null }],
    free_bytes: 40 * 2 ** 30, config_changed: false, pulled: false, installed_version: "2.1.4", tag_digits: 3,
    jump_unknown: false, depends_on: [], dependents: [], jobs: [], job_order: [], shared_networks: [] };
const FP = { "v2.1.5": "a".repeat(64), "v3.2.1": "b".repeat(64), v2: "c".repeat(64), latest: "d".repeat(64) };
const TARGET = { "v2.1.5": ["2.1.5", "patch", "newer"], "v3.2.1": ["3.2.1", "major", "newer"],
                 v2: ["2.1.5", "patch", "line"], latest: [null, null, "floating"], "8-alpine": ["8.10.2", "major", "line"] };
function dryRun(tag, rt) {
    const [target, jump, choice] = TARGET[tag] || [null, null, null];
    return { ...PLAN, target_version: target, jump, jump_unknown: target === null,
        retag: { tag, from_tag: "v2.1.4", image: REPO + ":" + tag, choice, level: choice === "newer" ? jump : null,
                 newest: choice === "line" ? "v2.1.5" : null, possible: true, reason: null, detail: null, variable: null,
                 changes: [{ file: "/opt/metrics/compose.yml", line: 12, old: "    image: " + REPO + ":v2.1.4",
                             new: "    image: " + REPO + ":" + tag, service: "metrics", sha256: "e".repeat(64) }],
                 git: null, fingerprint: FP[tag], ...rt } };
}
const RETAG_OP = { id: "20261015-101500-metrics", ts: "2026-10-15T08:15:00Z", kind: "retag", container: "metrics",
    project: "metrics", services: ["metrics"], containers: ["metrics"], image: REPO + ":v2.1.4",
    to_image: REPO + ":v2", from_tag: "v2.1.4", to_tag: "v2",
    changes: [{ file: "/opt/metrics/compose.yml", line: 12, old: "    image: " + REPO + ":v2.1.4", new: "    image: " + REPO + ":v2" },
              { file: "/opt/metrics/.env", line: 3, old: "X=1", new: "X=2" }],
    backup: "/var/backups/image-updates/compose/metrics/20261015-101500",
    from_version: "2.1.4", to_version: "2.1.5", rollback: REPO + ":rollback-20261015-101500", dumps: [],
    status: "ok", can_rollback: true, log: true, rollback_log: false };
const BACKUPS = { dir: "/var/backups/image-updates", free_bytes: 40 * 2 ** 30, dump_bytes: 0, dumps: [], images: [],
                  ops: [RETAG_OP], cleanup: null };
// Answers of the stubbed helpers that have not arrived yet, for every page of this file.
const pending = new Set();
const track = (p) => {
    const done = p.then(() => {}, () => {});
    pending.add(done);
    done.then(() => pending.delete(done));
};
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
const kvRows = (el) => [...el.querySelectorAll(".dm-kv li")].map((li) => [...li.children].map((x) => x.textContent).join(": "));

function boot(language, script) {
    const dom = new JSDOM(html, { runScripts: "outside-only" });
    const w = dom.window;
    w.document.querySelectorAll("script").forEach((s) => s.remove());   // the page's own tags never run
    w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
    w.HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
    w.Date.now = () => Date.parse("2026-10-15T10:00:00Z");
    const calls = [];
    w.cockpit = {
        language,
        file(p) { return { read: () => Promise.resolve(files[p] ?? null), watch: (cb) => cb(files[p], "t"), replace: () => Promise.resolve() }; },
        spawn(cmd, opts) {
            calls.push({ cmd: JSON.parse(JSON.stringify(cmd)), opts: JSON.parse(JSON.stringify(opts || {})) });
            const tool = cmd[0].split("/").pop();
            const s = script[(tool + " " + (cmd[1] || "")).trim()] || script[tool];
            if (!s) return Promise.reject(new Error("unscripted " + cmd.join(" ")));
            const r = typeof s === "function" ? s(cmd) : s;
            let cb = null;
            // r.gate: a promise the answer waits for, so a test can check the
            // state in between at a fixed point; settle() waits for an answer
            // only once its gate is open.
            const p = new Promise((resolve, reject) => Promise.resolve(r.gate).then(() => track(new Promise((sent) => setTimeout(() => {
                (r.chunks || []).forEach((c) => cb && cb(c));
                r.fail ? reject(r.fail) : resolve(r.out ?? "");
                sent();
            }, r.delay || 10)))));
            p.stream = (f) => { cb = f; return p; };
            return p;
        },
        transport: { wait() {} },
    };
    w.eval(icons);
    w.eval(js);
    const d = w.document;
    const $ = (id) => d.getElementById(id);
    return { w, d, $, calls, last: (verb) => [...calls].reverse().find((c) => c.cmd[1] === verb) };
}
const BASE = {
    "diun-inventory.py": { out: inventory },
    "diun-deps.sh check": { out: DEPS_OK },
    "diun-update.py backups": { out: JSON.stringify(BACKUPS) },
};
const DRY = (rt) => (cmd) => cmd[4] === "--dry-run" ? { out: JSON.stringify(dryRun(cmd[3], rt)) } : null;
const rowOf = (d, name) => [...d.querySelectorAll("#dm-rows tr")].find((tr) => tr.querySelector(".dm-name").textContent === name);
const optionsOf = (d) => [...d.querySelectorAll("#dm-retag-select optgroup")].map((g) =>
    g.label + ": " + [...g.querySelectorAll("option")].map((o) => o.textContent + (o.disabled ? " [x]" : "")).join(", "));
const choose = (x, tag) => {
    const sel = x.$("dm-retag-select");
    sel.value = tag;
    sel.dispatchEvent(new x.w.Event("change"));
};
const tick = (x, id, on) => { const b = x.$(id); b.checked = on; b.dispatchEvent(new x.w.Event("change")); };

(async () => {
    // 1. Rows: the button where the field offers something, chips as buttons
    const a = boot("de", BASE);
    await settle();
    const btn = (n) => rowOf(a.d, n).querySelector("td.az-cell-actions button[data-retag]");
    const withButton = [...a.d.querySelectorAll("#dm-rows tr")].filter((tr) => tr.querySelector("td.az-cell-actions button[data-retag]"))
        .map((tr) => tr.querySelector(".dm-name").textContent);
    console.log("rows with Change version:", withButton.join(", "));
    for (const n of ["metrics", "bugs-postgres", "inference", "cloud-app-1"]) assert(btn(n), n);
    for (const n of ["survey-app", "budget-ai", "remote-signal"]) assert(!btn(n), n + ": no choices or not compose");
    const b0 = btn("metrics");
    assert.deepStrictEqual([b0.className, b0.getAttribute("aria-label"), b0.title, b0.textContent,
                            b0.querySelector("svg").getAttribute("data-icon")],
                           ["az-btn az-btn--plain az-btn--sm", "Version ändern", "Version ändern", "", "exchange-alt"]);
    const lv = [...rowOf(a.d, "metrics").querySelectorAll(".dm-level")].map((x) => x.closest("button") || x);
    console.log("metrics levels:", lv.map((c) => `${c.tagName}:${c.dataset.tag}`).join(" "));
    assert.deepStrictEqual(lv.map((c) => [c.tagName, c.className, c.dataset.tag]),
                           [["BUTTON", "dm-level-btn", "v2.1.5"], ["BUTTON", "dm-level-btn", "v3.2.1"]]);
    assert(lv[0].title.endsWith("Klicken Sie, um den Tag auf v2.1.5 zu ändern."));
    const pgLevels = [...rowOf(a.d, "bugs-postgres").querySelectorAll(".dm-level")];
    assert(pgLevels.length && pgLevels.every((c) => !c.closest("button")), "levels the Update button brings and database majors stay labels");

    // 2. The field: configured tag first, groups, locked entries
    b0.click();
    await settle();
    console.log("\ndialog:", a.$("dm-dialog-title").textContent);
    assert.strictEqual(a.$("dm-dialog-title").textContent, "Version von metrics ändern?");
    const sel = a.$("dm-retag-select");
    assert.strictEqual(sel.options[0].textContent, "v2.1.4 (jetzt konfiguriert)");
    assert.strictEqual(sel.value, "");
    assert(a.$("dm-dialog-confirm").disabled, "nothing chosen yet");
    assert.strictEqual(a.d.querySelector('label[for="dm-retag-select"]').textContent, "Tag in der Compose-Datei");
    optionsOf(a.d).forEach((g) => console.log("  ", g));
    assert.deepStrictEqual(optionsOf(a.d), [
        "Versionslinien: Updates innerhalb der Linie: v3 (derzeit v3.2.1), v3.2 (derzeit v3.2.1), v2 (derzeit v2.1.5), v2.1 (derzeit v2.1.5)",
        "Patch-Versionen: v2.1.5", "Major-Versionen: v3.2.1", "Folgt jeder neuen Version: latest"]);
    a.$("dm-dialog-cancel").click();
    rowOf(a.d, "bugs-postgres").querySelector("button[data-retag]").click();
    await settle();
    optionsOf(a.d).forEach((g) => console.log("  ", g));
    assert.deepStrictEqual(optionsOf(a.d), [
        "Laufende Version festsetzen: 16.14-alpine (läuft jetzt)",
        "Versionslinien: Updates innerhalb der Linie: 18-alpine – Datenbank-Major per Dump und Restore, " +
            "17-alpine – Datenbank-Major per Dump und Restore",
        "Minor-Versionen: 16.15-alpine",
        "Major-Versionen: 18.6-alpine – Datenbank-Major per Dump und Restore, 17.11-alpine – Datenbank-Major per Dump und Restore",
        "Folgt jeder neuen Version: alpine – Datenbank-Major, braucht eine Migration [x]"]);
    a.$("dm-dialog-cancel").click();
    rowOf(a.d, "cloud-app-1").querySelector("button[data-retag]").click();
    await settle();
    optionsOf(a.d).forEach((g) => console.log("  ", g));
    assert.deepStrictEqual(optionsOf(a.d), [
        "Laufende Version festsetzen: 34.0.3-apache (läuft jetzt)",
        "Versionslinien: Updates innerhalb der Linie: 35-apache (derzeit 35.0.1-apache), 35.0-apache (derzeit 35.0.1-apache), " +
            "34-apache (derzeit 34.0.4-apache), 34.0-apache (derzeit 34.0.4-apache)",
        "Patch-Versionen: 34.0.4-apache", "Major-Versionen: 35.0.1-apache",
        "Folgt jeder neuen Version: stable-apache – Nextcloud: nur eine Hauptversion pro Schritt [x], " +
            "production-apache – Nextcloud: nur eine Hauptversion pro Schritt [x]"]);
    a.$("dm-dialog-cancel").click();

    // 3. Chip -> field preset, preview, change with the fingerprint
    const runOk = { chunks: ["Changing …\n", "@@RESULT " + JSON.stringify({ op: "20261015-101500-metrics", status: "ok",
        rollback: REPO + ":rollback-20261015-101500", dumps: [], from_version: "2.1.4", to_version: "2.1.5",
        image: REPO + ":v2.1.5", backup: "/var/backups/image-updates/compose/metrics/20261015-101500" }) + "\n"] };
    // The dry-run and the run answer only when the test opens their gate: the
    // states while they run are checked at fixed points.
    let openDry, openRun;
    const dryGate = new Promise((r) => { openDry = r; });
    const runGate = new Promise((r) => { openRun = r; });
    const c = boot("de", { ...BASE, "diun-update.py retag": (cmd) => {
        const dry = DRY()(cmd);
        return dry ? { ...dry, gate: dryGate } : { ...runOk, gate: runGate };
    } });
    await settle();
    rowOf(c.d, "metrics").querySelector('button.dm-level-btn[data-tag="v2.1.5"]').click();
    await settle();
    assert.strictEqual(c.$("dm-retag-select").value, "v2.1.5", "the level's tag is chosen");
    assert.strictEqual(c.$("dm-retag-preview").textContent, "Die Änderung wird geprüft …");
    assert(c.$("dm-dialog-confirm").disabled, "no confirm while planning");
    openDry();
    await settle();
    const dry = c.last("retag");
    console.log("\ndry-run:", JSON.stringify(dry.cmd.slice(1)), dry.opts.superuser);
    assert.deepStrictEqual(dry.cmd.slice(1), ["retag", "metrics", "v2.1.5", "--dry-run"]);
    assert.strictEqual(dry.opts.superuser, "try");
    const pv = c.$("dm-retag-preview");
    const diff = [...pv.querySelectorAll(".dm-diff")].map((x) =>
        [x.querySelector(".dm-diff-file").textContent, x.querySelector(".dm-diff-old").textContent, x.querySelector(".dm-diff-new").textContent]);
    console.log("diff   :", JSON.stringify(diff));
    assert.deepStrictEqual(diff, [["/opt/metrics/compose.yml:12 (metrics)", "-     image: " + REPO + ":v2.1.4",
                                   "+     image: " + REPO + ":v2.1.5"]]);
    console.log("plan   :", kvRows(pv).join(" | "));
    assert(kvRows(pv).includes("Bringt: 2.1.5 Patch") && kvRows(pv).includes("Installierte Version: 2.1.4"));
    assert(c.$("dm-dump-check").checked, "dump on by default");
    assert(pv.firstElementChild.classList.contains("dm-diff-group"), "the changed lines first");
    const cf = c.$("dm-dialog-confirm");
    assert(!cf.disabled && cf.textContent.trim() === "Version ändern");
    cf.click();
    await settle();
    assert(c.$("dm-retag-select").disabled && c.$("dm-dump-check").disabled, "the choice is fixed while it runs");
    openRun();
    await settle();
    const run = c.last("retag");
    console.log("run    :", JSON.stringify(run.cmd.slice(1)), run.opts.superuser);
    assert.deepStrictEqual(run.cmd.slice(1), ["retag", "metrics", "v2.1.5", "--expect", FP["v2.1.5"]]);
    assert.strictEqual(run.opts.superuser, "require");
    console.log("result :", c.$("dm-dialog-result").textContent);
    assert.strictEqual(c.$("dm-dialog-result").textContent,
        "Version geändert: 2.1.4 → 2.1.5, die Compose-Datei nennt jetzt v2.1.5. Rollback-Kopie: " + REPO + ":rollback-20261015-101500");
    assert(!c.$("dm-dialog-log").textContent.includes("@@RESULT"));

    // 4. A major needs its tick box; without dump -> --no-dump; back to the configured tag
    const m = boot("de", { ...BASE, "diun-update.py retag": (cmd) => DRY()(cmd) || runOk });
    await settle();
    rowOf(m.d, "metrics").querySelector("td.az-cell-actions button[data-retag]").click();
    await settle();
    choose(m, "v3.2.1");
    await settle();
    const mc = m.$("dm-dialog-confirm");
    const mpv = m.$("dm-retag-preview");
    console.log("\nmajor  :", mpv.querySelector(".az-alert--warning").textContent);
    assert(kvRows(mpv).includes("Bringt: 3.2.1 Major"));
    assert(mc.disabled, "waits for the tick box");
    tick(m, "dm-major-check", true);
    assert(!mc.disabled);
    choose(m, "");
    assert.strictEqual(mpv.innerHTML, "", "the configured tag: nothing to show");
    assert(mc.disabled, "and nothing to confirm");
    choose(m, "v3.2.1");
    await settle();
    assert(mc.disabled, "a new preview asks again");
    tick(m, "dm-major-check", true);
    m.$("dm-dump-check").checked = false;
    mc.click();
    await settle();
    assert.deepStrictEqual(m.last("retag").cmd.slice(1), ["retag", "metrics", "v3.2.1", "--expect", FP["v3.2.1"], "--no-dump"]);

    // 5. A tag that follows every release: its own warning, not "installed version unknown"
    const f = boot("de", { ...BASE, "diun-update.py retag": DRY() });
    await settle();
    rowOf(f.d, "metrics").querySelector("td.az-cell-actions button[data-retag]").click();
    await settle();
    choose(f, "latest");
    await settle();
    const fw = f.$("dm-retag-preview").querySelector(".dm-gate-group");
    console.log("\nlatest :", fw.textContent.replace(/\s+/g, " ").trim());
    assert.strictEqual(fw.querySelector(".az-alert--warning .az-alert__title").textContent, "Der Tag folgt jeder neuen Version");
    assert(fw.querySelector(".az-alert--warning .az-alert__body").textContent.startsWith("latest folgt jeder neuen Version, auch neuen Hauptversionen"));
    assert.strictEqual(fw.querySelector(".dm-check").textContent.trim(), "Mir ist bewusst, dass latest jeder neuen Version folgt.");
    assert(!f.$("dm-retag-preview").textContent.includes("unbekannt"));
    assert(f.$("dm-dialog-confirm").disabled);
    tick(f, "dm-jump-check", true);
    assert(!f.$("dm-dialog-confirm").disabled);

    // 6. A later answer for an earlier choice is dropped
    const s = boot("de", { ...BASE, "diun-update.py retag": (cmd) => ({ ...DRY()(cmd), delay: cmd[3] === "v3.2.1" ? 80 : 10 }) });
    await settle();
    rowOf(s.d, "metrics").querySelector("td.az-cell-actions button[data-retag]").click();
    await settle();
    choose(s, "v3.2.1");
    choose(s, "v2");
    await settle();
    const sdiff = s.$("dm-retag-preview").querySelector(".dm-diff-new").textContent;
    console.log("\nlast choice wins:", sdiff);
    assert.strictEqual(sdiff, "+     image: " + REPO + ":v2");
    assert(!s.$("dm-major-check"), "the slower major plan did not replace it");
    assert(kvRows(s.$("dm-retag-preview")).includes("Bringt: 2.1.5 Patch"));

    // 7. Refusals (a warning: what is not possible, then why) and hints
    const refused = (x) => { const a = x.$("dm-retag-preview").querySelector(".az-alert");
        return [a.className, a.querySelector(".az-alert__title").textContent, a.querySelector(".az-alert__body").textContent]; };
    const NOT = ["az-alert az-alert--warning", "Über das Addon nicht möglich"];
    const refusals = [
        [{ possible: false, reason: "variable-shared", detail: "PLAUSIBLE_VERSION", changes: [], fingerprint: null },
         "Die Variable PLAUSIBLE_VERSION wird auch anderswo verwendet; sie zu ändern, würde mehr als dieses Image ändern."],
        [{ possible: false, reason: "anchors", detail: "/opt/metrics/compose.yml:12", changes: [], fingerprint: null },
         "Die Image-Zeile steht unter einem YAML-Anker oder außerhalb eines Service " +
         "(/opt/metrics/compose.yml:12). Bitte ändern Sie den Tag von Hand."],
        [{ possible: false, reason: "something-new", detail: "x", changes: [], fingerprint: null },
         "something-new (x)"],
    ];
    for (const [rt, want] of refusals) {
        const r = boot("de", { ...BASE, "diun-update.py retag": DRY(rt) });
        await settle();
        rowOf(r.d, "metrics").querySelector("td.az-cell-actions button[data-retag]").click();
        await settle();
        choose(r, "v2.1.5");
        await settle();
        console.log("refused:", refused(r).slice(1).join(" / "));
        assert.deepStrictEqual(refused(r), [...NOT, want]);
        assert(r.$("dm-dialog-confirm").disabled);
    }
    const nf = boot("de", { ...BASE, "diun-update.py retag": (cmd) => ({ out: JSON.stringify({ ...dryRun(cmd[3]), updatable: false,
        reason: "compose-file-missing", missing: ["/opt/metrics/compose.yml"],
        retag: { ...dryRun(cmd[3]).retag, possible: false, reason: "compose-file-missing", changes: [], fingerprint: null } }) }) });
    await settle();
    rowOf(nf.d, "metrics").querySelector("td.az-cell-actions button[data-retag]").click();
    await settle();
    choose(nf, "v2");
    await settle();
    assert.deepStrictEqual(refused(nf), [...NOT, "Die Compose-Datei existiert nicht mehr (/opt/metrics/compose.yml)."]);
    const hv = boot("de", { ...BASE, "diun-update.py retag": DRY({ variable: "PLAUSIBLE_VERSION", git: "/opt",
        changes: [{ file: "/opt/metrics/.env", line: 3, old: "PLAUSIBLE_VERSION=v2.1.4", new: "PLAUSIBLE_VERSION=v2",
                    service: null, sha256: "e".repeat(64) }] }) });
    await settle();
    rowOf(hv.d, "metrics").querySelector("td.az-cell-actions button[data-retag]").click();
    await settle();
    choose(hv, "v2");
    await settle();
    const hints = [...hv.$("dm-retag-preview").querySelectorAll(".az-alert:not(.az-alert--warning)")].map((x) =>
        [x.querySelector(".az-alert__title").textContent, x.querySelector(".az-alert__body").textContent]);
    console.log("hints  :", hints.slice(0, 2).map((x) => x.join(": ")).join(" | "));
    assert.strictEqual(hv.d.querySelector(".dm-diff-file").textContent, "/opt/metrics/.env:3");
    assert.deepStrictEqual(hints[0], ["Tag aus einer Variable",
                                      "Der Tag kommt aus der Variable PLAUSIBLE_VERSION; sie wird dort geändert, wo sie gesetzt ist."]);
    assert.deepStrictEqual(hints[1], ["Git-Repository", "Das Projekt liegt im Git-Repository /opt. Das Addon committet nicht; " +
                                      "übernehmen Sie die Änderung dort, wenn sie in die Historie soll."]);
    assert.deepStrictEqual([...hv.$("dm-retag-preview").querySelectorAll(".az-alert code")].slice(0, 2).map((x) => x.textContent),
                           ["PLAUSIBLE_VERSION", "/opt"], "variable and path as code");
    const na = boot("en", { ...BASE, "diun-update.py retag": { fail: { problem: "access-denied" } } });
    await settle();
    rowOf(na.d, "metrics").querySelector("td.az-cell-actions button[data-retag]").click();
    await settle();
    choose(na, "v2");
    await settle();
    const naAlert = na.$("dm-retag-preview").querySelector(".az-alert--danger");
    assert.deepStrictEqual([naAlert.getAttribute("role"), naAlert.querySelector(".az-alert__title").textContent],
                           ["alert", "The change could not be checked"], "an error, not a warning");
    assert(naAlert.querySelector(".az-alert__body").textContent.startsWith("Denied"));

    // 8. Failure -> cause, rollback offered; a rollback that finds an edited file stops
    const RS = { container: "metrics", state: "restarting", exit_code: 1, oom: false, source: "log", line: "Error: boom" };
    const fl = boot("de", { ...BASE, "diun-update.py retag": (cmd) => DRY()(cmd) || { chunks: ["…\n", "@@RESULT " + JSON.stringify(
            { op: "20261015-101500-metrics", status: "failed", reason: RS, image: REPO + ":v2" }) + "\n"], fail: { exit_status: 4 } },
        "diun-update.py rollback": { chunks: ["/opt/metrics/compose.yml changed after the tag change – nothing changed.\n"],
                                     fail: { exit_status: 5 } } });
    await settle();
    rowOf(fl.d, "metrics").querySelector("td.az-cell-actions button[data-retag]").click();
    await settle();
    choose(fl, "v2");
    await settle();
    fl.$("dm-dialog-confirm").click();
    await settle();
    console.log("\nfailed :", fl.$("dm-dialog-result").textContent);
    assert.strictEqual(fl.$("dm-dialog-result").textContent,
        "Tag-Wechsel fehlgeschlagen – die Container laufen nicht stabil. Zurückrollen stellt die Dateien und das vorherige Image wieder her.");
    assert.strictEqual(fl.d.querySelector("#dm-dialog-reason .dm-reason-line").textContent, "Error: boom");
    assert(!fl.$("dm-dialog-extra").hidden);
    fl.$("dm-dialog-extra").click();
    await settle();
    assert.deepStrictEqual(fl.last("rollback").cmd.slice(1), ["rollback", "20261015-101500-metrics"]);
    console.log("rollback:", fl.$("dm-dialog-result").textContent);
    assert.strictEqual(fl.$("dm-dialog-result").textContent, "Rollback abgebrochen, bevor etwas geändert wurde – siehe Ausgabe.");
    const ab = boot("de", { ...BASE, "diun-update.py retag": (cmd) => DRY()(cmd) || { chunks: ["The files changed since the preview\n"],
                                                                                      fail: { exit_status: 5 } } });
    await settle();
    rowOf(ab.d, "metrics").querySelector("td.az-cell-actions button[data-retag]").click();
    await settle();
    choose(ab, "v2");
    await settle();
    ab.$("dm-dialog-confirm").click();
    await settle();
    assert.strictEqual(ab.$("dm-dialog-result").textContent, "Abgebrochen, bevor etwas geändert wurde – siehe Ausgabe.");

    // 9. History: the tag change under the image; its rollback names the files
    const h = boot("de", { ...BASE, "diun-update.py rollback": { chunks: ['@@RESULT {"status":"ok"}\n'] } });
    await settle();
    h.$("dm-tab-btn-backups").click();
    await settle();
    const opRow = h.d.querySelector("#dm-ops tr");
    console.log("\nhistory:", [...opRow.querySelectorAll("td")].map((td) => td.textContent.trim()).filter(Boolean).join(" | "));
    assert.strictEqual(opRow.querySelector(".dm-row-note").textContent, "Tag geändert: v2.1.4 → v2");
    opRow.querySelector("button[data-op]").click();
    console.log("rollback dialog:", h.$("dm-dialog-desc").textContent);
    assert(h.$("dm-dialog-desc").textContent.startsWith(
        "Stellt /opt/metrics/compose.yml, /opt/metrics/.env wieder her, wie sie vor dem Tag-Wechsel waren, setzt " + REPO + ":v2.1.4 wieder auf"));
    h.$("dm-dialog-confirm").click();
    await settle();
    assert.strictEqual(h.$("dm-dialog-result").textContent, "Rollback erfolgreich.");

    // 10. English: no German in the row, the field, the preview and the history
    const en = boot("en", { ...BASE, "diun-update.py retag": DRY({ variable: "V", git: "/opt" }) });
    await settle();
    rowOf(en.d, "bugs-postgres").querySelector("td.az-cell-actions button[data-retag]").click();
    await settle();
    const enOpts = optionsOf(en.d).join(" / ");
    en.$("dm-dialog-cancel").click();
    rowOf(en.d, "metrics").querySelector("td.az-cell-actions button[data-retag]").click();
    await settle();
    choose(en, "latest");
    await settle();
    en.$("dm-tab-btn-backups").click();
    await settle();
    const all = en.d.documentElement.outerHTML + enOpts;
    assert(enOpts.includes("18-alpine – database major by dump and restore") &&
           enOpts.includes("alpine – database major, needs a migration"), enOpts);
    assert(all.includes("Tag changed: v2.1.4 → v2") && all.includes("latest follows every new release"));
    const german = ["Version ändern", "Versionslinien", "festsetzen", "läuft jetzt", "derzeit", "Datenbank", "Hauptversion",
                    "Folgt jeder", "Prüfe", "Änderung", "Variable", "geändert", "konfiguriert", "Klicken", "Bringt", "bewusst"];
    const found = german.filter((g) => all.includes(g));
    console.log("\nEN German leftovers:", found.length ? found : "none");
    assert.deepStrictEqual(found, []);

    // 11. A key-value store (since 0.27.0): a newer major can be chosen as a tag
    // change; the preview names the data saved first, the history its copy, and the
    // rollback dialog says that the data goes back.
    const versions = files["/var/lib/diun-radar/versions.json"];
    const withChoices = JSON.parse(JSON.stringify(versions));
    withChoices.images.find((i) => i.image === "redis").versions["7.4.11"].choices = {
        pin: "7.4.11-alpine", lines: [{ tag: "8-alpine", newest: "8.10.2-alpine" }, { tag: "7-alpine", newest: "7.4.11-alpine" }],
        newer: [{ tag: "8.10.2-alpine", level: "major" }], floating: ["alpine"] };
    files["/var/lib/diun-radar/versions.json"] = withChoices;
    const SRC = "/var/lib/docker/volumes/" + "a".repeat(64) + "/_data";
    const STORE = { engine: "redis", name: "Redis", bytes: 3 * 2 ** 20,
        items: [{ container: "cloud-redis-1", type: "volume", source: SRC, destination: "/data", bytes: 3 * 2 ** 20 }] };
    const STORE_OP = { ...RETAG_OP, id: "20261101-211610-cloud-redis-1", container: "cloud-redis-1", image: "redis:7-alpine",
        from_tag: "7-alpine", to_tag: "8-alpine", store: "Redis", data_copy: { bytes: 3 * 2 ** 20, there: true },
        changes: [{ file: "/opt/cloud/compose.yml", line: 30, old: "    image: redis:7-alpine", new: "    image: redis:8-alpine" }],
        rollback: "redis:rollback-20261101-211610", services: ["redis"] };
    const GONE_OP = { ...STORE_OP, id: "20261101-201610-cloud-redis-1", data_copy: { bytes: 3 * 2 ** 20, there: false },
                      can_rollback: false, data_gone: true };
    const storeScript = (store) => ({ ...BASE, "diun-update.py retag": DRY({ store }),
        "diun-update.py backups": { out: JSON.stringify({ ...BACKUPS, ops: [STORE_OP, GONE_OP] }) },
        "diun-update.py rollback": { chunks: ["@@RESULT " + JSON.stringify({ op: STORE_OP.id, status: "ok" }) + "\n"] } });
    const rd = boot("de", storeScript(STORE));
    await settle();
    rowOf(rd.d, "cloud-redis-1").querySelector("td.az-cell-actions button[data-retag]").click();
    await settle();
    optionsOf(rd.d).forEach((g) => console.log("  ", g));
    assert.deepStrictEqual(optionsOf(rd.d), [
        "Laufende Version festsetzen: 7.4.11-alpine (läuft jetzt)",
        "Versionslinien: Updates innerhalb der Linie: 8-alpine – neue Hauptversion, die Daten werden vorher gesichert",
        "Major-Versionen: 8.10.2-alpine – neue Hauptversion, die Daten werden vorher gesichert",
        "Folgt jeder neuen Version: alpine – Datenbank-Major, braucht eine Migration [x]"]);
    choose(rd, "8-alpine");
    await settle();
    const rpv = rd.$("dm-retag-preview");
    const warn = rpv.querySelector(".az-alert--warning");
    console.log("store preview:", warn && warn.textContent);
    assert(warn && warn.textContent.startsWith("Neue Hauptversion von Redis") &&
           warn.textContent.includes("stoppt das Add-on Redis und sichert sein Datenverzeichnis"), warn && warn.textContent);
    assert(kvRows(rpv).some((r) => r.startsWith("cloud-redis-1: " + SRC)), kvRows(rpv).join(" | "));
    assert.strictEqual(rd.last("retag").cmd[1], "retag", "a store's major is a tag change, not dbmajor");
    // As every new major: one tick box, then the change itself with the preview's fingerprint.
    assert(rd.$("dm-dialog-confirm").disabled && !rpv.querySelector("#dm-mj-backup"), "the major's tick box, not a database major's");
    tick(rd, "dm-major-check", true);
    assert(!rd.$("dm-dialog-confirm").disabled);
    rd.$("dm-dialog-cancel").click();
    rd.$("dm-tab-btn-backups").click();
    await settle();
    const storeRow = [...rd.d.querySelectorAll("#dm-ops tr")].find((tr) => tr.textContent.includes(STORE_OP.to_tag) &&
        tr.querySelector("button[data-op]"));
    const gonePos = [...rd.d.querySelectorAll("#dm-ops tr")].find((tr) => tr.querySelector(".dm-rb-gone"));
    assert([...storeRow.querySelectorAll(".dm-row-note")].some((n) => n.textContent.startsWith("Daten von Redis vorher gesichert: 3")),
           storeRow.textContent);
    assert.strictEqual(gonePos.querySelector(".dm-rb-gone").textContent, "Datenkopie fehlt");
    assert(!gonePos.querySelector("button[data-op]"), "without its copy no rollback button");
    storeRow.querySelector("button[data-op]").click();
    console.log("store rollback dialog:", rd.$("dm-dialog-desc").textContent);
    assert(rd.$("dm-dialog-desc").textContent.startsWith("Stoppt Redis, legt seine Daten von vor dem Tag-Wechsel und /opt/cloud/compose.yml zurück"));
    rd.$("dm-dialog-cancel").click();
    // Nothing mounted: nothing saved, said so; in English without German.
    const rde = boot("en", storeScript({ ...STORE, items: [{ ...STORE.items[0], type: null, source: null, destination: null, bytes: null }] }));
    await settle();
    rowOf(rde.d, "cloud-redis-1").querySelector("td.az-cell-actions button[data-retag]").click();
    await settle();
    const rdeOpts = optionsOf(rde.d).join(" / ");
    choose(rde, "8-alpine");
    await settle();
    const rdeWarn = rde.$("dm-retag-preview").querySelector(".az-alert--warning").textContent;
    assert(rdeOpts.includes("8-alpine – new major, the data is saved first"), rdeOpts);
    assert(rdeWarn.startsWith("New major of Redis") && rdeWarn.includes("No data directory is mounted"), rdeWarn);
    assert.deepStrictEqual(kvRows(rde.$("dm-retag-preview")).filter((r) => r.startsWith("cloud-redis-1")), []);
    const enStore = rdeOpts + rdeWarn;
    assert.deepStrictEqual(german.filter((g) => enStore.includes(g)), []);
    files["/var/lib/diun-radar/versions.json"] = versions;

    console.log("\nALL CHECKS PASSED");
})().catch((e) => { console.error("FEHLGESCHLAGEN:", e.stack || e.message); process.exit(1); });

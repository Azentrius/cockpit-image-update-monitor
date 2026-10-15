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
const js = fs.readFileSync(path.join(PLUGIN, "diun.js"), "utf8");
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
const PLAN = { container: "plausible", image: REPO + ":v2.1.4", updatable: true, reason: null,
    project: "plausible", workdir: "/opt/plausible", services: ["plausible"], containers: ["plausible"],
    dbs: [{ name: "plausible_db", kind: "postgres", running: true, size: 52428800 },
          { name: "plausible_events_db", kind: "unsupported", running: true, size: null }],
    free_bytes: 40 * 2 ** 30, config_changed: false, pulled: false, installed_version: "2.1.4", tag_digits: 3,
    jump_unknown: false, depends_on: [], dependents: [], jobs: [], job_order: [], shared_networks: [] };
const FP = { "v2.1.5": "a".repeat(64), "v3.2.1": "b".repeat(64), v2: "c".repeat(64), latest: "d".repeat(64) };
const TARGET = { "v2.1.5": ["2.1.5", "patch", "newer"], "v3.2.1": ["3.2.1", "major", "newer"],
                 v2: ["2.1.5", "patch", "line"], latest: [null, null, "floating"] };
function dryRun(tag, rt) {
    const [target, jump, choice] = TARGET[tag] || [null, null, null];
    return { ...PLAN, target_version: target, jump, jump_unknown: target === null,
        retag: { tag, from_tag: "v2.1.4", image: REPO + ":" + tag, choice, level: choice === "newer" ? jump : null,
                 newest: choice === "line" ? "v2.1.5" : null, possible: true, reason: null, detail: null, variable: null,
                 changes: [{ file: "/opt/plausible/compose.yml", line: 12, old: "    image: " + REPO + ":v2.1.4",
                             new: "    image: " + REPO + ":" + tag, service: "plausible", sha256: "e".repeat(64) }],
                 git: null, fingerprint: FP[tag], ...rt } };
}
const RETAG_OP = { id: "20261015-101500-plausible", ts: "2026-10-15T08:15:00Z", kind: "retag", container: "plausible",
    project: "plausible", services: ["plausible"], containers: ["plausible"], image: REPO + ":v2.1.4",
    to_image: REPO + ":v2", from_tag: "v2.1.4", to_tag: "v2",
    changes: [{ file: "/opt/plausible/compose.yml", line: 12, old: "    image: " + REPO + ":v2.1.4", new: "    image: " + REPO + ":v2" },
              { file: "/opt/plausible/.env", line: 3, old: "X=1", new: "X=2" }],
    backup: "/var/backups/image-updates/compose/plausible/20261015-101500",
    from_version: "2.1.4", to_version: "2.1.5", rollback: REPO + ":rollback-20261015-101500", dumps: [],
    status: "ok", can_rollback: true, log: true, rollback_log: false };
const BACKUPS = { dir: "/var/backups/image-updates", free_bytes: 40 * 2 ** 30, dump_bytes: 0, dumps: [], images: [],
                  ops: [RETAG_OP], cleanup: null };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
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
            const p = new Promise((resolve, reject) => setTimeout(() => {
                (r.chunks || []).forEach((c) => cb && cb(c));
                r.fail ? reject(r.fail) : resolve(r.out ?? "");
            }, r.delay || 10));
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
    await sleep(80);
    const btn = (n) => rowOf(a.d, n).querySelector("td.az-cell-actions button[data-retag]");
    const withButton = [...a.d.querySelectorAll("#dm-rows tr")].filter((tr) => tr.querySelector("td.az-cell-actions button[data-retag]"))
        .map((tr) => tr.querySelector(".dm-name").textContent);
    console.log("rows with Change version:", withButton.join(", "));
    for (const n of ["plausible", "glitchtip-postgres", "ollama", "nextcloud-family-app-1"]) assert(btn(n), n);
    for (const n of ["formbricks-app", "actual-ai", "hbbs"]) assert(!btn(n), n + ": no choices or not compose");
    const b0 = btn("plausible");
    assert.deepStrictEqual([b0.className, b0.getAttribute("aria-label"), b0.title, b0.textContent,
                            b0.querySelector("svg").getAttribute("data-icon")],
                           ["az-btn az-btn--plain az-btn--sm", "Version ändern", "Version ändern", "", "exchange-alt"]);
    const chips = [...rowOf(a.d, "plausible").querySelectorAll(".dm-chip")];
    console.log("plausible chips:", chips.map((c) => `${c.tagName}:${c.dataset.tag}`).join(" "));
    assert.deepStrictEqual(chips.map((c) => [c.tagName, c.dataset.tag]), [["BUTTON", "v2.1.5"], ["BUTTON", "v3.2.1"]]);
    assert(chips[0].title.endsWith("Klicken, um den Tag auf v2.1.5 zu ändern."));
    const pgChips = [...rowOf(a.d, "glitchtip-postgres").querySelectorAll(".dm-chip")];
    assert(pgChips.every((c) => c.tagName === "SPAN"), "filled chips and database majors stay labels");

    // 2. The field: configured tag first, groups, locked entries
    b0.click();
    await sleep(30);
    console.log("\ndialog:", a.$("dm-dialog-title").textContent);
    assert.strictEqual(a.$("dm-dialog-title").textContent, "Version von plausible ändern?");
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
    rowOf(a.d, "glitchtip-postgres").querySelector("button[data-retag]").click();
    await sleep(30);
    optionsOf(a.d).forEach((g) => console.log("  ", g));
    assert.deepStrictEqual(optionsOf(a.d), [
        "Laufende Version festsetzen: 16.14-alpine (läuft jetzt)",
        "Versionslinien: Updates innerhalb der Linie: 18-alpine – Datenbank-Major, braucht eine Migration [x], " +
            "17-alpine – Datenbank-Major, braucht eine Migration [x]",
        "Minor-Versionen: 16.15-alpine",
        "Major-Versionen: 18.6-alpine – Datenbank-Major, braucht eine Migration [x], 17.11-alpine – Datenbank-Major, braucht eine Migration [x]",
        "Folgt jeder neuen Version: alpine – Datenbank-Major, braucht eine Migration [x]"]);
    a.$("dm-dialog-cancel").click();
    rowOf(a.d, "nextcloud-family-app-1").querySelector("button[data-retag]").click();
    await sleep(30);
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
    const runOk = { chunks: ["Changing …\n", "@@RESULT " + JSON.stringify({ op: "20261015-101500-plausible", status: "ok",
        rollback: REPO + ":rollback-20261015-101500", dumps: [], from_version: "2.1.4", to_version: "2.1.5",
        image: REPO + ":v2.1.5", backup: "/var/backups/image-updates/compose/plausible/20261015-101500" }) + "\n"] };
    const c = boot("de", { ...BASE, "diun-update.py retag": (cmd) => DRY()(cmd) || runOk });
    await sleep(80);
    rowOf(c.d, "plausible").querySelector('button.dm-chip[data-tag="v2.1.5"]').click();
    await sleep(5);
    assert.strictEqual(c.$("dm-retag-select").value, "v2.1.5", "the chip's tag is chosen");
    assert.strictEqual(c.$("dm-retag-preview").textContent, "Prüfe die Änderung …");
    assert(c.$("dm-dialog-confirm").disabled, "no confirm while planning");
    await sleep(40);
    const dry = c.last("retag");
    console.log("\ndry-run:", JSON.stringify(dry.cmd.slice(1)), dry.opts.superuser);
    assert.deepStrictEqual(dry.cmd.slice(1), ["retag", "plausible", "v2.1.5", "--dry-run"]);
    assert.strictEqual(dry.opts.superuser, "try");
    const pv = c.$("dm-retag-preview");
    const diff = [...pv.querySelectorAll(".dm-diff")].map((x) =>
        [x.querySelector(".dm-diff-file").textContent, x.querySelector(".dm-diff-old").textContent, x.querySelector(".dm-diff-new").textContent]);
    console.log("diff   :", JSON.stringify(diff));
    assert.deepStrictEqual(diff, [["/opt/plausible/compose.yml:12 (plausible)", "-     image: " + REPO + ":v2.1.4",
                                   "+     image: " + REPO + ":v2.1.5"]]);
    console.log("plan   :", kvRows(pv).join(" | "));
    assert(kvRows(pv).includes("Bringt: 2.1.5 Patch") && kvRows(pv).includes("Installierte Version: 2.1.4"));
    assert(c.$("dm-dump-check").checked, "dump on by default");
    assert(pv.firstElementChild.classList.contains("dm-diff-group"), "the changed lines first");
    const cf = c.$("dm-dialog-confirm");
    assert(!cf.disabled && cf.textContent.trim() === "Version ändern");
    cf.click();
    await sleep(5);
    assert(c.$("dm-retag-select").disabled && c.$("dm-dump-check").disabled, "the choice is fixed while it runs");
    await sleep(60);
    const run = c.last("retag");
    console.log("run    :", JSON.stringify(run.cmd.slice(1)), run.opts.superuser);
    assert.deepStrictEqual(run.cmd.slice(1), ["retag", "plausible", "v2.1.5", "--expect", FP["v2.1.5"]]);
    assert.strictEqual(run.opts.superuser, "require");
    console.log("result :", c.$("dm-dialog-result").textContent);
    assert.strictEqual(c.$("dm-dialog-result").textContent,
        "Version geändert: 2.1.4 → 2.1.5, die Compose-Datei nennt jetzt v2.1.5. Rollback-Kopie: " + REPO + ":rollback-20261015-101500");
    assert(!c.$("dm-dialog-log").textContent.includes("@@RESULT"));

    // 4. A major needs its tick box; without dump -> --no-dump; back to the configured tag
    const m = boot("de", { ...BASE, "diun-update.py retag": (cmd) => DRY()(cmd) || runOk });
    await sleep(80);
    rowOf(m.d, "plausible").querySelector("td.az-cell-actions button[data-retag]").click();
    await sleep(10);
    choose(m, "v3.2.1");
    await sleep(40);
    const mc = m.$("dm-dialog-confirm");
    const mpv = m.$("dm-retag-preview");
    console.log("\nmajor  :", mpv.querySelector(".dm-callout-warn").textContent);
    assert(kvRows(mpv).includes("Bringt: 3.2.1 Major"));
    assert(mc.disabled, "waits for the tick box");
    tick(m, "dm-major-check", true);
    assert(!mc.disabled);
    choose(m, "");
    assert.strictEqual(mpv.innerHTML, "", "the configured tag: nothing to show");
    assert(mc.disabled, "and nothing to confirm");
    choose(m, "v3.2.1");
    await sleep(40);
    assert(mc.disabled, "a new preview asks again");
    tick(m, "dm-major-check", true);
    m.$("dm-dump-check").checked = false;
    mc.click();
    await sleep(60);
    assert.deepStrictEqual(m.last("retag").cmd.slice(1), ["retag", "plausible", "v3.2.1", "--expect", FP["v3.2.1"], "--no-dump"]);

    // 5. A tag that follows every release: its own warning, not "installed version unknown"
    const f = boot("de", { ...BASE, "diun-update.py retag": DRY() });
    await sleep(80);
    rowOf(f.d, "plausible").querySelector("td.az-cell-actions button[data-retag]").click();
    await sleep(10);
    choose(f, "latest");
    await sleep(40);
    const fw = f.$("dm-retag-preview").querySelector(".dm-gate-group");
    console.log("\nlatest :", fw.textContent.replace(/\s+/g, " ").trim());
    assert(fw.querySelector(".dm-callout-warn").textContent.startsWith("latest folgt jeder neuen Version, auch neuen Hauptversionen"));
    assert.strictEqual(fw.querySelector(".dm-check").textContent.trim(), "Mir ist bewusst, dass latest jeder neuen Version folgt.");
    assert(!f.$("dm-retag-preview").textContent.includes("unbekannt"));
    assert(f.$("dm-dialog-confirm").disabled);
    tick(f, "dm-jump-check", true);
    assert(!f.$("dm-dialog-confirm").disabled);

    // 6. A later answer for an earlier choice is dropped
    const s = boot("de", { ...BASE, "diun-update.py retag": (cmd) => ({ ...DRY()(cmd), delay: cmd[3] === "v3.2.1" ? 80 : 10 }) });
    await sleep(80);
    rowOf(s.d, "plausible").querySelector("td.az-cell-actions button[data-retag]").click();
    await sleep(10);
    choose(s, "v3.2.1");
    choose(s, "v2");
    await sleep(120);
    const sdiff = s.$("dm-retag-preview").querySelector(".dm-diff-new").textContent;
    console.log("\nlast choice wins:", sdiff);
    assert.strictEqual(sdiff, "+     image: " + REPO + ":v2");
    assert(!s.$("dm-major-check"), "the slower major plan did not replace it");
    assert(kvRows(s.$("dm-retag-preview")).includes("Bringt: 2.1.5 Patch"));

    // 7. Refusals and hints
    const refusals = [
        [{ possible: false, reason: "variable-shared", detail: "PLAUSIBLE_VERSION", changes: [], fingerprint: null },
         "Über das Addon nicht möglich: Die Variable PLAUSIBLE_VERSION wird auch anderswo verwendet; sie zu ändern, würde mehr als dieses Image ändern."],
        [{ possible: false, reason: "anchors", detail: "/opt/plausible/compose.yml:12", changes: [], fingerprint: null },
         "Über das Addon nicht möglich: Die Image-Zeile steht unter einem YAML-Anker oder außerhalb eines Service " +
         "(/opt/plausible/compose.yml:12). Bitte ändern Sie den Tag von Hand."],
        [{ possible: false, reason: "something-new", detail: "x", changes: [], fingerprint: null },
         "Über das Addon nicht möglich: something-new (x)"],
    ];
    for (const [rt, want] of refusals) {
        const r = boot("de", { ...BASE, "diun-update.py retag": DRY(rt) });
        await sleep(80);
        rowOf(r.d, "plausible").querySelector("td.az-cell-actions button[data-retag]").click();
        await sleep(10);
        choose(r, "v2.1.5");
        await sleep(40);
        console.log("refused:", r.$("dm-retag-preview").textContent);
        assert.strictEqual(r.$("dm-retag-preview").textContent, want);
        assert(r.$("dm-dialog-confirm").disabled);
    }
    const nf = boot("de", { ...BASE, "diun-update.py retag": (cmd) => ({ out: JSON.stringify({ ...dryRun(cmd[3]), updatable: false,
        reason: "compose-file-missing", missing: ["/opt/plausible/compose.yml"],
        retag: { ...dryRun(cmd[3]).retag, possible: false, reason: "compose-file-missing", changes: [], fingerprint: null } }) }) });
    await sleep(80);
    rowOf(nf.d, "plausible").querySelector("td.az-cell-actions button[data-retag]").click();
    await sleep(10);
    choose(nf, "v2");
    await sleep(40);
    assert.strictEqual(nf.$("dm-retag-preview").textContent,
                       "Über das Addon nicht möglich: Die Compose-Datei existiert nicht mehr (/opt/plausible/compose.yml).");
    const hv = boot("de", { ...BASE, "diun-update.py retag": DRY({ variable: "PLAUSIBLE_VERSION", git: "/opt",
        changes: [{ file: "/opt/plausible/.env", line: 3, old: "PLAUSIBLE_VERSION=v2.1.4", new: "PLAUSIBLE_VERSION=v2",
                    service: null, sha256: "e".repeat(64) }] }) });
    await sleep(80);
    rowOf(hv.d, "plausible").querySelector("td.az-cell-actions button[data-retag]").click();
    await sleep(10);
    choose(hv, "v2");
    await sleep(40);
    const hints = [...hv.$("dm-retag-preview").querySelectorAll(".dm-callout:not(.dm-callout-warn)")].map((x) => x.textContent);
    console.log("hints  :", hints.slice(0, 2).join(" | "));
    assert.strictEqual(hv.d.querySelector(".dm-diff-file").textContent, "/opt/plausible/.env:3");
    assert.strictEqual(hints[0], "Der Tag kommt aus der Variable PLAUSIBLE_VERSION; sie wird dort geändert, wo sie gesetzt ist.");
    assert.strictEqual(hints[1], "Das Projekt liegt im Git-Repository /opt. Das Addon committet nicht; übernehmen Sie die Änderung dort, " +
                                 "wenn sie in die Historie soll.");
    const na = boot("en", { ...BASE, "diun-update.py retag": { fail: { problem: "access-denied" } } });
    await sleep(80);
    rowOf(na.d, "plausible").querySelector("td.az-cell-actions button[data-retag]").click();
    await sleep(10);
    choose(na, "v2");
    await sleep(40);
    assert(na.$("dm-retag-preview").textContent.startsWith("Denied"));

    // 8. Failure -> cause, rollback offered; a rollback that finds an edited file stops
    const RS = { container: "plausible", state: "restarting", exit_code: 1, oom: false, source: "log", line: "Error: boom" };
    const fl = boot("de", { ...BASE, "diun-update.py retag": (cmd) => DRY()(cmd) || { chunks: ["…\n", "@@RESULT " + JSON.stringify(
            { op: "20261015-101500-plausible", status: "failed", reason: RS, image: REPO + ":v2" }) + "\n"], fail: { exit_status: 4 } },
        "diun-update.py rollback": { chunks: ["/opt/plausible/compose.yml changed after the tag change – nothing changed.\n"],
                                     fail: { exit_status: 5 } } });
    await sleep(80);
    rowOf(fl.d, "plausible").querySelector("td.az-cell-actions button[data-retag]").click();
    await sleep(10);
    choose(fl, "v2");
    await sleep(40);
    fl.$("dm-dialog-confirm").click();
    await sleep(60);
    console.log("\nfailed :", fl.$("dm-dialog-result").textContent);
    assert.strictEqual(fl.$("dm-dialog-result").textContent,
        "Tag-Wechsel fehlgeschlagen – die Container laufen nicht stabil. Zurückrollen stellt die Dateien und das vorherige Image wieder her.");
    assert.strictEqual(fl.d.querySelector("#dm-dialog-reason .dm-reason-line").textContent, "Error: boom");
    assert(!fl.$("dm-dialog-extra").hidden);
    fl.$("dm-dialog-extra").click();
    await sleep(60);
    assert.deepStrictEqual(fl.last("rollback").cmd.slice(1), ["rollback", "20261015-101500-plausible"]);
    console.log("rollback:", fl.$("dm-dialog-result").textContent);
    assert.strictEqual(fl.$("dm-dialog-result").textContent, "Rollback abgebrochen, bevor etwas geändert wurde – siehe Ausgabe.");
    const ab = boot("de", { ...BASE, "diun-update.py retag": (cmd) => DRY()(cmd) || { chunks: ["The files changed since the preview\n"],
                                                                                      fail: { exit_status: 5 } } });
    await sleep(80);
    rowOf(ab.d, "plausible").querySelector("td.az-cell-actions button[data-retag]").click();
    await sleep(10);
    choose(ab, "v2");
    await sleep(40);
    ab.$("dm-dialog-confirm").click();
    await sleep(60);
    assert.strictEqual(ab.$("dm-dialog-result").textContent, "Abgebrochen, bevor etwas geändert wurde – siehe Ausgabe.");

    // 9. History: the tag change under the image; its rollback names the files
    const h = boot("de", { ...BASE, "diun-update.py rollback": { chunks: ['@@RESULT {"status":"ok"}\n'] } });
    await sleep(80);
    h.$("dm-tab-btn-backups").click();
    await sleep(20);
    const opRow = h.d.querySelector("#dm-ops tr");
    console.log("\nhistory:", [...opRow.querySelectorAll("td")].map((td) => td.textContent.trim()).filter(Boolean).join(" | "));
    assert.strictEqual(opRow.querySelector(".dm-row-note").textContent, "Tag geändert: v2.1.4 → v2");
    opRow.querySelector("button[data-op]").click();
    console.log("rollback dialog:", h.$("dm-dialog-desc").textContent);
    assert(h.$("dm-dialog-desc").textContent.startsWith(
        "Stellt /opt/plausible/compose.yml, /opt/plausible/.env wieder her, wie sie vor dem Tag-Wechsel waren, setzt " + REPO + ":v2.1.4 wieder auf"));
    h.$("dm-dialog-confirm").click();
    await sleep(60);
    assert.strictEqual(h.$("dm-dialog-result").textContent, "Rollback erfolgreich.");

    // 10. English: no German in the row, the field, the preview and the history
    const en = boot("en", { ...BASE, "diun-update.py retag": DRY({ variable: "V", git: "/opt" }) });
    await sleep(80);
    rowOf(en.d, "glitchtip-postgres").querySelector("td.az-cell-actions button[data-retag]").click();
    await sleep(10);
    const enOpts = optionsOf(en.d).join(" / ");
    en.$("dm-dialog-cancel").click();
    rowOf(en.d, "plausible").querySelector("td.az-cell-actions button[data-retag]").click();
    await sleep(10);
    choose(en, "latest");
    await sleep(40);
    en.$("dm-tab-btn-backups").click();
    await sleep(20);
    const all = en.d.documentElement.outerHTML + enOpts;
    assert(enOpts.includes("18-alpine – database major, needs a migration"));
    assert(all.includes("Tag changed: v2.1.4 → v2") && all.includes("latest follows every new release"));
    const german = ["Version ändern", "Versionslinien", "festsetzen", "läuft jetzt", "derzeit", "Datenbank", "Hauptversion",
                    "Folgt jeder", "Prüfe", "Änderung", "Variable", "geändert", "konfiguriert", "Klicken", "Bringt", "bewusst"];
    const found = german.filter((g) => all.includes(g));
    console.log("\nEN German leftovers:", found.length ? found : "none");
    assert.deepStrictEqual(found, []);

    console.log("\nALL CHECKS PASSED");
})().catch((e) => { console.error("FEHLGESCHLAGEN:", e.stack || e.message); process.exit(1); });

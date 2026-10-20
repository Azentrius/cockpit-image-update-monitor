// SPDX-License-Identifier: Apache-2.0
// Language selection and requirements panel / install dialog, against the
// real diun.js in jsdom with a scripted Cockpit stub.
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { JSDOM } = require("jsdom");

const PLUGIN = path.join(__dirname, "..", "plugin");
const FIX = path.join(__dirname, "fixtures");
const html = fs.readFileSync(path.join(PLUGIN, "index.html"), "utf8");
// The page as index.html loads it: the texts (i18n.js), then the code (diun.js).
const js = ["i18n.js", "diun.js"].map((f) => fs.readFileSync(path.join(PLUGIN, f), "utf8")).join("\n");
const inventory = fs.readFileSync(path.join(FIX, "inventory3.jsonl"), "utf8");
const files = {
    "/var/lib/diun/events.jsonl": fs.readFileSync(path.join(FIX, "events.jsonl"), "utf8"),
    "/var/lib/diun-radar/versions.json": JSON.parse(fs.readFileSync(path.join(FIX, "versions2.json"), "utf8")),
    "/var/lib/diun/dismissed.json": null,
};
const DEPS_OK = {
    root: true,
    docker: { state: "ok", version: "29.5.2", compose: true },
    diun: { state: "running", managed: true, events: true, can_install: false, can_start: false },
    skopeo: { state: "ok", version: "1.13.3", can_install: true },
    radar: { state: "active", can_install: true },
};
const clone = (o) => JSON.parse(JSON.stringify(o));
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

function boot({ language, deps = DEPS_OK, install } = {}) {
    const dom = new JSDOM(html, { runScripts: "outside-only" });
    const w = dom.window;
    w.document.querySelectorAll("script").forEach((s) => s.remove());   // the page's own tags never run
    // jsdom has no modal dialogs: minimal stand-ins.
    w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
    w.HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
    w.Date.now = () => Date.parse("2026-10-12T02:50:00Z");
    const calls = [];
    w.cockpit = {
        language,
        file(p) {
            return {
                read() { return Promise.resolve(files[p] === undefined ? null : files[p]); },
                watch(cb) { cb(files[p], "t1"); },
                replace() { return Promise.resolve(); },
            };
        },
        spawn(cmd, opts) {
            calls.push({ cmd, opts });
            if (cmd[0].endsWith("diun-inventory.py")) return Promise.resolve(inventory);
            if (cmd[1] === "check") return Promise.resolve(JSON.stringify(typeof deps === "function" ? deps() : deps));
            if (cmd[1] === "backups") return Promise.resolve(JSON.stringify({ dumps: [], images: [], ops: [] }));
            // install/start: scripted stream + result
            let streamCb = null;
            const p = new Promise((resolve, reject) => {
                setTimeout(() => {
                    (install.chunks || []).forEach((c) => streamCb && streamCb(c));
                    install.fail ? reject(install.fail) : resolve("");
                }, 20);
            });
            p.stream = (cb) => { streamCb = cb; return p; };
            track(p);
            return p;
        },
        transport: { wait() {} },
    };
    w.eval(js);
    const d = w.document;
    return { w, d, calls, text: () => d.body.textContent.replace(/\s+/g, " ") };
}

(async () => {
    // 1. Language selection
    for (const [lang, expect] of [["de", "de"], ["de_AT", "de"], ["en", "en"], ["fr", "en"], [undefined, "en"]]) {
        const a = boot({ language: lang });
        await settle();
        const heads = [...a.d.querySelectorAll("thead th")].map((x) => x.textContent).join("|");
        const btn = a.d.querySelector("#dm-refresh .dm-btn-label").textContent;
        console.log(`language=${String(lang).padEnd(9)} -> html lang=${a.d.documentElement.lang} | ${btn} | ${heads}`);
        assert.strictEqual(a.d.documentElement.lang, expect);
        assert.strictEqual(btn, expect === "de" ? "Aktualisieren" : "Refresh");
        assert(!a.d.getElementById("dm-deps").hidden === false, "panel hidden when all ok");
    }

    // 2. English mode leaves no German strings anywhere (visible text, labels, tooltips)
    const en = boot({ language: "en" });
    await settle();
    const all = en.d.documentElement.outerHTML;
    const german = ["Aktualisieren", "Lädt", "Übersicht", "Installierte", "Neue Version", "aktuell<", "unbekannt", "fließend",
                    "Erledigt", "neueste", "vor ", "Datenbank", "Abbrechen", "Voraussetzungen", "Keine laufenden"];
    const found = german.filter((g) => all.includes(g));
    console.log("EN: German leftovers:", found.length ? found : "none");
    assert.deepStrictEqual(found, []);
    console.log("EN meta:", en.d.getElementById("dm-meta").textContent);
    const enRow = [...en.d.querySelectorAll("#dm-rows tr")].find((tr) => tr.textContent.includes("plausible_db"));
    console.log("EN row :", [...enRow.querySelectorAll("td")].map((td) => `${td.dataset.label}=${td.textContent.trim()}`).join(" | "));

    // 3. Requirements missing -> panel with the right items and buttons (German)
    const missing = clone(DEPS_OK);
    missing.skopeo = { state: "missing", version: "", can_install: true };
    missing.radar = { state: "inactive", can_install: true };
    missing.diun = { state: "stopped", managed: true, events: true, can_install: false, can_start: true };
    let currentDeps = missing;
    const b = boot({ language: "de", deps: () => currentDeps,
                     install: { chunks: ["+ apt-get update\n", "…\n", "skopeo installiert.\n"] } });
    await settle();
    const panel = b.d.getElementById("dm-deps");
    const items = [...b.d.querySelectorAll(".dm-dep")].map((li) => li.textContent.replace(/\s+/g, " ").trim());
    console.log("\nDE panel visible:", !panel.hidden);
    items.forEach((i) => console.log("  -", i));
    assert(!panel.hidden && items.length === 3);
    const btns = [...b.d.querySelectorAll(".dm-dep button")].map((x) => `${x.dataset.verb}:${x.dataset.target}:${x.textContent}`);
    console.log("  buttons:", btns.join(", "));
    assert.deepStrictEqual(btns, ["start:diun:Diun starten", "install:skopeo:skopeo installieren", "install:radar:Radar einrichten"]);
    assert(panel.classList.contains("az-alert--warning") && !panel.classList.contains("az-alert--danger"), "missing parts: a warning");

    // 4. Install skopeo: dialog, confirm, streamed log, success, re-check
    b.d.querySelector('.dm-dep button[data-target="skopeo"]').click();
    const dlg = b.d.getElementById("dm-dialog");
    assert(dlg.hasAttribute("open"));
    console.log("\nDialog:", b.d.getElementById("dm-dialog-title").textContent, "|",
                b.d.querySelector("#dm-dialog-confirm .dm-btn-label").textContent);
    assert.strictEqual(b.d.querySelector("#dm-dialog-confirm .dm-btn-label").textContent, "skopeo installieren");
    assert.deepStrictEqual([...b.d.querySelectorAll("#dm-dialog-desc code")].map((x) => x.textContent), ["skopeo"],
                           "package names, paths and commands in the text as code");
    const checksBefore = b.calls.filter((c) => c.cmd[1] === "check").length;
    currentDeps = clone(missing); currentDeps.skopeo = DEPS_OK.skopeo;   // after install
    b.d.getElementById("dm-dialog-confirm").click();
    const call = b.calls.find((c) => c.cmd[1] === "install");
    console.log("spawn  :", JSON.stringify(call.cmd), JSON.stringify(call.opts));
    // (arrays come from the jsdom realm, so compare their JSON)
    assert.strictEqual(JSON.stringify(call.cmd), JSON.stringify(["/usr/lib/cockpit-image-updates/diun-deps.sh", "install", "skopeo"]));
    assert.strictEqual(call.opts.superuser, "require");
    assert.strictEqual(JSON.stringify(call.opts.environ), JSON.stringify(["DIUN_LANG=de"]));
    assert(b.d.getElementById("dm-dialog-cancel").disabled, "cancel locked while running");
    await settle();
    console.log("log    :", JSON.stringify(b.d.getElementById("dm-dialog-log").textContent));
    console.log("result :", b.d.getElementById("dm-dialog-result").textContent,
                "| close btn:", b.d.getElementById("dm-dialog-cancel").textContent);
    assert.strictEqual(b.d.getElementById("dm-dialog-result").textContent, "Erfolgreich abgeschlossen.");
    const checksAfter = b.calls.filter((c) => c.cmd[1] === "check").length;
    assert(checksAfter > checksBefore, "requirements re-checked after install");
    const left = [...b.d.querySelectorAll(".dm-dep button")].map((x) => x.dataset.target);
    console.log("panel after:", left.join(", "));
    assert(!left.includes("skopeo"));
    b.d.getElementById("dm-dialog-cancel").click();
    assert(!dlg.hasAttribute("open"));

    // 5. Failures: exit code, missing admin rights (English)
    const DENIED = "Denied: this needs administrative access. Switch it on at the top of Cockpit.";
    for (const [fail, expect] of [[{ exit_status: 3 }, "The action failed (exit code 3). The output above shows why."],
                                  [{ exit_status: 77 }, DENIED],
                                  [{ problem: "access-denied" }, DENIED]]) {
        const c = boot({ language: "en", deps: missing, install: { chunks: ["Aborted: /opt/diun already exists – nothing overwritten.\n"], fail } });
        await settle();
        c.d.querySelector('.dm-dep button[data-target="diun"]').click();
        assert.deepStrictEqual([...c.d.querySelectorAll("#dm-dialog-desc code")].map((x) => x.textContent),
                               ["/opt/diun", "docker compose up -d"]);
        c.d.getElementById("dm-dialog-confirm").click();
        await settle();
        const res = c.d.getElementById("dm-dialog-result");
        console.log("fail", JSON.stringify(fail).padEnd(26), "->", res.textContent);
        assert.strictEqual(res.textContent, expect);
        assert(res.classList.contains("is-error"));
    }

    // Docker unreachable: an error (label and panel), the hint is plain text
    const dk = clone(missing); dk.docker = { state: "no-daemon" };
    const g = boot({ language: "en", deps: dk });
    await settle();
    const docker = g.d.querySelector(".dm-dep");
    console.log("\ndocker:", docker.textContent.replace(/\s+/g, " ").trim());
    assert.deepStrictEqual([docker.querySelector(".az-label").className, docker.querySelector(".az-label").textContent],
                           ["az-label az-label--danger", "unreachable"]);
    assert(g.d.getElementById("dm-deps").classList.contains("az-alert--danger"), "Docker missing: the panel is an error");
    assert.strictEqual(docker.querySelector(".dm-dep-hint").textContent, "Install or start Docker on the server yourself.");

    // 6. Not admin -> single hint instead of a misleading list
    const na = clone(DEPS_OK); na.root = false; na.docker.state = "no-daemon";
    const e = boot({ language: "de", deps: na });
    await settle();
    console.log("\nnot admin:", e.d.getElementById("dm-deps-intro").textContent,
                "| items:", e.d.querySelectorAll(".dm-dep").length);
    assert(!e.d.getElementById("dm-deps").hidden && e.d.querySelectorAll(".dm-dep").length === 0);

    console.log("\nALL CHECKS PASSED");
})().catch((e) => { console.error("FEHLGESCHLAGEN:", e.stack || e.message); process.exit(1); });

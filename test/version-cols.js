// SPDX-License-Identifier: Apache-2.0
// Columns "Configured / Installed / Newest version" (2026-10-12):
// installed version from inventory (tag / env) or radar (label / digest),
// pulled-but-not-rolled-out arrow, reachable vs. outlined chips, unknown.
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { JSDOM } = require("jsdom");

const PLUGIN = path.join(__dirname, "..", "plugin");
const FIX = path.join(__dirname, "fixtures");
const html = fs.readFileSync(path.join(PLUGIN, "index.html"), "utf8").replace(/<script[^>]*><\/script>/g, "");
const js = fs.readFileSync(path.join(PLUGIN, "diun.js"), "utf8");
const inventory = fs.readFileSync(path.join(FIX, "inventory3.jsonl"), "utf8");
const radar = JSON.parse(fs.readFileSync(path.join(FIX, "versions2.json"), "utf8"));

function boot(language, files) {
    const dom = new JSDOM(html, { runScripts: "outside-only" });
    const w = dom.window;
    w.Date.now = () => Date.parse("2026-10-12T02:50:00Z");
    w.cockpit = {
        language,
        file(p) { return { read: () => Promise.resolve(files[p] ?? null), watch: () => {}, replace: () => Promise.resolve() }; },
        spawn(cmd) {
            const p = cmd[0].endsWith("diun-inventory.py") ? Promise.resolve(inventory)
                : cmd[0].endsWith("diun-deps.sh") ? Promise.resolve(JSON.stringify({ root: true, docker: { state: "ok" },
                    diun: { state: "running", events: true }, skopeo: { state: "ok" }, radar: { state: "active" } }))
                : Promise.reject(new Error("unexpected " + cmd.join(" ")));
            p.stream = () => p;
            return p;
        },
        transport: { wait() {} },
    };
    w.eval(js);
    return w.document;
}
const FILES = { "/var/lib/diun-radar/versions.json": radar, "/var/lib/diun/events.jsonl": "", "/var/lib/diun/dismissed.json": null };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const row = (d, name) => [...d.querySelectorAll("#dm-rows tr")].find((tr) => tr.querySelector("td").textContent === name);
const cells = (tr) => [...tr.querySelectorAll("td")];

(async () => {
    const d = boot("de", FILES);
    await sleep(60);
    const heads = [...d.querySelectorAll("#dm-table thead th")].map((t) => t.textContent);
    console.log("cols:", heads.join(" | "));
    assert.deepStrictEqual(heads.slice(0, 6).join("|"), "Container|Image|Konfiguriert|Installiert|Neueste Version|Status");

    // pulled, not rolled out: 16.14 -> 16.15, source tooltip names the variable
    let c = cells(row(d, "glitchtip-postgres"));
    console.log("glitchtip-postgres:", c.slice(2, 6).map((x) => x.textContent.trim()).join(" | "));
    assert.strictEqual(c[2].textContent, "16-alpine");
    assert.strictEqual(c[3].textContent.trim(), "16.14 → 16.15");
    assert(c[3].querySelector(".dm-ver").title.includes("PG_VERSION"));
    assert(c[3].querySelector(".dm-arrow").title.includes("16.15 liegt schon auf dem Server"));

    // latest resolved by digest comparison
    c = cells(row(d, "ollama"));
    console.log("ollama:", c[3].textContent.trim(), "|", c[3].querySelector(".dm-ver").title.replace(/\n/g, " / "));
    assert.strictEqual(c[3].textContent.trim(), "0.30.11");
    assert(c[3].querySelector(".dm-ver").title.includes("Registry-Tag 0.30.11"));
    assert(c[3].querySelector(".dm-ver").title.includes("Digest: c484b703176a"));

    // pinned tag: newer versions only via a changed tag -> outlined chips
    c = cells(row(d, "plausible"));
    const manual = [...c[4].querySelectorAll(".dm-chip")].map((x) => x.classList.contains("dm-chip-manual"));
    console.log("plausible chips manual:", manual, "|", c[4].querySelector(".dm-chip").title.replace(/\n/g, " / "));
    assert.deepStrictEqual(manual.join(), "true,true");
    assert(c[4].querySelector(".dm-chip").title.includes("derzeit v2.1.4"));

    // latest: all levels reachable with the Update button -> filled
    c = cells(row(d, "docuseal"));
    assert(![...c[4].querySelectorAll(".dm-chip")].some((x) => x.classList.contains("dm-chip-manual")));
    assert(c[4].querySelector(".dm-chip").title.includes("Der Tag latest liefert das per Update-Knopf"));

    // 16-alpine: minor reachable (filled), DB major grey info
    c = cells(row(d, "calcom-db"));
    const kinds = [...c[4].querySelectorAll(".dm-chip")].map((x) => x.className);
    console.log("calcom-db chips:", kinds.join(" / "));
    assert.deepStrictEqual(kinds.join("|"), "dm-chip dm-chip-minor|dm-chip dm-chip-info");

    // up to date shows the version with a check mark
    assert.strictEqual(cells(row(d, "calcom"))[4].textContent.trim(), "✓ 6.2.0");

    // unknown installed version: newest shown, status not "aktuell"
    c = cells(row(d, "formbricks-app"));
    console.log("formbricks-app:", c.slice(3, 6).map((x) => x.textContent.trim()).join(" | "));
    assert.strictEqual(c[3].textContent.trim(), "?");
    assert.strictEqual(c[4].textContent.trim(), "neueste: 6.0.2");
    assert.strictEqual(c[5].textContent.trim(), "Unbekannt");

    // stale registry data is marked
    assert(cells(row(d, "diun-socket-proxy"))[4].textContent.includes("(Stand älter)"));
    assert(!d.getElementById("dm-legend").hidden, "legend visible");
    console.log("legend:", d.getElementById("dm-legend").textContent.replace(/\s+/g, " ").trim());

    // English
    const e = boot("en", FILES);
    await sleep(60);
    const eh = [...e.querySelectorAll("#dm-table thead th")].map((t) => t.textContent).slice(2, 5);
    console.log("EN cols:", eh.join(" | "), "| ollama tip:", cells(row(e, "ollama"))[3].querySelector(".dm-ver").title.split("\n")[0]);
    assert.deepStrictEqual(eh.join("|"), "Configured|Installed|Newest version");
    assert(!/Installiert|neueste|Umrandet|liefert|Registry-Tag /.test(e.body.textContent + [...e.querySelectorAll("[title]")].map((x) => x.title).join(" ")));

    // no radar file: legend hidden, installed from inventory still shown
    const n = boot("de", { ...FILES, "/var/lib/diun-radar/versions.json": null });
    await sleep(60);
    assert(n.getElementById("dm-legend").hidden);
    assert.strictEqual(cells(row(n, "glitchtip-web"))[3].textContent.trim(), "6.1.8");
    assert.strictEqual(cells(row(n, "ollama"))[3].textContent.trim(), "?");

    console.log("\nALL CHECKS PASSED");
})().catch((e) => { console.error(e); process.exit(1); });

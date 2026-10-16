// SPDX-License-Identifier: Apache-2.0
// Columns "Configured / Installed / Newest version" (2026-10-12):
// installed version from inventory (tag / env) or radar (label / digest),
// pulled-but-not-rolled-out arrow, the level labels (Update button, another
// tag, a database migration), unknown.
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { JSDOM } = require("jsdom");

const PLUGIN = path.join(__dirname, "..", "plugin");
const FIX = path.join(__dirname, "fixtures");
const html = fs.readFileSync(path.join(PLUGIN, "index.html"), "utf8").replace(/<script[^>]*><\/script>/g, "");
const js = fs.readFileSync(path.join(PLUGIN, "diun.js"), "utf8");
const icons = fs.readFileSync(path.join(PLUGIN, "icons.js"), "utf8");
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
    w.eval(icons);
    w.eval(js);
    return w.document;
}
const FILES = { "/var/lib/diun-radar/versions.json": radar, "/var/lib/diun/events.jsonl": "", "/var/lib/diun/dismissed.json": null };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const row = (d, name) => [...d.querySelectorAll("#dm-rows tr")].find((tr) => tr.querySelector(".dm-name").textContent === name);
const cells = (tr) => [...tr.querySelectorAll("td")];
const levels = (td) => [...td.querySelectorAll(".dm-level")];
const tipOf = (lv) => (lv.closest("button.dm-level-btn") || lv).title;
const iconOf = (el) => { const s = el.querySelector("svg.az-icon"); return s ? s.getAttribute("data-icon") : null; };

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

    // pinned tag: newer versions only via a changed tag -> neutral labels
    c = cells(row(d, "plausible"));
    const reach = levels(c[4]).map((x) => x.dataset.reach);
    console.log("plausible levels:", reach, "|", tipOf(levels(c[4])[0]).replace(/\n/g, " / "));
    assert.deepStrictEqual(reach.join(), "tag,tag");
    assert(tipOf(levels(c[4])[0]).includes("derzeit v2.1.4"));
    assert(levels(c[4]).every((x) => x.className === "az-label dm-level" && !iconOf(x)), "another tag: neutral, no icon");

    // latest: all levels reachable with the Update button -> hints
    c = cells(row(d, "docuseal"));
    assert(levels(c[4]).length && levels(c[4]).every((x) => x.dataset.reach === "update"));
    assert(levels(c[4]).every((x) => x.className === "az-label az-label--info dm-level" && iconOf(x) === "info-circle"));
    assert(tipOf(levels(c[4])[0]).includes("Der Tag latest liefert das per Update-Knopf"));

    // 16-alpine: minor reachable (hint), DB major without judgement (ban)
    c = cells(row(d, "calcom-db"));
    const kinds = levels(c[4]).map((x) => `${x.className}:${iconOf(x)}:${x.dataset.level}:${x.dataset.reach}`);
    console.log("calcom-db levels:", kinds.join(" / "));
    assert.deepStrictEqual(kinds, ["az-label az-label--info dm-level:info-circle:minor:update",
                                   "az-label dm-level:ban:major:migration"]);
    assert(levels(c[4])[0].textContent.startsWith("Minor "), "the level as a word");

    // up to date shows the version with check-circle
    const ok = cells(row(d, "calcom"))[4];
    assert.deepStrictEqual([ok.textContent.trim(), iconOf(ok)], ["6.2.0", "check-circle"]);

    // a floating tag: what it points to in the registry right now
    const tagNow = (n) => { const x = cells(row(d, n))[2]; return [x.textContent.trim(), (x.querySelector(".dm-tagnow") || {}).title]; };
    console.log("tag_now:", ["ollama", "formbricks-app", "glitchtip-postgres"].map((n) => n + " -> " + tagNow(n)[0]).join(" | "));
    assert.deepStrictEqual(tagNow("ollama"), ["latest = 0.40.3", "In der Registry zeigt latest gerade auf 0.40.3 (vom Radar geprüft)."]);
    assert.strictEqual(tagNow("formbricks-app")[0], "latest = 6.0.2");
    assert.deepStrictEqual(tagNow("glitchtip-postgres"), ["16-alpine", undefined], "no tag_now: the tag alone");
    // pg17 = 0.8.7 counts pgvector releases, the installed 17.10 is PostgreSQL's: not shown
    assert.deepStrictEqual(tagNow("formbricks-db"), ["pg17", undefined], "tag_now in another numbering: the tag alone");

    // unknown installed version: newest shown, status not "aktuell"
    c = cells(row(d, "formbricks-app"));
    console.log("formbricks-app:", c.slice(3, 6).map((x) => x.textContent.trim()).join(" | "));
    assert.strictEqual(c[3].textContent.trim(), "?");
    assert.strictEqual(c[4].textContent.trim(), "neueste: 6.0.2");
    assert.strictEqual(c[5].textContent.trim(), "Unbekannt");

    // stale registry data is marked
    assert(cells(row(d, "diun-socket-proxy"))[4].textContent.includes("(Stand älter)"));
    const legend = d.getElementById("dm-legend");
    assert(!legend.hidden, "legend visible");
    console.log("legend:", legend.textContent.replace(/\s+/g, " ").trim());
    assert.deepStrictEqual([...legend.querySelectorAll(".az-label")].map((x) => `${x.className}:${iconOf(x)}`),
        ["az-label az-label--info:info-circle", "az-label:null", "az-label:ban"], "the legend shows the same labels");

    // No registry shows the image (built or tagged on the server, 2026-10-16), or
    // a registry error without earlier data: said so, and with nothing compared
    // the status is never "up to date" (the inventory still has a digest).
    const DENIED = 'level=fatal msg="Error listing repository tags: fetching tags list: requested access to the resource is denied"';
    const local = JSON.parse(JSON.stringify(radar));
    for (const [image, status, error] of [["onlyoffice/documentserver", "unlisted", DENIED],
                                          ["crazymax/diun", "error", "registry timeout"]]) {
        const r = local.images.find((x) => x.image === image);
        ["versions", "by_tag", "newest"].forEach((k) => delete r[k]);
        Object.assign(r, { status, error });
    }
    assert.strictEqual(cells(row(d, "onlyoffice-ds"))[5].textContent.trim(), "Aktuell", "compared: up to date");
    const u = boot("de", { ...FILES, "/var/lib/diun-radar/versions.json": local });
    await sleep(60);
    c = cells(row(u, "onlyoffice-ds"));
    console.log("onlyoffice-ds unlisted:", c.slice(3, 6).map((x) => x.textContent.trim()).join(" | "));
    assert.deepStrictEqual(c.slice(3, 6).map((x) => x.textContent.trim()), ["9.4.0.1", "nicht in einer Registry", "Unbekannt"]);
    let tip = c[4].querySelector(".dm-muted").title;
    assert(tip.startsWith("Keine Registry zeigt onlyoffice/documentserver: Es wurde auf diesem Server gebaut"), tip);
    assert(tip.endsWith("\n" + DENIED), tip);
    c = cells(row(u, "diun"));
    console.log("diun registry error:", c.slice(4, 6).map((x) => x.textContent.trim()).join(" | "));
    assert.deepStrictEqual(c.slice(4, 6).map((x) => x.textContent.trim()), ["Registry-Fehler", "Unbekannt"]);
    assert(c[4].querySelector(".dm-muted").title.endsWith("der nächste Lauf versucht es erneut.\nregistry timeout"));
    const ue = boot("en", { ...FILES, "/var/lib/diun-radar/versions.json": local });
    await sleep(60);
    c = cells(row(ue, "onlyoffice-ds"));
    assert.deepStrictEqual(c.slice(4, 6).map((x) => x.textContent.trim()), ["not in a registry", "Unknown"]);
    tip = c[4].querySelector(".dm-muted").title;
    assert(tip.startsWith("No registry shows onlyoffice/documentserver: it was built or tagged on this server"), tip);
    assert(cells(row(ue, "diun"))[4].querySelector(".dm-muted").title.startsWith("The registry did not answer properly"));

    // English
    const e = boot("en", FILES);
    await sleep(60);
    const eh = [...e.querySelectorAll("#dm-table thead th")].map((t) => t.textContent).slice(2, 5);
    console.log("EN cols:", eh.join(" | "), "| ollama tip:", cells(row(e, "ollama"))[3].querySelector(".dm-ver").title.split("\n")[0]);
    assert.deepStrictEqual(eh.join("|"), "Configured|Installed|Newest version");
    assert(!/Installiert|neueste|Umrandet|liefert|Registry-Tag |zeigt gerade/.test(e.body.textContent + [...e.querySelectorAll("[title]")].map((x) => x.title).join(" ")));
    assert.strictEqual(cells(row(e, "ollama"))[2].querySelector(".dm-tagnow").title, "In the registry, latest points to 0.40.3 right now (checked by the radar).");

    // no radar file: legend hidden, installed from inventory still shown
    const n = boot("de", { ...FILES, "/var/lib/diun-radar/versions.json": null });
    await sleep(60);
    assert(n.getElementById("dm-legend").hidden);
    assert.strictEqual(cells(row(n, "glitchtip-web"))[3].textContent.trim(), "6.1.8");
    assert.strictEqual(cells(row(n, "ollama"))[3].textContent.trim(), "?");

    console.log("\nALL CHECKS PASSED");
})().catch((e) => { console.error(e); process.exit(1); });

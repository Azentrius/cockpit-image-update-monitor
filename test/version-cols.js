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

function boot(language, files, inv = inventory) {
    const dom = new JSDOM(html, { runScripts: "outside-only" });
    const w = dom.window;
    w.Date.now = () => Date.parse("2026-10-12T02:50:00Z");
    w.cockpit = {
        language,
        file(p) { return { read: () => Promise.resolve(files[p] ?? null), watch: () => {}, replace: () => Promise.resolve() }; },
        spawn(cmd) {
            const p = cmd[0].endsWith("diun-inventory.py") ? Promise.resolve(inv)
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

    // A development build without a version that names its source revision
    // (pomerium:main on a test host, 2026-10-18): "latest @35b2a97" instead of
    // "?". With a known version the revision is not shown.
    const REV = "35b2a975d74519de0678486bdbd46f644533b710";
    const withRev = inventory.trim().split("\n").map((l) => JSON.parse(l)).map((x) =>
        (x.name === "formbricks-app" || x.name === "ollama" ? { ...x, revision: REV, created: "2026-10-13T23:30:19Z" } : x))
        .map((x) => JSON.stringify(x)).join("\n") + "\n";
    for (const [lang, start] of [["de", "Keine Versionsnummer: Das Image nennt nur den Quellstand, aus dem es gebaut wurde, " + REV + "."],
                                 ["en", "No version number: the image names only the source revision it was built from, " + REV + "."]]) {
        const r = boot(lang, FILES, withRev);
        await sleep(60);
        const cell = cells(row(r, "formbricks-app"))[3];
        console.log("revision " + lang + ":", cell.textContent.trim(), "|", cell.querySelector(".dm-ver").title.replace(/\n/g, " / "));
        assert.strictEqual(cell.textContent.trim(), "latest @35b2a97");
        const tip = cell.querySelector(".dm-ver").title.split("\n");
        assert.strictEqual(tip[0], start);
        assert(tip[1].startsWith(lang === "de" ? "Gebaut am " : "Built on "), tip[1]);
        assert.strictEqual(cells(row(r, "formbricks-app"))[5].textContent.trim(), lang === "de" ? "Unbekannt" : "Unknown");
        assert.strictEqual(cells(row(r, "ollama"))[3].textContent.trim(), "0.30.11", "a known version wins");
    }

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

    // latest points to no release tag but to a build pushed after the newest
    // release (MobSF on a test host, 2026-10-16: latest = a 4.5.4 build, v4.5.3
    // the newest release): said so instead of "newest: 5.1.0", and up to date
    // while the container runs that very build.
    const dev = JSON.parse(JSON.stringify(radar));
    const pf = dev.images.find((x) => x.image === "boky/postfix");
    Object.keys(pf.installed).forEach((k) => { pf.installed[k] = { version: null, src: "digest", tag: null, ts: pf.checked }; });
    delete pf.versions;
    Object.assign(pf, { status: "unknown", tag_now: {
        digest: "sha256:aafc772384232497bed875e1eb66b4d3e54ba1ebc86e2e185a6dc1dbc48182ef", version: null, tag: null,
        pushed: "2026-10-13T05:15:45Z", after: { tag: "5.1.0", pushed: "2026-09-21T03:04:04Z" } } });
    const b = boot("de", { ...FILES, "/var/lib/diun-radar/versions.json": dev });
    await sleep(60);
    c = cells(row(b, "smtp-relay"));
    console.log("smtp-relay, build after a release:", c.slice(2, 6).map((x) => x.textContent.trim()).join(" | "));
    assert.deepStrictEqual(c.slice(2, 6).map((x) => x.textContent.trim()), ["latest", "?", "Build nach 5.1.0", "Aktuell"]);
    assert.strictEqual(iconOf(c[4]), "check-circle");
    tip = c[4].querySelector(".dm-uptodate").title;
    assert(tip.startsWith("latest zeigt auf keinen Release-Tag, sondern auf einen Build, den Docker Hub am 13.10.2026 bekam, " +
                          "nach dem neuesten Release 5.1.0 (21.09.2026)."), tip);
    assert(tip.endsWith("\nDer Container läuft auf diesem Build: Mit diesem Tag gibt es nichts Neueres."), tip);
    // the tag moved on to another build: the note stays, the status is unknown
    pf.tag_now.digest = "sha256:" + "9".repeat(64);
    const m = boot("de", { ...FILES, "/var/lib/diun-radar/versions.json": dev });
    await sleep(60);
    c = cells(row(m, "smtp-relay"));
    assert.deepStrictEqual(c.slice(4, 6).map((x) => x.textContent.trim()), ["Build nach 5.1.0", "Unbekannt"]);
    assert(c[4].querySelector(".dm-muted").title.endsWith("\nDer Container läuft auf einem anderen Build von latest."));
    // a newer release than the one the radar noted: the newest release again
    pf.newest = "5.2.0";
    const r2 = boot("de", { ...FILES, "/var/lib/diun-radar/versions.json": dev });
    await sleep(60);
    assert.deepStrictEqual(cells(row(r2, "smtp-relay")).slice(4, 6).map((x) => x.textContent.trim()), ["neueste: 5.2.0", "Unbekannt"]);
    pf.newest = "5.1.0";
    pf.tag_now.digest = "sha256:aafc772384232497bed875e1eb66b4d3e54ba1ebc86e2e185a6dc1dbc48182ef";
    const be = boot("en", { ...FILES, "/var/lib/diun-radar/versions.json": dev });
    await sleep(60);
    c = cells(row(be, "smtp-relay"));
    assert.deepStrictEqual(c.slice(4, 6).map((x) => x.textContent.trim()), ["build after 5.1.0", "Up to date"]);
    tip = c[4].querySelector(".dm-uptodate").title;
    // (ICU versions abbreviate September as "Sep" or "Sept")
    assert(/^latest points to no release tag but to a build Docker Hub got on 13 Oct 2026, after the newest release 5\.1\.0 \(21 Sept? 2026\)\./.test(tip), tip);

    // An image built on the server (a test host, 2026-10-18: caddy-caddy is
    // xcaddy on caddy:2-builder, copied onto caddy:2): no registry shows it,
    // the radar checks the bases of its Dockerfile instead.
    const bases = (newer, extra) => {
        const r = JSON.parse(JSON.stringify(radar));
        const pm = r.images.find((x) => x.image === "ghcr.io/cbcoutinho/nextcloud-mcp-server");
        ["versions", "by_tag", "newest", "tag_now"].forEach((k) => delete pm[k]);
        Object.assign(pm, { status: "unlisted", error: "requested access to the resource is denied", built: "2026-06-15T18:35:36Z",
            base: [{ image: "caddy", tag: "2-builder", final: false, now: "2.11.7", pushed: "2026-10-14T00:25:00Z", newer },
                   { image: "caddy", tag: "2", final: true, version: "2.11.4", now: "2.11.7", pushed: "2026-10-14T00:20:11Z", newer, ...extra }] });
        return { ...FILES, "/var/lib/diun-radar/versions.json": r };
    };
    let bb = boot("de", bases(true));
    await sleep(60);
    c = cells(row(bb, "prod-mcp-1"));
    console.log("built here, newer base:", c.slice(3, 6).map((x) => x.textContent.trim()).join(" | "));
    assert.deepStrictEqual(c.slice(3, 6).map((x) => x.textContent.trim()), ["?", "Basis neuer: caddy:2 = 2.11.7", "Neue Version"]);
    tip = c[4].querySelector(".dm-level").title.split("\n");
    console.log("  " + tip.join(" / "));
    assert.deepStrictEqual(tip, [
        "Auf diesem Server gebaut am 15.06.2026. Keine Registry zeigt das Image selbst, wohl aber die Images, auf denen sein Dockerfile aufbaut:",
        "caddy:2-builder (Build-Stufe): jetzt 2.11.7, veröffentlicht am 14.10.2026 – neuer als der Build",
        "caddy:2 (Laufzeit): jetzt 2.11.7, veröffentlicht am 14.10.2026; der Build enthält 2.11.4 – neuer als der Build",
        "Neu bauen bringt es: docker compose build --pull prod-mcp-1, dann docker compose up -d prod-mcp-1, in /opt/prod."]);
    assert.strictEqual(iconOf(c[4]), "info-circle");
    // every base older than the build: up to date, though the version is unknown
    bb = boot("de", bases(false));
    await sleep(60);
    c = cells(row(bb, "prod-mcp-1"));
    assert.deepStrictEqual(c.slice(4, 6).map((x) => x.textContent.trim()), ["Basis aktuell", "Aktuell"]);
    assert.strictEqual(iconOf(c[4]), "check-circle");
    assert(c[4].querySelector(".dm-uptodate").title.endsWith("Eigene Änderungen im Projekt prüft der Radar nicht."));
    // the registry did not answer for any base: nothing claimed
    const errs = bases(false);
    errs["/var/lib/diun-radar/versions.json"].images.find((x) => x.image === "ghcr.io/cbcoutinho/nextcloud-mcp-server").base
        .forEach((x) => { Object.keys(x).filter((k) => !["image", "tag", "final"].includes(k)).forEach((k) => delete x[k]); x.error = "registry down"; });
    bb = boot("de", errs);
    await sleep(60);
    assert.deepStrictEqual(cells(row(bb, "prod-mcp-1")).slice(4, 6).map((x) => x.textContent.trim()), ["Basis nicht geprüft", "Unbekannt"]);
    bb = boot("en", bases(true, { pinned: true, newer: false }));
    await sleep(60);
    c = cells(row(bb, "prod-mcp-1"));
    assert.deepStrictEqual(c.slice(4, 6).map((x) => x.textContent.trim()), ["base newer: caddy:2-builder = 2.11.7", "New version"],
                           "a pinned base never counts; the build stage does");
    assert(c[4].querySelector(".dm-level").title.includes("caddy:2: pinned by digest, does not move"));

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

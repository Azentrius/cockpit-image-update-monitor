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
// The page as index.html loads it: the texts (i18n.js), then the code (diun.js).
const js = ["i18n.js", "diun.js"].map((f) => fs.readFileSync(path.join(PLUGIN, f), "utf8")).join("\n");
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
// The stubbed helpers answer at once and the page has no timers of its own: one turn of the
// event loop runs everything that follows from the answers, without waiting for milliseconds.
const flush = () => new Promise((r) => setImmediate(r));
const row = (d, name) => [...d.querySelectorAll("#dm-rows tr")].find((tr) => tr.querySelector(".dm-name").textContent === name);
const cells = (tr) => [...tr.querySelectorAll("td")];
const levels = (td) => [...td.querySelectorAll(".dm-level")];
const tipOf = (lv) => (lv.closest("button.dm-level-btn") || lv).title;
const iconOf = (el) => { const s = el.querySelector("svg.az-icon"); return s ? s.getAttribute("data-icon") : null; };

(async () => {
    const d = boot("de", FILES);
    await flush();
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
        await flush();
        const cell = cells(row(r, "formbricks-app"))[3];
        console.log("revision " + lang + ":", cell.textContent.trim(), "|", cell.querySelector(".dm-ver").title.replace(/\n/g, " / "));
        assert.strictEqual(cell.textContent.trim(), "latest @35b2a97");
        const tip = cell.querySelector(".dm-ver").title.split("\n");
        assert.strictEqual(tip[0], start);
        assert(tip[1].startsWith(lang === "de" ? "Gebaut am " : "Built on "), tip[1]);
        assert.strictEqual(cells(row(r, "formbricks-app"))[5].textContent.trim(), lang === "de" ? "Unbekannt" : "Unknown");
        assert.strictEqual(cells(row(r, "ollama"))[3].textContent.trim(), "0.30.11", "a known version wins");
    }

    // A line tag (cubejs/cube:v1.6 on a test host, 2026-10-18) names only its
    // line: the inventory reads 1.6 from it, the radar names the release by
    // digest. The radar's version wins when it refines the tag's number; an
    // older radar that read 1.6 from the tag as well leaves it at 1.6.
    const lineInv = inventory.trim().split("\n").map((l) => JSON.parse(l)).map((x) =>
        (x.name === "formbricks-cube" ? { ...x, tag: "v1.6", version: "1.6", version_src: "tag" } : x))
        .map((x) => JSON.stringify(x)).join("\n") + "\n";
    const lineRadar = (inst, versions, extra) => {
        const r2 = JSON.parse(JSON.stringify(radar));
        const cube = r2.images.find((x) => x.image === "cubejs/cube");
        Object.assign(cube, { tag: "v1.6", reach: ["patch"], installed: { "sha256:formbricks-cube": inst }, versions,
                              by_tag: { patch: null, minor: "v1.8", major: null, status: "minor" }, status: "patch" }, extra);
        return { ...FILES, "/var/lib/diun-radar/versions.json": r2 };
    };
    const lr = boot("de", lineRadar({ version: "1.6.74", src: "digest", tag: "v1.6.74" },
        { "1.6.74": { patch: "v1.6.75", minor: "v1.8.8", major: null, status: "patch", as_tag: "v1.6.74" } },
        { tag_now: { digest: "sha256:now-v1.6", version: "1.6.75", tag: "v1.6.75" } }), lineInv);
    await flush();
    c = cells(row(lr, "formbricks-cube"));
    console.log("line tag:", c.slice(2, 6).map((x) => x.textContent.trim()).join(" | "));
    assert.deepStrictEqual(c.slice(2, 4).map((x) => x.textContent.trim()), ["v1.6 = 1.6.75", "1.6.74"]);
    assert(c[3].querySelector(".dm-ver").title.includes("Registry-Tag v1.6.74"), "the source is the digest");
    assert.deepStrictEqual(levels(c[4]).map((x) => `${x.dataset.level}:${x.dataset.reach}:${x.textContent.trim()}`),
                           ["patch:update:Patch v1.6.75", "minor:tag:Minor v1.8.8"]);
    const lo = boot("de", lineRadar({ version: "1.6", src: "tag" },
        { "1.6": { patch: null, minor: "v1.8", major: null, status: "minor", as_tag: "v1.6" } }), lineInv);
    await flush();
    assert.strictEqual(cells(row(lo, "formbricks-cube"))[3].textContent.trim(), "1.6", "an older radar: the tag's number");
    // The registry failed in the last run: the radar kept the release and its
    // patch from the run before, marked stale. The row still offers the patch
    // and is not "up to date".
    const ls = boot("de", lineRadar({ version: "1.6.74", src: "digest", tag: "v1.6.74" },
        { "1.6.74": { patch: "v1.6.75", minor: "v1.8.8", major: null, status: "minor", as_tag: "v1.6.74" } },
        { stale: true, error: "registry down", status: "minor",
          by_tag: { patch: null, minor: null, major: null, status: "current" } }), lineInv);
    await flush();
    c = cells(row(ls, "formbricks-cube"));
    console.log("line tag, registry down:", c.slice(3, 6).map((x) => x.textContent.trim()).join(" | "));
    assert.strictEqual(c[3].textContent.trim(), "1.6.74");
    assert.deepStrictEqual(levels(c[4]).map((x) => `${x.dataset.level}:${x.textContent.trim()}`),
                           ["patch:Patch v1.6.75", "minor:Minor v1.8.8"]);
    assert(c[4].textContent.includes("(Stand älter)"), "stale data is marked");
    assert.notStrictEqual(c[5].textContent.trim(), "Aktuell", "a known patch is never 'up to date'");

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
    await flush();
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
    await flush();
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
    await flush();
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
    await flush();
    c = cells(row(m, "smtp-relay"));
    assert.deepStrictEqual(c.slice(4, 6).map((x) => x.textContent.trim()), ["Build nach 5.1.0", "Unbekannt"]);
    assert(c[4].querySelector(".dm-muted").title.endsWith("\nDer Container läuft auf einem anderen Build von latest."));
    // a newer release than the one the radar noted: the newest release again
    pf.newest = "5.2.0";
    const r2 = boot("de", { ...FILES, "/var/lib/diun-radar/versions.json": dev });
    await flush();
    assert.deepStrictEqual(cells(row(r2, "smtp-relay")).slice(4, 6).map((x) => x.textContent.trim()), ["neueste: 5.2.0", "Unbekannt"]);
    pf.newest = "5.1.0";
    pf.tag_now.digest = "sha256:aafc772384232497bed875e1eb66b4d3e54ba1ebc86e2e185a6dc1dbc48182ef";
    const be = boot("en", { ...FILES, "/var/lib/diun-radar/versions.json": dev });
    await flush();
    c = cells(row(be, "smtp-relay"));
    assert.deepStrictEqual(c.slice(4, 6).map((x) => x.textContent.trim()), ["build after 5.1.0", "Up to date"]);
    tip = c[4].querySelector(".dm-uptodate").title;
    // (ICU versions abbreviate September as "Sep" or "Sept")
    assert(/^latest points to no release tag but to a build Docker Hub got on 13 Oct 2026, after the newest release 5\.1\.0 \(21 Sept? 2026\)\./.test(tip), tip);

    // A registry without any release tag and without push dates (an own image
    // on ghcr.io built from main, Sharewerk 2026-10-19): the container runs the
    // build latest points to, so it is up to date, not unknown, with the date
    // the image was built (smtp-relay: 2026-10-01; the page's own time zone).
    const builtDe = new Date("2026-10-01T00:00:00Z").toLocaleDateString("de-DE", { dateStyle: "medium" });
    const builtEn = new Date("2026-10-01T00:00:00Z").toLocaleDateString("en-GB", { dateStyle: "medium" });
    const keep = { newest: pf.newest, tag_now: JSON.parse(JSON.stringify(pf.tag_now)) };
    delete pf.newest;
    delete pf.tag_now.after;
    delete pf.tag_now.pushed;
    const g = boot("de", { ...FILES, "/var/lib/diun-radar/versions.json": dev });
    await flush();
    c = cells(row(g, "smtp-relay"));
    console.log("smtp-relay, no release tag at all:", c.slice(2, 6).map((x) => x.textContent.trim()).join(" | "));
    assert.deepStrictEqual(c.slice(2, 6).map((x) => x.textContent.trim()), ["latest", "?", "neuester Build vom " + builtDe, "Aktuell"]);
    assert.strictEqual(iconOf(c[4]), "check-circle");
    tip = c[4].querySelector(".dm-uptodate").title;
    assert.strictEqual(tip, "Die Registry hat für dieses Image keinen Release-Tag; latest zeigt auf einen Build ohne Versionsnummer." +
                            "\nDer Container läuft auf diesem Build: Mit diesem Tag gibt es nichts Neueres.");
    const ge = boot("en", { ...FILES, "/var/lib/diun-radar/versions.json": dev });
    await flush();
    assert.deepStrictEqual(cells(row(ge, "smtp-relay")).slice(4, 6).map((x) => x.textContent.trim()), ["newest build from " + builtEn, "Up to date"]);
    // a reproducible build (created 1970) shows no date
    const inv1970 = inventory.split("\n").map((l) => l.includes('"name": "smtp-relay"')
        ? l.replace('"created": "2026-10-01T00:00:00Z"', '"created": "1970-01-01T00:00:00Z"') : l).join("\n");
    assert.notStrictEqual(inv1970, inventory);
    const g70 = boot("de", { ...FILES, "/var/lib/diun-radar/versions.json": dev }, inv1970);
    await flush();
    assert.deepStrictEqual(cells(row(g70, "smtp-relay")).slice(4, 6).map((x) => x.textContent.trim()), ["neuester Build", "Aktuell"]);
    // latest moved on: another build, muted, and the status stays unknown
    pf.tag_now.digest = "sha256:" + "9".repeat(64);
    const go = boot("de", { ...FILES, "/var/lib/diun-radar/versions.json": dev });
    await flush();
    c = cells(row(go, "smtp-relay"));
    assert.deepStrictEqual(c.slice(4, 6).map((x) => x.textContent.trim()), ["anderer Build", "Unbekannt"]);
    assert(c[4].querySelector(".dm-muted").title.endsWith("\nDer Container läuft auf einem anderen Build von latest."));
    // with release tags in the registry nothing tells whether latest is newer: "neueste: X", unknown
    pf.tag_now.digest = keep.tag_now.digest;
    pf.newest = "5.1.0";
    const gr = boot("de", { ...FILES, "/var/lib/diun-radar/versions.json": dev });
    await flush();
    assert.deepStrictEqual(cells(row(gr, "smtp-relay")).slice(4, 6).map((x) => x.textContent.trim()), ["neueste: 5.1.0", "Unbekannt"]);
    pf.newest = keep.newest;
    pf.tag_now = keep.tag_now;

    // The registry failed in the last run: keep_last_good kept tag_now and the
    // rest of the record from the run before and marked it stale, with the
    // error and the time of the last good check. The page shows the last known
    // build, marked as older data with the error, and claims no fresh check.
    const keepChecked = pf.checked;
    const keepNow = JSON.parse(JSON.stringify(pf.tag_now));      // a copy: the steps below change pf.tag_now
    assert(!("stale" in pf) && !("error" in pf), "the fixture record starts fresh");
    const staleRec = (r) => Object.assign(r, { stale: true, error: "registry down: connection refused",
                                               checked: "2026-10-11T08:00:00Z" });
    const staleMark = (td) => td.querySelector(".dm-stale");
    const lastGood = new Date("2026-10-11T08:00:00Z").toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" });
    delete pf.newest;
    delete pf.tag_now.after;
    delete pf.tag_now.pushed;
    staleRec(pf);
    const sd = boot("de", { ...FILES, "/var/lib/diun-radar/versions.json": dev });
    await flush();
    c = cells(row(sd, "smtp-relay"));
    console.log("smtp-relay, no release tag, registry down:", c.slice(4, 6).map((x) => x.textContent.trim()).join(" | "));
    assert.deepStrictEqual(c.slice(4, 6).map((x) => x.textContent.trim()),
                           ["neuester Build vom " + builtDe + " (Stand älter)", "Aktuell"]);
    tip = c[4].querySelector(".dm-uptodate").title;
    assert(!tip.includes("nichts Neueres"), "no claim of a fresh check: " + tip);
    assert(tip.includes("Bei der letzten erfolgreichen Abfrage lief der Container auf diesem Build"), tip);
    assert.strictEqual(staleMark(c[4]).title, "Beim letzten Lauf erreichte der Radar die Registry nicht: " +
                       "registry down: connection refused. Angezeigt ist der Stand vom " + lastGood + ".");
    const se = boot("en", { ...FILES, "/var/lib/diun-radar/versions.json": dev });
    await flush();
    c = cells(row(se, "smtp-relay"));
    assert.deepStrictEqual(c.slice(4, 6).map((x) => x.textContent.trim()),
                           ["newest build from " + builtEn + " (older data)", "Up to date"]);
    assert(!c[4].querySelector(".dm-uptodate").title.includes("nothing newer"), "no claim of a fresh check (en)");
    assert(staleMark(c[4]).title.startsWith("The radar's last run could not reach the registry: registry down: connection refused."));
    // stale and another build: muted, marked, unknown
    pf.tag_now.digest = "sha256:" + "9".repeat(64);
    const so = boot("de", { ...FILES, "/var/lib/diun-radar/versions.json": dev });
    await flush();
    c = cells(row(so, "smtp-relay"));
    assert.deepStrictEqual(c.slice(4, 6).map((x) => x.textContent.trim()), ["anderer Build (Stand älter)", "Unbekannt"]);
    assert(staleMark(c[4]), "marked stale");
    // the next good run: the mark is gone
    pf.tag_now.digest = keepNow.digest;
    delete pf.stale;
    delete pf.error;
    const sb = boot("de", { ...FILES, "/var/lib/diun-radar/versions.json": dev });
    await flush();
    c = cells(row(sb, "smtp-relay"));
    assert.deepStrictEqual(c.slice(4, 6).map((x) => x.textContent.trim()), ["neuester Build vom " + builtDe, "Aktuell"]);
    assert.strictEqual(staleMark(c[4]), null, "no stale mark after a good run");
    // the build after a release (Docker Hub), registry down: marked the same way
    pf.newest = keep.newest;
    pf.tag_now = JSON.parse(JSON.stringify(keepNow));
    staleRec(pf);
    const sa = boot("de", { ...FILES, "/var/lib/diun-radar/versions.json": dev });
    await flush();
    c = cells(row(sa, "smtp-relay"));
    assert.deepStrictEqual(c.slice(4, 6).map((x) => x.textContent.trim()), ["Build nach 5.1.0 (Stand älter)", "Aktuell"]);
    tip = c[4].querySelector(".dm-uptodate").title;
    assert(!tip.includes("nichts Neueres") && tip.includes("Bei der letzten erfolgreichen Abfrage"), tip);
    assert(staleMark(c[4]).title.includes("registry down: connection refused"));
    pf.tag_now.digest = "sha256:" + "9".repeat(64);
    const sao = boot("de", { ...FILES, "/var/lib/diun-radar/versions.json": dev });
    await flush();
    c = cells(row(sao, "smtp-relay"));
    assert.deepStrictEqual(c.slice(4, 6).map((x) => x.textContent.trim()), ["Build nach 5.1.0 (Stand älter)", "Unbekannt"]);
    delete pf.stale;
    delete pf.error;
    if (keepChecked === undefined) delete pf.checked; else pf.checked = keepChecked;
    pf.tag_now = keepNow;

    // A radar run that reached no registry, not even on its second try
    // (versions.json "no_registry"): the meta line says so, its tooltip names
    // the error and points to DNS and the network.
    const nrDoc = JSON.parse(JSON.stringify(radar));
    nrDoc.no_registry = { error: "lookup ghcr.io on 127.0.0.53:53: server misbehaving", images: 21 };
    for (const [lang, word, dns] of [["de", "(keine Registry erreicht)", "DNS"], ["en", "(no registry reached)", "DNS"]]) {
        const nd = boot(lang, { ...FILES, "/var/lib/diun-radar/versions.json": nrDoc });
        await flush();
        const meta = nd.getElementById("dm-meta");
        console.log("no registry reached:", meta.textContent);
        assert(meta.textContent.endsWith(word), meta.textContent);
        assert(meta.title.includes("server misbehaving") && meta.title.includes(dns), meta.title);
    }
    assert(!d.getElementById("dm-meta").textContent.includes("Registry"), "a good run: no word about registries");
    assert.strictEqual(d.getElementById("dm-meta").title, "", "a good run: no tooltip");

    // Nothing newer for over a year (ronmi/forgejo-mcp: latest built 2025-10-28): the newest
    // there is, but old; the project may be abandoned. A hint, the status stays up to date.
    // Not for a build less than a year old, and not where something newer is known (also a
    // database major the update does not bring).
    const aged = (names, created) => inventory.split("\n").map((l) => names.some((n) => l.includes(`"name": "${n}"`))
        ? l.replace(/"created": "[^"]*"/, `"created": "${created}"`) : l).join("\n");
    const oldBuilt = new Date("2025-09-01T00:00:00Z").toLocaleDateString("de-DE", { dateStyle: "medium" });
    const ao = boot("de", FILES, aged(["smtp-relay", "calcom", "formbricks-db"], "2025-09-01T00:00:00Z"));
    await flush();
    for (const name of ["smtp-relay", "calcom"]) {
        c = cells(row(ao, name));
        const hint = c[4].querySelector(".dm-age");
        console.log(name + ", built 2025-09-01:", c[4].textContent.trim(), "|", c[5].textContent.trim());
        assert(hint && hint.textContent === "seit über einem Jahr nichts Neues", name + ": " + c[4].textContent);
        assert(hint.title.includes(oldBuilt) && hint.title.includes("verwaist"), hint.title);
        assert.strictEqual(c[5].textContent.trim(), "Aktuell");
    }
    assert.strictEqual(cells(row(ao, "formbricks-db"))[4].querySelector(".dm-age"), null, "a database major is known: no hint");
    assert.strictEqual(cells(row(d, "smtp-relay"))[4].querySelector(".dm-age"), null, "built 2026-10-01: no hint");
    const aoe = boot("en", FILES, aged(["calcom"], "2025-09-01T00:00:00Z"));
    await flush();
    assert.strictEqual(cells(row(aoe, "calcom"))[4].querySelector(".dm-age").textContent, "nothing new for over a year");
    // the newest build of a registry without release tags, built over a year ago
    const keepNow3 = JSON.parse(JSON.stringify(pf.tag_now));
    const keepNewest3 = pf.newest;
    delete pf.newest;
    delete pf.tag_now.after;
    delete pf.tag_now.pushed;
    const anb = boot("de", { ...FILES, "/var/lib/diun-radar/versions.json": dev }, aged(["smtp-relay"], "2025-09-01T00:00:00Z"));
    await flush();
    c = cells(row(anb, "smtp-relay"));
    assert(c[4].textContent.startsWith("neuester Build vom " + oldBuilt), c[4].textContent);
    assert(c[4].querySelector(".dm-age"), "the newest build, over a year old: the hint");
    pf.newest = keepNewest3;
    pf.tag_now = keepNow3;

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
    await flush();
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
    await flush();
    c = cells(row(bb, "prod-mcp-1"));
    assert.deepStrictEqual(c.slice(4, 6).map((x) => x.textContent.trim()), ["Basis aktuell", "Aktuell"]);
    assert.strictEqual(iconOf(c[4]), "check-circle");
    assert(c[4].querySelector(".dm-uptodate").title.endsWith("Eigene Änderungen im Projekt prüft der Radar nicht."));
    // the registry did not answer for any base: nothing claimed
    const errs = bases(false);
    errs["/var/lib/diun-radar/versions.json"].images.find((x) => x.image === "ghcr.io/cbcoutinho/nextcloud-mcp-server").base
        .forEach((x) => { Object.keys(x).filter((k) => !["image", "tag", "final"].includes(k)).forEach((k) => delete x[k]); x.error = "registry down"; });
    bb = boot("de", errs);
    await flush();
    assert.deepStrictEqual(cells(row(bb, "prod-mcp-1")).slice(4, 6).map((x) => x.textContent.trim()), ["Basis nicht geprüft", "Unbekannt"]);
    bb = boot("en", bases(true, { pinned: true, newer: false }));
    await flush();
    c = cells(row(bb, "prod-mcp-1"));
    assert.deepStrictEqual(c.slice(4, 6).map((x) => x.textContent.trim()), ["base newer: caddy:2-builder = 2.11.7", "New version"],
                           "a pinned base never counts; the build stage does");
    assert(c[4].querySelector(".dm-level").title.includes("caddy:2: pinned by digest, does not move"));

    // English
    const e = boot("en", FILES);
    await flush();
    const eh = [...e.querySelectorAll("#dm-table thead th")].map((t) => t.textContent).slice(2, 5);
    console.log("EN cols:", eh.join(" | "), "| ollama tip:", cells(row(e, "ollama"))[3].querySelector(".dm-ver").title.split("\n")[0]);
    assert.deepStrictEqual(eh.join("|"), "Configured|Installed|Newest version");
    assert(!/Installiert|neueste|Umrandet|liefert|Registry-Tag |zeigt gerade/.test(e.body.textContent + [...e.querySelectorAll("[title]")].map((x) => x.title).join(" ")));
    assert.strictEqual(cells(row(e, "ollama"))[2].querySelector(".dm-tagnow").title, "In the registry, latest points to 0.40.3 right now (checked by the radar).");

    // no radar file: legend hidden, installed from inventory still shown
    const n = boot("de", { ...FILES, "/var/lib/diun-radar/versions.json": null });
    await flush();
    assert(n.getElementById("dm-legend").hidden);
    assert.strictEqual(cells(row(n, "glitchtip-web"))[3].textContent.trim(), "6.1.8");
    assert.strictEqual(cells(row(n, "ollama"))[3].textContent.trim(), "?");

    console.log("\nALL CHECKS PASSED");
})().catch((e) => { console.error(e); process.exit(1); });

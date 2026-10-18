// SPDX-License-Identifier: Apache-2.0
// Which Diun event belongs to which row (2026-10-16): a row takes the newest
// event of its own image AND tag. An event for another tag of the same image
// (postgres:15 vs postgres:16) or for a reference with a digest (a CI job's
// postgres@sha256:…, a pinned postgres:16-alpine@sha256:…) marks no row; on
// a test host such an event had marked every postgres row "update available"
// right after their updates.
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
const radar = JSON.parse(fs.readFileSync(path.join(FIX, "versions2.json"), "utf8"));
const running = Object.fromEntries(inventory.trim().split("\n").map((l) => JSON.parse(l)).map((c) => [c.name, c.digest]));

const NEW = "sha256:" + "1".repeat(64);
const event = (ts, image, digest, extra) => JSON.stringify({ ts, status: "update", hostname: "docker-host",
    provider: "docker", image, digest, created: "", platform: "linux/amd64", ...extra });
const PG_LINK = "https://hub.docker.com/_/postgres";
const EVENTS = [
    // postgres:15-alpine has a new build: only calcom-db
    event("2026-10-16T00:00:00Z", "docker.io/library/postgres:15-alpine", NEW, { hub_link: PG_LINK }),
    // newer events for references with a digest: no row
    event("2026-10-16T04:00:20Z", "docker.io/library/postgres@sha256:" + "8".repeat(64), "sha256:" + "7".repeat(64)),
    event("2026-10-16T05:00:00Z", "docker.io/library/postgres:16-alpine@sha256:" + "4".repeat(64), "sha256:" + "5".repeat(64)),
    // nextcloud:apache runs the build Diun saw; a newer event for :fpm is another tag
    event("2026-10-15T22:00:00Z", "docker.io/library/nextcloud:apache", running["nextcloud-family-app-1"]),
    event("2026-10-16T06:00:00Z", "docker.io/library/nextcloud:fpm", NEW),
].join("\n") + "\n";

function boot(language, files, inventoryOut) {
    const dom = new JSDOM(html, { runScripts: "outside-only" });
    const w = dom.window;
    w.document.querySelectorAll("script").forEach((s) => s.remove());   // the page's own tags never run
    w.Date.now = () => Date.parse("2026-10-16T12:00:00Z");
    w.cockpit = {
        language,
        file(p) { return { read: () => Promise.resolve(files[p] ?? null), watch: () => {}, replace: () => Promise.resolve() }; },
        spawn(cmd) {
            const p = cmd[0].endsWith("diun-inventory.py") ? inventoryOut()
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
const FILES = { "/var/lib/diun-radar/versions.json": radar, "/var/lib/diun/events.jsonl": EVENTS,
                "/var/lib/diun/dismissed.json": null };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const rows = (d) => [...d.querySelectorAll("#dm-rows tr")];
const row = (d, name) => rows(d).find((tr) => tr.querySelector(".dm-name").textContent === name);
const status = (d, name) => row(d, name).querySelectorAll("td")[5].textContent.trim();
const updateButton = (d, name) => d.querySelector(`[data-update="${name}"][data-kind="update"]`);

(async () => {
    const d = boot("en", FILES, () => Promise.resolve(inventory));
    await sleep(60);

    // the event of its own tag: update, and "Dismiss" acknowledges that event
    console.log("calcom-db:", status(d, "calcom-db"));
    assert.strictEqual(status(d, "calcom-db"), "Update available");
    assert(updateButton(d, "calcom-db"), "Update button");
    assert.strictEqual(row(d, "calcom-db").querySelector("[data-key]").dataset.key,
                       "docker-host|docker.io/library/postgres:15-alpine|" + NEW);

    // newer events of the same image, but another tag or a digest: no update
    for (const name of ["nextcloud-family-db-1", "nextcloud-family-app-1", "nextcloud-family-cron-1"]) {
        console.log(name + ":", status(d, name));
        assert.notStrictEqual(status(d, name), "Update available", name);
        assert(!updateButton(d, name), name + ": no Update button");
        assert(!row(d, name).querySelector("[data-key]"), name + ": nothing to dismiss");
    }
    const updates = rows(d).filter((tr) => tr.querySelectorAll("td")[5].textContent.trim() === "Update available")
        .map((tr) => tr.querySelector(".dm-name").textContent);
    console.log("rows with an update:", updates.join(", "));
    assert.deepStrictEqual(updates, ["calcom-db"]);

    // the registry link is the image's, from any of its events
    const link = (name) => (row(d, name).querySelector("td.dm-img a") || {}).href;
    assert.strictEqual(link("nextcloud-family-db-1"), PG_LINK, "link from the event of another tag");
    assert.strictEqual(link("calcom-db"), PG_LINK);
    assert.strictEqual(link("ollama"), undefined, "no event, no link");

    // a dismissed event: no update any more
    const dis = { ["docker-host|docker.io/library/postgres:15-alpine|" + NEW]: true };
    const g = boot("en", { ...FILES, "/var/lib/diun/dismissed.json": dis }, () => Promise.resolve(inventory));
    await sleep(60);
    assert.notStrictEqual(status(g, "calcom-db"), "Update available");

    // A new digest of the tag, but the build that runs: Docker Hub published
    // the list of platforms of nextcloud:apache again when it added riscv64
    // (a test host, 2026-10-17). Diun's "created" says which build it is.
    const BUILT = "2026-10-15T19:00:50.089352556Z";
    const withCreated = (created) => inventory.trim().split("\n").map((l) => JSON.parse(l))
        .map((c) => (c.name === "calcom-db" ? { ...c, created } : c)).map((c) => JSON.stringify(c)).join("\n") + "\n";
    for (const [what, evCreated, rowCreated, want] of [
        ["same build, a new list of platforms", "2026-10-15 19:00:50.089352556 +0000 UTC", BUILT, false],
        ["same build, another time zone", "2026-10-15 21:00:50 +0200 CEST", BUILT, false],
        ["a newer build", "2026-10-17 01:36:06.1 +0000 UTC", BUILT, true],
        ["Diun names no build time", "", BUILT, true],
        ["the image names no build time", "2026-10-15 19:00:50 +0000 UTC", "", true],
        ["a fixed date proves nothing (reproducible builds)", "1970-01-01 00:00:00 +0000 UTC", "1970-01-01T00:00:00Z", true]]) {
        const evs = event("2026-10-16T00:00:00Z", "docker.io/library/postgres:15-alpine", NEW, { created: evCreated }) + "\n";
        const x = boot("en", { ...FILES, "/var/lib/diun/events.jsonl": evs }, () => Promise.resolve(withCreated(rowCreated)));
        await sleep(60);
        console.log(what + ":", status(x, "calcom-db"));
        assert.strictEqual(status(x, "calcom-db") === "Update available", want, what);
    }

    // without an inventory the rows come from the events: one per image and
    // tag, none for a reference with a digest
    const f = boot("en", FILES, () => Promise.reject(Object.assign(new Error("denied"), { problem: "access-denied" })));
    await sleep(60);
    const shown = rows(f).map((tr) => tr.querySelectorAll("td")[1].textContent.trim() + ":" +
                                       tr.querySelector(".dm-cfg").textContent).sort();
    console.log("events only:", shown.join(", "));
    assert.deepStrictEqual(shown, ["docker.io/library/nextcloud:apache", "docker.io/library/nextcloud:fpm",
                                   "docker.io/library/postgres:15-alpine"]);

    console.log("\nALL CHECKS PASSED");
})().catch((e) => { console.error(e); process.exit(1); });

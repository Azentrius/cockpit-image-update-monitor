// SPDX-License-Identifier: Apache-2.0
// The database major in the page (stage 5): PostgreSQL majors with a known
// target can be chosen in "Change version", planned by `dbmajor … --dry-run`
// (a refusal says why and what to change), shown with where the data lies and
// what stops meanwhile, without the dump tick box, run only after the tick
// boxes as `dbmajor … --expect <fingerprint>`, with its result; the kept data
// released from the history. Against the real scripts in jsdom.
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { JSDOM } = require("jsdom");
const js = require("./page-scripts").pageScripts();

const PLUGIN = path.join(__dirname, "..", "plugin");
const FIX = path.join(__dirname, "fixtures");
const html = fs.readFileSync(path.join(PLUGIN, "index.html"), "utf8");
const icons = fs.readFileSync(path.join(PLUGIN, "icons.js"), "utf8");
const FILES = {
    "/var/lib/diun/events.jsonl": fs.readFileSync(path.join(FIX, "events.jsonl"), "utf8"),
    "/var/lib/diun-radar/versions.json": JSON.parse(fs.readFileSync(path.join(FIX, "versions2.json"), "utf8")),
    "/var/lib/diun/dismissed.json": null,
};
const inventory = fs.readFileSync(path.join(FIX, "inventory3.jsonl"), "utf8");
const DEPS_OK = JSON.stringify({ root: true, docker: { state: "ok", compose: true },
    diun: { state: "running", events: true }, skopeo: { state: "ok" }, radar: { state: "active" } });
const KEPT = { where: "Volume glitchtip_pgdata-pg16-20261021-010000", bytes: 120000000, major: 16, can_release: true };
const OP = { id: "20261021-010000-glitchtip-postgres", ts: "2026-10-20T23:00:00Z", kind: "dbmajor", container: "glitchtip-postgres",
    image: "postgres:16-alpine", services: ["postgres"], from_major: 16, to_major: 17, from_version: "16.14", to_version: "17.11",
    rollback: "postgres:rollback-20261021-010000", dumps: [], status: "ok", can_rollback: true, kept: KEPT };
const BACKUPS = { dir: "/var/backups/image-updates", free_bytes: 40 * 2 ** 30, dump_bytes: 0, dumps: [], images: [], ops: [OP] };
const plan = (tag, major) => ({ container: "glitchtip-postgres", image: "postgres:16-alpine", updatable: true, project: "glitchtip",
    workdir: "/opt/glitchtip", services: ["postgres"], containers: ["glitchtip-postgres"], pulled: false, config_changed: false,
    dbs: [{ name: "glitchtip-postgres", kind: "postgres", running: true, size: 120000000 }], free_bytes: 40 * 2 ** 30,
    installed_version: "16.14", tag_digits: 1, jump_unknown: false, target_version: tag.replace("-alpine", ""), jump: "major",
    depends_on: [], dependents: [{ service: "web", containers: ["glitchtip-web"] }], jobs: [], job_order: [], shared_networks: [],
    retag: { tag, from_tag: "16-alpine", image: "postgres:" + tag, choice: "newer", level: "major", possible: true,
             changes: [{ file: "/opt/glitchtip/docker-compose.yml", line: 5, old: "    image: postgres:16-alpine",
                         new: "    image: postgres:" + tag, service: "postgres" }], fingerprint: "a".repeat(64) },
    major });
const MAJOR17 = { possible: true, reason: null, detail: null, from_major: 16, to_major: 17,
    data: { type: "volume", name: "glitchtip_pgdata", driver: "local", source: "/var/lib/docker/volumes/glitchtip_pgdata/_data",
            destination: "/var/lib/postgresql/data" }, pgdata: "/var/lib/postgresql/data", stop: ["web", "worker"],
    extensions: ["pg_trgm"], data_bytes: 120000000, data_free: 40 * 2 ** 30, fingerprint: "b".repeat(64) };
const MAJOR18 = { possible: false, reason: "major-pgdata", detail: "/var/lib/postgresql/18/docker | /var/lib/postgresql/data" };

const pending = new Set();
async function settle() {
    for (let round = 0; round < 500; round++) {
        if (pending.size) await Promise.all([...pending]);
        await new Promise((r) => setImmediate(r));
        if (!pending.size) return;
    }
    throw new Error("the page did not settle");
}
function boot(language, script) {
    const dom = new JSDOM(html, { runScripts: "outside-only" });
    const w = dom.window;
    w.document.querySelectorAll("script").forEach((s) => s.remove());   // the page's own tags never run
    w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
    w.HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
    w.Date.now = () => Date.parse("2026-10-21T02:00:00Z");
    const calls = [];
    w.cockpit = {
        language,
        file(p) { return { read: () => Promise.resolve(FILES[p] ?? null), watch: (cb) => cb(FILES[p], "t"), replace: () => Promise.resolve() }; },
        spawn(cmd) {
            calls.push(JSON.parse(JSON.stringify(cmd)));
            const tool = cmd[0].split("/").pop();
            const s = script[(tool + " " + (cmd[1] || "")).trim()] || script[tool];
            if (!s) return Promise.reject(new Error("unscripted " + cmd.join(" ")));
            const r = typeof s === "function" ? s(cmd) : s;
            let cb = null;
            const p = new Promise((resolve) => setTimeout(() => { (r.chunks || []).forEach((c) => cb && cb(c)); resolve(r.out ?? ""); }, 5));
            p.stream = (f) => { cb = f; return p; };
            const done = p.then(() => {}, () => {});
            pending.add(done);
            done.then(() => pending.delete(done));
            return p;
        },
        transport: { wait() {} },
    };
    w.eval(icons);
    w.eval(js);
    return { w, d: w.document, $: (id) => w.document.getElementById(id), calls };
}
const BASE = {
    "diun-inventory.py": { out: inventory }, "diun-deps.sh check": { out: DEPS_OK },
    "diun-update.py backups": { out: JSON.stringify(BACKUPS) },
    "diun-update.py dbmajor": (cmd) => cmd[4] === "--dry-run"
        ? { out: JSON.stringify(plan(cmd[3], cmd[3].startsWith("18") ? MAJOR18 : MAJOR17)) }
        : { chunks: ["…\n", "@@RESULT " + JSON.stringify({ op: OP.id, status: "ok", rollback: OP.rollback, dumps: [],
                                                          from_version: "16.14", to_version: "17.11", image: "postgres:17.11-alpine",
                                                          old_data: KEPT.where }) + "\n"] },
    "diun-update.py delete-olddata": { chunks: ["Released.\n"] },
};
const rowOf = (d, name) => [...d.querySelectorAll("#dm-rows tr")].find((tr) => tr.querySelector(".dm-name").textContent === name);

(async () => {
    for (const [lang, title, button, ok] of [
        ["de", "Datenbank-Major: PostgreSQL 16 → 17", "Datenbank-Major ausführen",
         "Datenbank-Major erfolgreich: 16.14 → 17.11. Die alten Daten bleiben in Volume glitchtip_pgdata-pg16-20261021-010000, bis Sie sie im Reiter Backups freigeben."],
        ["en", "Database major: PostgreSQL 16 → 17", "Run database major",
         "Database major successful: 16.14 → 17.11. The old data is kept in Volume glitchtip_pgdata-pg16-20261021-010000 until you release it on the Backups tab."]]) {
        const x = boot(lang, BASE);
        await settle();
        rowOf(x.d, "glitchtip-postgres").querySelector("button[data-retag]").click();
        await settle();
        const sel = x.$("dm-retag-select");
        const majors = [...sel.querySelectorAll("option[data-major]")].map((o) => o.value);
        assert.deepStrictEqual(majors, ["18-alpine", "17-alpine", "18.6-alpine", "17.11-alpine"], "PostgreSQL majors with a target");
        assert(sel.querySelector('option[value="alpine"]').disabled, "a tag that follows every major stays locked");
        const choose = async (tag) => { sel.value = tag; sel.dispatchEvent(new x.w.Event("change")); await settle(); };
        // 18 without PGDATA: refused, the reason says what to change.
        await choose("18.6-alpine");
        const why = x.$("dm-retag-preview").textContent;
        console.log(`18 (${lang}):`, why);
        assert(/\/var\/lib\/postgresql\/18\/docker/.test(why) && /PGDATA: \/var\/lib\/postgresql\/data/.test(why), why);
        assert(x.$("dm-dialog-confirm").disabled);
        assert.deepStrictEqual(x.calls.at(-1).slice(1), ["dbmajor", "glitchtip-postgres", "18.6-alpine", "--dry-run"]);
        // 17: the plan of the major, no dump tick box, three tick boxes before the button.
        await choose("17.11-alpine");
        const pv = x.$("dm-retag-preview");
        assert.strictEqual(pv.querySelector(".az-alert__title").textContent, title);
        const kv = [...pv.querySelectorAll(".dm-kv")[0].querySelectorAll("li")].map((li) => li.textContent);
        console.log(`17 (${lang}):`, kv.join(" | "));
        assert(kv[0].includes("glitchtip_pgdata") && kv[2].includes("web") && kv[2].includes("worker") && kv[3].includes("pg_trgm"), kv.join(" | "));
        assert.strictEqual(pv.querySelector("#dm-dump-check"), null, "a major always dumps");
        const gates = [...pv.querySelectorAll(".dm-gate")];
        assert.deepStrictEqual(gates.map((g) => g.id).sort(), ["dm-major-check", "dm-mj-backup", "dm-mj-downtime"]);
        const confirm = x.$("dm-dialog-confirm");
        assert(confirm.disabled && confirm.textContent.trim() === button, confirm.textContent);
        for (const g of gates.slice(0, -1)) { g.checked = true; g.dispatchEvent(new x.w.Event("change")); }
        assert(confirm.disabled, "all tick boxes first");
        gates.at(-1).checked = true;
        gates.at(-1).dispatchEvent(new x.w.Event("change"));
        assert(!confirm.disabled);
        confirm.click();
        await settle();
        assert.deepStrictEqual(x.calls.find((c) => c[1] === "dbmajor" && c[4] === "--expect").slice(1),
                               ["dbmajor", "glitchtip-postgres", "17.11-alpine", "--expect", "b".repeat(64)]);
        assert.strictEqual(x.$("dm-dialog-result").textContent, ok);
        x.$("dm-dialog-cancel").click();
    }
    // Refusals of 0.25.0: roles with an MD5 password (the new image lets them in
    // with scram-sha-256 only), too little room for the trial run.
    for (const [lang, major, want] of [
        ["en", { possible: false, reason: "major-md5", detail: "legacy, oldapp" },
         "Roles log in with an MD5 password (legacy, oldapp); the image of the new major lets clients in over the network with scram-sha-256 only."],
        ["de", { possible: false, reason: "major-probe-space", detail: "100 MiB | 1.11 GiB | /var/lib/docker" },
         "Zu wenig Platz für den Probelauf in Dockers Verzeichnis /var/lib/docker: 100 MiB frei, 1.11 GiB nötig."]]) {
        const r = boot(lang, { ...BASE, "diun-update.py dbmajor": (cmd) => ({ out: JSON.stringify(plan(cmd[3], major)) }) });
        await settle();
        rowOf(r.d, "glitchtip-postgres").querySelector("button[data-retag]").click();
        await settle();
        const sel = r.$("dm-retag-select");
        sel.value = "17.11-alpine";
        sel.dispatchEvent(new r.w.Event("change"));
        await settle();
        const why = r.$("dm-retag-preview").textContent;
        console.log(`${major.reason} (${lang}):`, why);
        assert(why.includes(want), why);
        assert(r.$("dm-dialog-confirm").disabled);
    }
    // The history: the kept data released for good, after a confirmation.
    const b = boot("de", BASE);
    await settle();
    b.$("dm-tab-btn-backups").click();
    await settle();
    b.d.querySelector("#dm-ops button[data-release]").click();
    console.log("release:", b.$("dm-dialog-desc").textContent);
    assert(b.$("dm-dialog-desc").textContent.startsWith("Löscht die Daten von PostgreSQL 16 in Volume glitchtip_pgdata-pg16-20261021-010000"));
    b.$("dm-dialog-confirm").click();
    await settle();
    assert.deepStrictEqual(b.calls.find((c) => c[1] === "delete-olddata").slice(1), ["delete-olddata", OP.id]);
    assert.strictEqual(b.$("dm-dialog-result").textContent, "Freigegeben.");
    console.log("database major in the page: all checks passed");
})().catch((e) => { console.error(e); process.exit(1); });

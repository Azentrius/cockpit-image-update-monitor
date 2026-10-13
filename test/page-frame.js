// SPDX-License-Identifier: Apache-2.0
// The shared Azentrius page frame against the real diun.js and icons.js in
// jsdom: header (title, suffix, one filled "Refresh" with its icon and busy
// state), tabs (mouse and keyboard, nothing loads on a switch), status labels
// with their fixed icons, buttons per place, the dialog's buttons and the
// signature with version and host.
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { JSDOM } = require("jsdom");

const PLUGIN = path.join(__dirname, "..", "plugin");
const FIX = path.join(__dirname, "fixtures");
const html = fs.readFileSync(path.join(PLUGIN, "index.html"), "utf8");
const js = fs.readFileSync(path.join(PLUGIN, "diun.js"), "utf8");
const icons = fs.readFileSync(path.join(PLUGIN, "icons.js"), "utf8");
const VERSION = /const VERSION = "([^"]+)"/.exec(js)[1];
const inventory = fs.readFileSync(path.join(FIX, "inventory3.jsonl"), "utf8");
const preview = JSON.parse(fs.readFileSync(path.join(FIX, "preview.json"), "utf8"));
const files = {
    "/var/lib/diun/events.jsonl": fs.readFileSync(path.join(FIX, "events.jsonl"), "utf8"),
    "/var/lib/diun-radar/versions.json": JSON.parse(fs.readFileSync(path.join(FIX, "versions2.json"), "utf8")),
    "/var/lib/diun/dismissed.json": null,
    "/etc/hostname": "docker-host\n",
};
const DEPS = { root: true, docker: { state: "ok", compose: true }, diun: { state: "running", events: true },
               skopeo: { state: "missing", can_install: true }, radar: { state: "active", running: false },
               cleanup: { state: "active", can_install: true } };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function boot(language, opts) {
    opts = opts || {};
    const dom = new JSDOM(html, { runScripts: "outside-only" });
    const w = dom.window;
    w.document.querySelectorAll("script").forEach((s) => s.remove());   // the page's own tags never run
    w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
    w.HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
    w.Date.now = () => Date.parse("2026-10-12T02:50:00Z");
    const calls = [];
    let release = null;
    w.cockpit = {
        language,
        file(p) { return { read: () => Promise.resolve(p in (opts.files || files) ? (opts.files || files)[p] : null),
                           watch: (cb) => cb(files[p], "t"), replace: () => Promise.resolve() }; },
        spawn(cmd) {
            const verb = (cmd[0].split("/").pop() + " " + (cmd[1] || "")).trim();
            calls.push(verb);
            let p;
            if (verb === "diun-inventory.py" && opts.inventoryFails) {
                p = Promise.reject(new Error("not permitted"));
            } else if (verb === "diun-inventory.py") {
                p = opts.holdInventory && !release ? new Promise((r) => { release = () => r(inventory); }) : Promise.resolve(inventory);
            } else if (verb === "diun-deps.sh check") {
                p = Promise.resolve(JSON.stringify(DEPS));
            } else if (verb === "diun-update.py backups") {
                p = Promise.resolve(JSON.stringify(preview.backups));
            } else if (verb === "diun-update.py plan") {
                p = Promise.resolve(JSON.stringify(preview.plans["*"]));
            } else {
                p = Promise.reject(new Error("unscripted " + verb));
            }
            p.stream = () => p;
            return p;
        },
        transport: { wait() {} },
    };
    w.eval(icons);
    w.eval(js);
    const d = w.document;
    return { w, d, calls, release: () => release && release(), count: (v) => calls.filter((c) => c === v).length };
}
const iconOf = (el) => { const s = el && el.querySelector("svg.az-icon"); return s ? s.getAttribute("data-icon") : null; };
const key = (w, el, k) => el.dispatchEvent(new w.KeyboardEvent("keydown", { key: k, bubbles: true }));

(async () => {
    // 1. Header: title from app.title, suffix "monitor" (never translated), meta, one filled button.
    const a = boot("de", { holdInventory: true });
    await sleep(20);
    const title = a.d.querySelector("header.az-page-header h1.az-page-title");
    const suffix = title.querySelector(".az-page-title__suffix");
    console.log("title:", title.textContent.replace(/\s+/g, " "), "| suffix:", suffix.textContent);
    assert.strictEqual(title.textContent.replace(/\s+/g, " ").trim(), "Image-Updates monitor");
    assert.strictEqual(suffix.textContent, "monitor");
    assert(a.d.getElementById("dm-meta").classList.contains("az-meta"));
    const refresh = a.d.getElementById("dm-refresh");
    assert(refresh.classList.contains("az-btn") && refresh.classList.contains("az-btn--primary"));
    assert.strictEqual(iconOf(refresh), "sync-alt", "Refresh carries sync-alt on the left");
    assert(refresh.firstElementChild.classList.contains("dm-btn-spinner"));
    // while loading: disabled, "Lädt …", the spinner instead of the icon (CSS: .is-busy)
    console.log("loading:", refresh.textContent.trim(), "| disabled:", refresh.disabled, "| busy:", refresh.classList.contains("is-busy"));
    assert(refresh.disabled && refresh.classList.contains("is-busy") && refresh.textContent.trim() === "Lädt …");
    assert.strictEqual(refresh.getAttribute("aria-busy"), "true");
    a.release();
    await sleep(40);
    console.log("loaded :", refresh.textContent.trim(), "| disabled:", refresh.disabled, "| meta:", a.d.getElementById("dm-meta").textContent);
    assert(!refresh.disabled && !refresh.classList.contains("is-busy") && refresh.textContent.trim() === "Aktualisieren");
    assert.strictEqual(refresh.getAttribute("aria-busy"), "false");
    assert(a.d.getElementById("dm-meta").textContent.includes("31 Container"));
    const primaries = [...a.d.querySelectorAll(".az-page .az-btn--primary")].map((b) => b.id);
    console.log("filled buttons on the page:", primaries.join(", "));
    assert.deepStrictEqual(primaries, ["dm-refresh"]);

    // 2. Tabs: az-tabs/az-tab with aria-selected; messages and requirements under the tab bar.
    const tabs = [...a.d.querySelectorAll("nav.az-tabs[role=tablist] > button.az-tab[role=tab]")];
    const state = () => tabs.map((t) => `${t.id}:${t.getAttribute("aria-selected")}:${t.getAttribute("tabindex") || "0"}`).join(" ");
    console.log("\ntabs:", state(), "| label:", a.d.querySelector("nav.az-tabs").getAttribute("aria-label"));
    assert.deepStrictEqual(tabs.map((t) => t.textContent), ["Updates", "Backups & Dumps"]);
    assert.strictEqual(state(), "dm-tab-btn-updates:true:0 dm-tab-btn-backups:false:-1");
    assert.strictEqual(a.d.querySelector("nav.az-tabs").getAttribute("aria-label"), "Bereiche");
    const order = [...a.d.querySelector(".az-page").children].map((c) => c.id || c.tagName.toLowerCase() + "." + c.className.split(" ")[0]);
    console.log("frame:", order.join(" > "));
    assert.deepStrictEqual(order.slice(0, 6),
        ["header.az-page-header", "nav.az-tabs", "dm-status", "dm-deps", "dm-tab-updates", "dm-tab-backups"]);
    assert.strictEqual(order[order.length - 1], "footer.az-signature");
    assert(!a.d.getElementById("dm-deps").hidden, "requirements visible (skopeo missing)");
    // mouse
    const loadsBefore = a.calls.length;
    tabs[1].click();
    await sleep(20);
    console.log("click backups:", state(), "| updates hidden:", a.d.getElementById("dm-tab-updates").hidden);
    assert.strictEqual(state(), "dm-tab-btn-updates:false:-1 dm-tab-btn-backups:true:0");
    assert(a.d.getElementById("dm-tab-updates").hidden && !a.d.getElementById("dm-tab-backups").hidden);
    assert(!a.d.getElementById("dm-deps").hidden, "requirements stay visible in every tab");
    assert.strictEqual(a.calls.length, loadsBefore, "switching tabs loads nothing");
    assert(a.d.querySelectorAll("#dm-ops tr").length > 0, "backups were loaded with the page");
    // keyboard (WAI-ARIA): arrows move and show, Home/End, focus follows
    tabs[1].focus();
    key(a.w, tabs[1], "ArrowRight");
    assert.strictEqual(a.d.activeElement, tabs[0]);
    assert.strictEqual(state(), "dm-tab-btn-updates:true:0 dm-tab-btn-backups:false:-1");
    key(a.w, tabs[0], "ArrowLeft");
    assert.strictEqual(a.d.activeElement, tabs[1]);
    assert.strictEqual(state(), "dm-tab-btn-updates:false:-1 dm-tab-btn-backups:true:0");
    key(a.w, tabs[1], "Home");
    assert.strictEqual(state(), "dm-tab-btn-updates:true:0 dm-tab-btn-backups:false:-1");
    key(a.w, tabs[0], "End");
    assert.strictEqual(state(), "dm-tab-btn-updates:false:-1 dm-tab-btn-backups:true:0");
    console.log("keyboard: ArrowRight/ArrowLeft/Home/End ok, loads:", a.calls.length - loadsBefore);
    assert.strictEqual(a.calls.length, loadsBefore, "keyboard switches load nothing either");
    // Refresh keeps the tab that is shown
    refresh.click();
    await sleep(40);
    assert.strictEqual(state(), "dm-tab-btn-updates:false:-1 dm-tab-btn-backups:true:0");
    assert(a.count("diun-inventory.py") === 2 && a.count("diun-update.py backups") === 2, "Refresh loads both tabs");
    console.log("refresh keeps the backups tab; inventory runs:", a.count("diun-inventory.py"));

    // 3. Status labels with their fixed icons, no gold (az-label only).
    const b = boot("de");
    await sleep(40);
    const rows = [...b.d.querySelectorAll("#dm-rows tr")];
    const status = (name) => rows.find((tr) => tr.querySelector(".dm-name").textContent === name).querySelector('td[data-label="Status"] .az-label');
    const want = { ollama: ["az-label az-label--info", "info-circle", "Update verfügbar"],
                   "glitchtip-postgres": ["az-label az-label--info", "info-circle", "Ausrollen ausstehend"],
                   plausible: ["az-label az-label--info", "info-circle", "Neue Version"],
                   calcom: ["az-label az-label--success", "check-circle", "Aktuell"],
                   "formbricks-app": ["az-label", "ban", "Unbekannt"] };
    for (const [name, [cls, ic, text]] of Object.entries(want)) {
        const l = status(name);
        console.log(`status ${name.padEnd(19)} ${l.className.padEnd(26)} ${iconOf(l).padEnd(13)} ${l.textContent}`);
        assert.deepStrictEqual([l.className, iconOf(l), l.textContent], [cls, ic, text]);
    }
    assert.strictEqual(b.d.querySelectorAll(".dm-badge").length, 0, "no old badges left");
    assert(!/LOCAL_ICONS|M256 8C119\.034/.test(js), "every icon comes from the shared icons.js");

    // 4. Buttons per place
    const upd = b.d.querySelector('button[data-update="ollama"]');
    assert.strictEqual(upd.className, "az-btn az-btn--secondary az-btn--sm");
    assert.deepStrictEqual([iconOf(upd), upd.textContent], ["arrow-circle-up", "Update"]);
    const roll = b.d.querySelector('button[data-update="glitchtip-postgres"]');
    assert.deepStrictEqual([iconOf(roll), roll.textContent], ["arrow-circle-up", "Ausrollen"]);
    const dis = b.d.querySelector("button[data-key]");
    console.log("\ndismiss:", dis.className, iconOf(dis), dis.getAttribute("aria-label"), "|", dis.title);
    assert.strictEqual(dis.className, "az-btn az-btn--plain az-btn--sm");
    assert.deepStrictEqual([iconOf(dis), dis.getAttribute("aria-label"), dis.title, dis.textContent], ["check", "Erledigt", "Erledigt", ""]);
    assert(b.d.querySelector("#dm-rows td:last-child").classList.contains("az-cell-actions"));
    assert(b.d.getElementById("dm-table").classList.contains("az-table") && b.d.getElementById("dm-table").classList.contains("az-table--stack"));
    assert(b.d.getElementById("dm-table").closest(".az-card .az-card__body--flush"), "table in a card, flush");
    const radar = b.d.getElementById("dm-radar");
    assert.strictEqual(radar.className.replace(" is-busy", ""), "az-btn az-btn--secondary az-btn--sm");
    assert(radar.closest(".az-card__actions"), "\"Run radar now\" belongs to the card it applies to");
    assert.strictEqual(iconOf(radar), "play");
    const dep = b.d.querySelector('.dm-dep button[data-target="skopeo"]');
    assert.deepStrictEqual([dep.className, iconOf(dep), dep.textContent], ["az-btn az-btn--secondary az-btn--sm", "plus", "Installieren"]);
    assert.deepStrictEqual([iconOf(b.d.querySelector(".dm-dep .az-label")), b.d.querySelector(".dm-dep .az-label").className],
        ["exclamation-triangle", "az-label az-label--warning"]);
    // backups tab
    const rb = b.d.querySelector("#dm-ops button[data-op]");
    assert.deepStrictEqual([rb.className, iconOf(rb), rb.textContent], ["az-btn az-btn--secondary az-btn--sm", "history", "Zurückrollen"]);
    const del = b.d.querySelector("#dm-dumps button[data-dump]");
    assert.deepStrictEqual([del.className, iconOf(del), del.getAttribute("aria-label")], ["az-btn az-btn--plain az-btn--sm", "trash", "Löschen"]);
    const inUse = b.d.querySelector("#dm-images button[data-img][disabled]");
    assert(inUse && inUse.title.startsWith("Ein Container nutzt"), "image in use: delete locked, reason as tooltip");
    const res = [...b.d.querySelectorAll("#dm-ops .az-label")].map((l) => `${l.className}:${iconOf(l)}:${l.textContent}`);
    console.log("history results:", res.join(" | "));
    assert.deepStrictEqual(res, ["az-label:history:zurückgerollt", "az-label az-label--success:check-circle:erfolgreich",
                                 "az-label:null:keine Änderung"]);
    const cause = b.d.querySelector("#dm-ops .dm-op-reason");
    assert(cause && cause.textContent.startsWith("formbricks-app: Error: Invalid environment variables"),
           "failed update: its cause under the result");
    const clean = b.d.getElementById("dm-cleanup-btn");
    assert.deepStrictEqual([clean.className, iconOf(clean)], ["az-btn az-btn--secondary az-btn--sm", "trash"]);
    // text buttons on the page always carry an icon
    const bare = [...b.d.querySelectorAll(".az-page button.az-btn:not([hidden])")].filter((x) => !x.querySelector("svg.az-icon"));
    assert.deepStrictEqual(bare.map((x) => x.id || x.className), []);

    // 5. Dialog: the main action filled, Cancel a link; delete confirms in danger
    b.d.querySelector('button[data-update="ollama"]').click();
    await sleep(30);
    const confirm = b.d.getElementById("dm-dialog-confirm"), cancel = b.d.getElementById("dm-dialog-cancel");
    console.log("\nupdate dialog:", confirm.className, "|", cancel.className, cancel.textContent);
    assert(confirm.classList.contains("az-btn--primary") && !confirm.classList.contains("az-btn--danger"));
    assert.strictEqual(cancel.className, "az-btn az-btn--link");
    assert(b.d.getElementById("dm-dialog").classList.contains("az-modal"));
    cancel.click();
    b.d.getElementById("dm-tab-btn-backups").click();
    b.d.querySelector("#dm-dumps button[data-dump]").click();
    console.log("delete dialog:", confirm.className);
    assert(confirm.classList.contains("az-btn--danger") && !confirm.classList.contains("az-btn--primary"));
    cancel.click();
    b.d.querySelector("#dm-ops button[data-op]").click();
    assert(confirm.classList.contains("az-btn--primary"), "rolling back is no deletion");
    cancel.click();

    // 6. Signature: word mark, "<product> monitor <version>", host
    const sig = b.d.querySelector("footer.az-signature");
    console.log("\nsignature:", sig.textContent.replace(/\s+/g, " ").trim());
    assert.strictEqual(sig.querySelector(".az-signature__mark").textContent, "Azentrius");
    assert.strictEqual(b.d.getElementById("dm-sig-version").textContent, `Image-Updates monitor ${VERSION}`);
    assert.deepStrictEqual([b.d.getElementById("dm-sig-host").textContent, b.d.getElementById("dm-sig-host").hidden], ["docker-host", false]);
    const e = boot("en", { files: { ...files, "/etc/hostname": null } });
    await sleep(40);
    assert.strictEqual(e.d.getElementById("dm-sig-version").textContent, `Image updates monitor ${VERSION}`);
    assert(e.d.getElementById("dm-sig-host").hidden, "no host name -> product only");
    assert.strictEqual(e.d.querySelector(".az-page-title").textContent.replace(/\s+/g, " ").trim(), "Image updates monitor");

    // 7. Messages under the tab bar as an alert with its icon
    const f = boot("en", { inventoryFails: true });
    await sleep(40);
    const st = f.d.getElementById("dm-status");
    console.log("status:", st.className, iconOf(st), "|", st.textContent);
    assert(!st.hidden && st.classList.contains("az-alert") && iconOf(st) === "info-circle");
    assert(st.textContent.startsWith("Container inventory unavailable"));

    // 8. English: no German anywhere (texts, labels, tooltips, aria)
    const all = e.d.documentElement.outerHTML;
    const german = ["Aktualisieren", "Lädt", "Bereiche", "Laufende", "Aktionen", "Aktuell", "Unbekannt", "Erledigt", "Zurückrollen",
                    "Löschen", "Installieren", "Datenbank", "Projekt", "Größe", "Abbrechen", "Image-Updates"];
    const found = german.filter((g) => all.includes(g));
    console.log("\nEN German leftovers:", found.length ? found : "none");
    assert.deepStrictEqual(found, []);

    console.log("\nALL CHECKS PASSED");
})().catch((err) => { console.error("FAILED:", err.stack || err.message); process.exit(1); });

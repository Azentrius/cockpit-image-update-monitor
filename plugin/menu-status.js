// SPDX-License-Identifier: Apache-2.0
/* Image Update Monitor, a Cockpit add-on: the status icon next to the menu
 * entry, and the moment the page itself starts.
 *
 * A page can set the status of its own menu entry only: Cockpit's shell files
 * cockpit.transport.control("notify", {page_status}) under the page of the
 * frame that sends it. So manifest.json asks Cockpit to load this page right
 * after the login ("preload", as Cockpit's own Software updates page does),
 * hidden, and this file reads nothing but /var/lib/diun-radar/status.json,
 * the add-on's status contract (docs/INTERNALS.md, `status`):
 *   danger -> "error", warning -> "warning", info -> "info", ok and none: no icon.
 * The icon's tooltip is the contract's summary in the session language. The
 * state is judged as `diun-update.py status` judges it (diunstatus.py): from
 * the file's facts, the radar's age as of now.
 *
 * diun.js (inventory, watches, helpers) waits for `shown`: the first time the
 * page is shown. cockpit.hidden can be trusted once a first answer is back:
 * the shell sends a hidden frame its hint right after "init" (Cockpit 264 to
 * 360), and messages from the shell arrive in order.
 */
(function () {
    "use strict";

    const STATUS_PATH = "/var/lib/diun-radar/status.json";
    const STALE_S = 24 * 3600;        // the radar is outdated after 24 h (STALE_AFTER in diunstatus.py)
    const TYPES = { danger: "error", warning: "warning", info: "info" };   // Cockpit's page status types
    const SEVERITY = { danger: 0, warning: 1, info: 2 };
    const KEYS = { updates: "menu.updates", failed: "menu.failed", radar: "menu.radar",
                   rollout: "menu.rollout", "new-versions": "menu.newVersions" };
    // The radar's and Diun's timestamps (TIME_RE in diunstatus.py).
    const TIME_RE = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?/;

    const I18N = window.ImageUpdatesI18n;
    const LANG = /^de([-_]|$)/i.test(String(cockpit.language || "")) ? "de" : "en";

    function t(key, vars) {
        let s = I18N[LANG][key];
        if (s === undefined) s = I18N.en[key];
        if (typeof s === "object") s = (vars && vars.n === 1) ? s.one : s.other;
        return s.replace(/\{(\w+)\}/g, (m, k) => (vars && k in vars ? String(vars[k]) : m));
    }

    const isObject = (v) => Boolean(v) && typeof v === "object" && !Array.isArray(v);

    // Seconds since the epoch of "2026-10-15T19:00:50Z" (seconds_of); null if unreadable.
    function secondsOf(s) {
        const m = typeof s === "string" ? TIME_RE.exec(s) : null;
        const at = m ? Date.parse(m[1] + "T" + m[2] + "Z") : NaN;
        if (isNaN(at)) return null;
        const zone = m[3];
        let offset = 0;
        if (zone && zone !== "Z") {
            offset = (zone[0] === "-" ? -1 : 1) * (Number(zone.slice(1, 3)) * 3600 + Number(zone.slice(-2)) * 60);
        }
        return at / 1000 - offset;
    }

    // The items of the contract, as states and counts (items_of).
    function itemsOf(facts, now) {
        const part = (id) => (isObject(facts[id]) ? facts[id] : {});
        const names = (id) => {
            const list = part(id).names;
            if (!Array.isArray(list)) return { id, state: "none", n: 0 };
            return { id, state: list.length ? "info" : "ok", n: list.length };
        };
        const ops = Array.isArray(part("failed").ops) ? part("failed").ops.filter(isObject) : [];
        const radar = part("radar");
        const generated = secondsOf(radar.generated);
        let radarState = "ok";
        if (!radar.found) radarState = "none";
        else if (generated === null || now - generated > STALE_S) radarState = "warning";
        return [
            names("updates"),
            { id: "failed", state: ops.length ? "danger" : "ok", n: ops.length },
            { id: "radar", state: radarState, n: 0 },
            names("rollout"),
            names("new-versions"),
        ];
    }

    // The menu's status for a status.json (`now` in seconds): {type, title},
    // or null for no icon. Without facts the file counts as unreadable: none.
    function statusOf(doc, now) {
        if (!isObject(doc) || !isObject(doc.facts)) return null;
        const items = itemsOf(doc.facts, now);
        // The gravest first (overall, summary_of); sort() keeps the order of equals.
        const found = items.filter((it) => it.state in SEVERITY)
                .sort((a, b) => SEVERITY[a.state] - SEVERITY[b.state]);
        if (!found.length) return null;
        const text = found.map((it) => t(KEYS[it.id], { n: it.n })).join(", ");
        return { type: TYPES[found[0].state], title: text.charAt(0).toUpperCase() + text.slice(1) };
    }

    // Sent only when it changed; the first time always (a reloaded frame may
    // find the shell still showing what it sent before).
    let sent;
    function notify(status) {
        const json = JSON.stringify(status);
        if (json === sent) return;
        sent = json;
        cockpit.transport.control("notify", { page_status: status });
    }

    // Again when the radar turns outdated, should no new file come before.
    let timer = null;
    function render(doc) {
        window.clearTimeout(timer);
        const now = Date.now() / 1000;
        notify(statusOf(doc, now));
        const radar = isObject(doc) && isObject(doc.facts) && isObject(doc.facts.radar) ? doc.facts.radar : {};
        const due = radar.found ? secondsOf(radar.generated) : null;
        if (due !== null && due + STALE_S >= now) {
            timer = window.setTimeout(() => render(doc), Math.min(due + STALE_S - now + 1, STALE_S) * 1000);
        }
    }

    let answered;
    const firstAnswer = new Promise((resolve) => { answered = resolve; });

    // The radar run and every update, tag change, rollback and cleanup write
    // the file anew; a missing or unreadable one shows no icon.
    cockpit.file(STATUS_PATH, { superuser: "try" }).watch((content) => {
        let doc = null;
        try {
            doc = content ? JSON.parse(content) : null;
        } catch (e) {
            doc = null;
        }
        render(doc);
        answered();
    });
    // Should no answer come at all, the shell's hint has long arrived by then.
    cockpit.transport.wait(() => window.setTimeout(answered, 3000));

    const shown = firstAnswer.then(() => new Promise((resolve) => {
        function check() {
            if (cockpit.hidden) return;
            cockpit.removeEventListener("visibilitychange", check);
            resolve();
        }
        cockpit.addEventListener("visibilitychange", check);
        check();
    }));

    window.ImageUpdatesMenuStatus = { shown, statusOf };
})();

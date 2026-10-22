// SPDX-License-Identifier: Apache-2.0
// Stand-in for Cockpit's base1/cockpit.js so the plugin runs in a plain
// browser (file://) with test data: no Cockpit, no Docker, no root.
// build.py replaces __FIXTURES__ with test/fixtures/* and __NOW__ with a
// fixed clock so "Radar vor 32 min" etc. stay stable in screenshots.
(function () {
    "use strict";
    const FX = __FIXTURES__;
    const NOW = Date.parse(__NOW__);
    Date.now = () => NOW;

    function fakeProcess(out, chunks, fail) {
        let cb = null;
        const p = new Promise((resolve, reject) => {
            if (!chunks.length) {
                setTimeout(() => (fail ? reject(fail) : resolve(out)), 40);
                return;
            }
            let i = 0;
            const iv = setInterval(() => {
                if (cb) cb(chunks[i]);
                i += 1;
                if (i >= chunks.length) {
                    clearInterval(iv);
                    if (fail) reject(fail); else resolve("");
                }
            }, 60);
        });
        p.stream = (f) => { cb = f; return p; };
        return p;
    }

    // `retag <container> <tag> --dry-run`: the plan of the fixture with the one
    // image line changed to the tag; containers without a fixture are refused.
    function retagPlan(name, tag) {
        const fx = FX.retag[name];
        if (!fx) {
            return { container: name, updatable: true, retag: { tag, possible: false, reason: "not-found",
                                                               detail: name, changes: [] } };
        }
        const [target, jump, choice] = fx.targets[tag] || [null, null, "newer"];
        const old = fx.line.old;
        const neu = old.slice(0, old.lastIndexOf(":") + 1) + tag;
        return { ...fx.plan, target_version: target, jump, jump_unknown: target === null,
                 retag: { tag, from_tag: fx.plan.image.split(":").pop(), image: neu.trim().replace(/^image:\s*/, ""),
                          choice, level: choice === "newer" ? jump : null, possible: true, reason: null, detail: null,
                          variable: null, changes: [{ ...fx.line, new: neu, sha256: "0".repeat(64) }],
                          git: null, fingerprint: "f".repeat(64) } };
    }

    // `dbmajor <container> <tag> --dry-run`: the tag change of retagPlan plus
    // "major" from the fixture; a target major the fixture lists as refused
    // (PostgreSQL 18 without PGDATA) is refused as the helper does it.
    function majorPlan(name, tag) {
        const plan = retagPlan(name, tag);
        const fx = FX.majors[name];
        const to = parseInt(plan.target_version, 10);
        const no = !fx ? { reason: "major-not-supported", detail: plan.image || name } : (fx.refused || {})[to];
        return { ...plan, major: no ? { possible: false, ...no }
                                    : { ...fx.major, to_major: to, fingerprint: "e".repeat(64) } };
    }

    window.cockpit = {
        language: "en",
        hidden: false,
        addEventListener() {},
        removeEventListener() {},
        locale(po) {
            if (po && po[""] && po[""].language) this.language = po[""].language;
        },
        file(path) {
            return {
                read: () => Promise.resolve(path in FX.files ? FX.files[path] : null),
                watch: (cb) => cb(FX.files[path], "tag-1"),
                replace: () => Promise.resolve(),
            };
        },
        spawn(argv) {
            const tool = argv[0].split("/").pop();
            const verb = argv[1];
            if (tool === "diun-inventory.py") return fakeProcess(FX.inventory, []);
            if (tool === "diun-deps.sh" && verb === "check") return fakeProcess(JSON.stringify(FX.deps), []);
            if (tool === "diun-update.py" && verb === "plan") {
                return fakeProcess(JSON.stringify(FX.plans[argv[2]] || FX.plans["*"]), []);
            }
            if (tool === "diun-update.py" && verb === "backups") return fakeProcess(JSON.stringify(FX.backups), []);
            if (tool === "diun-update.py" && verb === "retag" && argv[4] === "--dry-run") {
                return fakeProcess(JSON.stringify(retagPlan(argv[2], argv[3])), []);
            }
            if (tool === "diun-update.py" && verb === "retag") return fakeProcess("", FX.retagLog);
            if (tool === "diun-update.py" && verb === "dbmajor" && argv[4] === "--dry-run") {
                return fakeProcess(JSON.stringify(majorPlan(argv[2], argv[3])), []);
            }
            if (tool === "diun-update.py" && verb === "rebuild" && argv[3] === "--dry-run") {
                return fakeProcess(JSON.stringify(FX.rebuild[argv[2]] ||
                    { container: argv[2], updatable: false, reason: "not-built" }), []);
            }
            if (tool === "diun-update.py" && verb.indexOf("delete-") === 0) return fakeProcess("", ["Deleted.\n"]);
            if (tool === "diun-update.py" && verb === "radar") return fakeProcess("", ["Version radar running …\n", "Version radar finished.\n"]);
            if (tool === "diun-update.py" && verb === "cleanup") {
                const c = FX.backups.cleanup;
                return fakeProcess("", c.dumps.map((p) => "Deleted dump " + p + "\n")
                    .concat(c.images.map((r) => "Removed rollback image " + r + "\n"))
                    .concat(["@@RESULT " + JSON.stringify({ status: "ok", dumps: c.dumps, images: c.images,
                                                            bytes: c.bytes, failed: [] }) + "\n"]));
            }
            return fakeProcess("", FX.log);    // update / rollback / install: live log
        },
        transport: { wait() {}, control() {} },
    };
})();

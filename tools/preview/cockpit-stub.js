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

    window.cockpit = {
        language: "en",
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
            if (tool === "diun-update.py" && verb.indexOf("delete-") === 0) return fakeProcess("", ["Deleted.\n"]);
            return fakeProcess("", FX.log);    // update / rollback / install: live log
        },
        transport: { wait() {} },
    };
})();

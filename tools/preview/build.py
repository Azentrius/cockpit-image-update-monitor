#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Builds a static preview of the plugin with the test fixtures.

    python3 tools/preview/build.py            -> tools/preview/out/diun/index.html
    open   tools/preview/out/diun/index.html?lang=de   (or ?lang=en)

Cockpit's base1/cockpit.js is replaced by cockpit-stub.js, which answers the
plugin's spawn/file calls from test/fixtures. Nothing touches a real server.
The output directory is generated and git-ignored.
"""
import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
FIX = os.path.join(REPO, "test", "fixtures")
OUT = os.path.join(HERE, "out")
NOW = "2026-10-12T02:50:00Z"

PO_STUB = """// Stand-in for Cockpit's base1/po.js: ?lang=de switches the session language.
const _l = new URLSearchParams(location.search).get("lang");
if (_l && _l !== "en") cockpit.locale({"": {"language": _l, "language-direction": "ltr"}});
"""


def main():
    preview = json.load(open(os.path.join(FIX, "preview.json")))
    fixtures = {
        "files": {
            "/var/lib/diun/events.jsonl": open(os.path.join(FIX, "events.jsonl")).read(),
            "/var/lib/diun-radar/versions.json": json.load(open(os.path.join(FIX, "versions2.json"))),
            "/var/lib/diun/dismissed.json": None,
        },
        "inventory": open(os.path.join(FIX, "inventory3.jsonl")).read(),
        "deps": {"root": True, "docker": {"state": "ok", "compose": True},
                 "diun": {"state": "running", "events": True}, "skopeo": {"state": "ok"},
                 "radar": {"state": "active", "running": False},
                 "cleanup": {"state": "active", "can_install": True}},
        "plans": preview["plans"],
        "backups": preview["backups"],
        "log": preview["log"],
    }
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(os.path.join(OUT, "base1"))
    shutil.copytree(os.path.join(REPO, "plugin"), os.path.join(OUT, "diun"))
    stub = open(os.path.join(HERE, "cockpit-stub.js")).read()
    stub = stub.replace("__FIXTURES__", json.dumps(fixtures, ensure_ascii=False)).replace("__NOW__", json.dumps(NOW))
    open(os.path.join(OUT, "base1", "cockpit.js"), "w").write(stub)
    open(os.path.join(OUT, "base1", "po.js"), "w").write(PO_STUB)
    print("Preview:", os.path.join(OUT, "diun", "index.html") + "?lang=de")


if __name__ == "__main__":
    main()

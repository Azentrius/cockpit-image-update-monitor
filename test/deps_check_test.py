#!/usr/bin/env python3
"""`diun-deps.sh check` builds its JSON with printf: whatever this machine has
(Docker or not, systemd or not, root or not), stdin must be one valid object
with the fields the plugin reads.
    ../lib/diun-deps.sh check | python3 deps_check_test.py"""
import json
import sys

d = json.load(sys.stdin)
want = {
    "docker": {"state", "version", "compose"},
    "diun": {"state", "managed", "events", "can_install", "can_start"},
    "skopeo": {"state", "version", "can_install"},
    "radar": {"state", "running", "can_install"},
    "cleanup": {"state", "can_install"},
}
missing = [f"{k}.{f}" for k, fields in want.items() for f in fields if f not in d.get(k, {})]
if "root" not in d:
    missing.append("root")
print(json.dumps(d))
if missing:
    sys.exit("missing: " + ", ".join(missing))
print("ALL CHECKS PASSED")

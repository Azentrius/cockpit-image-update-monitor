#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Builds the v2 fixtures (inventory3.jsonl + versions2.json) from
inventory2.jsonl and the values the new radar reported on a real host
(2026-10-12, before the postgres roll-out)."""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
TS = "2026-10-12T02:17:34Z"
ALL, LINE, NONE = ["patch", "minor", "major"], ["patch", "minor"], []

# (image, tag): (db, reach, {version: (patch, minor, major, status, as_tag)}, by_tag, newest)
R = {
    ("crazymax/diun", "4.33.0"): (False, NONE, {"4.33.0": (None, None, None, "current", "4.33.0")}, True, "4.33.0"),
    ("ghcr.io/tecnativa/docker-socket-proxy", "v0.4.1"): (False, NONE, {"0.4.1": ("v0.4.2", "v0.5.0", None, "minor", "v0.4.1")}, True, "v0.5.0"),
    ("onlyoffice/documentserver", "9.4.0.1"): (False, NONE, {"9.4.0.1": (None, None, None, "current", "9.4.0.1")}, True, "9.4.0.1"),
    ("nextcloud", "apache"): (False, ALL, {"34.0.3": ("34.0.4-apache", None, "35.0.1-apache", "major", "34.0.3-apache")}, False, "35.0.1"),
    ("postgres", "16-alpine"): (True, LINE, {"16.14": (None, "16.15-alpine", "18.6-alpine", "major", "16.14-alpine"),
                                             "16.15": (None, None, "18.6-alpine", "major", "16.15-alpine")}, True, "18.6"),
    ("redis", "7-alpine"): (True, LINE, {"7.4.11": (None, None, "8.10.2-alpine", "major", "7.4.11-alpine")}, True, "8.10.2"),
    ("docuseal/docuseal", "latest"): (False, ALL, {"3.1.6": ("3.1.7", "3.3.1", None, "minor", "3.1.6")}, False, "3.3.1"),
    ("docker.io/sakowicz/actual-ai", "latest"): (False, ALL, {"2.4.1": ("2.4.4", None, "3.0.2", "major", "2.4.1")}, False, "3.0.2"),
    ("ollama/ollama", "latest"): (False, ALL, {"0.30.11": (None, "0.40.3", None, "minor", "0.30.11")}, False, "0.40.3"),
    ("actualbudget/actual-server", "26.6.0"): (False, NONE, {"26.6.0": (None, "26.10.0", None, "minor", "26.6.0")}, True, "26.10.0"),
    ("ghcr.io/plausible/community-edition", "v2.1.4"): (False, NONE, {"2.1.4": ("v2.1.5", None, "v3.2.1", "major", "v2.1.4")}, True, "v3.2.1"),
    ("clickhouse/clickhouse-server", "24.3.3.102-alpine"): (True, NONE, {"24.3.3.102": ("24.3.18.7-alpine", "24.12.6.70-alpine", "26.9.16.11-alpine", "major", "24.3.3.102-alpine")}, True, "26.9.16.11"),
    ("rustdesk/rustdesk-server", "latest"): (False, ALL, {"1.1.15": ("1.1.16", None, None, "patch", "1.1.15")}, False, "1.1.16"),
    ("calcom/cal.com", "latest"): (False, ALL, {"6.2.0": (None, None, None, "current", "v6.2.0")}, False, "v6.2.0"),
    ("postgres", "15-alpine"): (True, LINE, {"15.18": (None, "15.19-alpine", "18.6-alpine", "major", "15.18-alpine")}, True, "18.6"),
    ("cubejs/cube", "v1.6.6"): (False, NONE, {"1.6.6": ("v1.6.74", "v1.8.3", None, "minor", "v1.6.6")}, True, "v1.8.3"),
    ("pgvector/pgvector", "pg17"): (True, LINE, {"17.10": None}, True, None),
    ("valkey/valkey", "latest"): (True, ALL, {"9.1.0": ("9.1.2", None, None, "patch", "9.1.0")}, False, "9.2"),
    ("ghcr.io/cbcoutinho/nextcloud-mcp-server", "latest"): (False, ALL, {}, False, "0.199.3"),
    ("ghcr.io/formbricks/formbricks", "latest"): (False, ALL, {}, False, "6.0.2"),
    ("boky/postfix", "latest"): (False, ALL, {"5.1.0": (None, None, None, "current", "5.1.0")}, False, "5.1.0"),
    ("glitchtip/glitchtip", "latest"): (False, ALL, {"6.1.8": (None, "6.2.6", None, "minor", "6.1.8")}, False, "6.2.6"),
    ("ghcr.io/formbricks/hub", "latest"): (False, ALL, {"0.4.0": (None, "0.8.7", None, "minor", "0.4.0")}, False, "0.8.7"),
}
BY_TAG = {
    ("postgres", "16-alpine"): (None, None, "18-alpine", "major"),
    ("postgres", "15-alpine"): (None, None, "18-alpine", "major"),
    ("redis", "7-alpine"): (None, None, "8-alpine", "major"),
    ("pgvector/pgvector", "pg17"): (None, None, "pg18", "major"),
}
# Installed version per container: (version, src, from_inventory, digest-tag)
INST = {
    "nextcloud-family-app-1": ("34.0.3", "env:NEXTCLOUD_VERSION", True, None),
    "nextcloud-family-cron-1": ("34.0.3", "env:NEXTCLOUD_VERSION", True, None),
    "nextcloud-family-db-1": ("16.15", "env:PG_VERSION", True, None),
    "docuseal-db": ("16.14", "env:PG_VERSION", True, None),
    "plausible_db": ("16.14", "env:PG_VERSION", True, None),
    "glitchtip-postgres": ("16.14", "env:PG_VERSION", True, None),
    "nextcloud-family-redis-1": ("7.4.11", "env:REDIS_VERSION", True, None),
    "docuseal": ("3.1.6", "digest", False, "3.1.6"),
    "actual-ai": ("2.4.1", "digest", False, "2.4.1"),
    "ollama": ("0.30.11", "digest", False, "0.30.11"),
    "hbbs": ("1.1.15", "label", False, None),
    "hbbr": ("1.1.15", "label", False, None),
    "calcom": ("6.2.0", "label", False, None),
    "calcom-db": ("15.18", "env:PG_VERSION", True, None),
    "formbricks-db": ("17.10", "env:PG_VERSION", True, None),
    "formbricks-redis": ("9.1.0", "env:VALKEY_VERSION", True, None),
    "glitchtip-redis": ("9.1.0", "env:VALKEY_VERSION", True, None),
    "prod-mcp-1": (None, "digest", False, None),
    "formbricks-app": (None, "digest", False, None),
    "smtp-relay": ("5.1.0", "digest", False, "5.1.0"),
    "smtp-relay-2": ("5.1.0", "digest", False, "5.1.0"),
    "glitchtip-web": ("6.1.8", "env:GLITCHTIP_VERSION", True, None),
    "glitchtip-worker": ("6.1.8", "env:GLITCHTIP_VERSION", True, None),
    "formbricks-hub": ("0.4.0", "digest", False, "0.4.0"),
}

rows = [json.loads(line) for line in open(os.path.join(HERE, "inventory2.jsonl"))]
recs = {}
with open(os.path.join(HERE, "inventory3.jsonl"), "w") as out:
    for c in rows:
        key = (c["image"], c["tag"])
        v, src, local, dtag = INST.get(c["name"]) or (re.match(r"v?([0-9.]+)", c["tag"]).group(1), "tag", True, None)
        c["version"], c["version_src"] = (v, src) if local else (None, None)
        c["pulled_version"] = "16.15" if c.get("pulled") else None
        c["files"] = [f"/opt/{c['project']}/docker-compose.yml"] if c.get("managed") else []
        c["ci_job"] = False
        c["created"] = "2026-10-01T00:00:00Z"
        c["revision"] = ""
        out.write(json.dumps(c) + "\n")
        db, reach, versions, has_by_tag, newest = R[key]
        rec = recs.setdefault(key, {"image": key[0], "tag": key[1], "containers": [], "db": db, "reach": reach,
                                    "installed": {}, "versions": {}, "newest": newest, "checked": TS})
        rec["containers"].append(c["name"])
        entry = {"version": v, "src": src}
        if src == "digest":
            entry.update(tag=dtag, ts=TS)
        rec["installed"][c["imgid"]] = entry
        if c.get("pulled"):
            rec["installed"][c["tagid"]] = {"version": "16.15", "src": "env:PG_VERSION"}
        for ver, cmp in versions.items():
            if cmp is None:
                rec["versions"][ver] = {"status": "unknown"}
            else:
                p, mi, ma, st, as_tag = cmp
                rec["versions"][ver] = {"patch": p, "minor": mi, "major": ma, "status": st, "as_tag": as_tag}
        if has_by_tag:
            bt = BY_TAG.get(key)
            if bt:
                rec["by_tag"] = dict(zip(("patch", "minor", "major", "status"), bt))
            else:
                first = next(iter(rec["versions"].values()))
                rec["by_tag"] = {k: first.get(k) for k in ("patch", "minor", "major", "status")}
        sts = [x["status"] for x in rec["versions"].values()]
        order = ["major", "minor", "patch", "current", "unknown"]
        rec["status"] = min(sts, key=order.index) if sts else "unknown"
# an image whose registry call failed this run, previous result reused
recs[("ghcr.io/tecnativa/docker-socket-proxy", "v0.4.1")].update(stale=True, error="registry timeout")
json.dump({"generated": TS, "images": list(recs.values())},
          open(os.path.join(HERE, "versions2.json"), "w"), indent=1)
print(len(rows), "containers,", len(recs), "radar records")

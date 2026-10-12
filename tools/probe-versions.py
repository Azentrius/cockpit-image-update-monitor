#!/usr/bin/env python3
"""Read-only probe: which sources reveal the real version of each running image?"""
import json
import re
import subprocess

def docker(*a):
    return subprocess.run(["docker", *a], capture_output=True, text=True, check=True).stdout

ids = docker("ps", "-q").split()
cs = json.loads(docker("inspect", *ids))
imgs = {i["Id"]: i for i in json.loads(docker("image", "inspect", *sorted({c["Image"] for c in cs})))}
VER = re.compile(r"version", re.I)
for c in sorted(cs, key=lambda c: c["Name"]):
    i = imgs[c["Image"]]
    labels = {k: v for k, v in ((i.get("Config") or {}).get("Labels") or {}).items() if VER.search(k)}
    env = [e for e in ((i.get("Config") or {}).get("Env") or []) if VER.search(e.split("=", 1)[0])]
    print(json.dumps({
        "name": c["Name"].lstrip("/"),
        "ref": c["Config"]["Image"],
        "created": i.get("Created", "")[:19],
        "digests": i.get("RepoDigests") or [],
        "labels": labels,
        "env": env,
    }, ensure_ascii=False))

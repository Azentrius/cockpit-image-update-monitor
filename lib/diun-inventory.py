#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Container inventory for Image Update Monitor, the Cockpit add-on.

Emits one JSON object per running container:
  name, image (repo, tag-less), tag (as configured: latest, 16-alpine, v2.1.4)
  digest   current repo digest of the image the container runs on
  imgid    image the container runs on
  version, version_src
           exact version of that image from local data: the tag itself
           (v2.1.4) or the app's version variable (PG_VERSION=16.14,
           GLITCHTIP_VERSION=6.1.8); null if only the registry can tell
           (the version radar resolves those by digest)
  tagid    image the tag points to locally right now
  pulled   True if a newer image was already pulled for this tag but the
           container still runs the old one ("pulled, not rolled out")
  pulled_version  version of that newer local image, if known
  project, service, managed   docker compose labels; managed = the addon
           can recreate it with docker compose

Invoked by the plugin via cockpit.spawn (superuser) on every load, so it
uses a handful of docker calls instead of several per container.
Only one instance at a time: a second call exits 75 immediately (2026-10-12).
"""

import fcntl
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from diunlib import local_version, norm, split_ref  # noqa: E402

LOCK = "/run/lock/diun-inventory.lock"
L_PROJECT = "com.docker.compose.project"
L_SERVICE = "com.docker.compose.service"
L_WORKDIR = "com.docker.compose.project.working_dir"
L_FILES = "com.docker.compose.project.config_files"


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=60, check=True).stdout


def main():
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 75

    ids = docker("ps", "-q").split()
    if not ids:
        return 0
    containers = json.loads(docker("inspect", *ids))

    # Where every local repo:tag points right now (one call for all).
    tag_to_id = {}
    for line in docker("image", "ls", "--no-trunc",
                       "--format", "{{.Repository}}:{{.Tag}} {{.ID}}").splitlines():
        ref, _, iid = line.rpartition(" ")
        if ref and not ref.endswith(":<none>"):
            tag_to_id[ref] = iid

    def tag_id(ref):
        repo, tag = split_ref(ref)
        return "" if "@" in ref else tag_to_id.get(f"{norm(repo)}:{tag}", "")

    # Images in use plus the newer ones their tags point to (one call).
    image_ids = sorted(({c["Image"] for c in containers} |
                        {tag_id(c["Config"]["Image"]) for c in containers}) - {""})
    images = {i["Id"]: i for i in json.loads(docker("image", "inspect", *image_ids))}

    for c in containers:
        ref = c["Config"]["Image"]
        repo, tag = split_ref(ref)
        imgid = c["Image"]
        img = images.get(imgid) or {}
        digests = img.get("RepoDigests") or []
        digest = digests[0].split("@", 1)[1] if digests and "@" in digests[0] else ""
        tagid = tag_id(ref)
        pulled = bool(tagid) and tagid != imgid
        version, version_src = local_version(repo, tag, img)
        pulled_version = local_version(repo, tag, images.get(tagid))[0] if pulled else None
        labels = c["Config"].get("Labels") or {}
        project = labels.get(L_PROJECT, "")
        service = labels.get(L_SERVICE, "")
        print(json.dumps({
            "name": c["Name"].lstrip("/"),
            "image": repo,
            "tag": tag,
            "digest": digest,
            "imgid": imgid,
            "version": version,
            "version_src": version_src,
            "tagid": tagid,
            "pulled": pulled,
            "pulled_version": pulled_version,
            "project": project,
            "service": service,
            "managed": bool(project and service and labels.get(L_WORKDIR) and labels.get(L_FILES)),
        }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as e:
        print((e.stderr or str(e)).strip(), file=sys.stderr)
        sys.exit(1)

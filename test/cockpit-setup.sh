#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Prepares a throw-away machine for test/cockpit-session.py (CI, test workflow, job cockpit;
# never a server with real containers):
#   - two Cockpit users with the passwords given: ADMIN_USER in the sudo group, PLAIN_USER not,
#   - a local registry (127.0.0.1:5000) and the compose project "imgupd-ui": its container runs
#     busybox 1.36.1 under the tag localhost:5000/imgupd-ui/app:1, while that tag points to
#     busybox 1.37.0 by now, in the registry and here (pulled, not rolled out).
# Prints APP, OLD_ID and NEW_ID for the session test (KEY=value lines, for $GITHUB_ENV); all
# other output goes to stderr.
#
#   sudo env ADMIN_USER=… ADMIN_PW=… PLAIN_USER=… PLAIN_PW=… sh test/cockpit-setup.sh >> "$GITHUB_ENV"
set -eu
: "${ADMIN_USER:?}" "${ADMIN_PW:?}" "${PLAIN_USER:?}" "${PLAIN_PW:?}"
REG=localhost:5000
IMG=$REG/imgupd-ui/app:1
DIR=/opt/imgupd-ui
REGISTRY_IMAGE=${REGISTRY_IMAGE:-registry:2.8.3}

useradd -m -s /bin/bash -G sudo "$ADMIN_USER"
useradd -m -s /bin/bash "$PLAIN_USER"
printf '%s:%s\n%s:%s\n' "$ADMIN_USER" "$ADMIN_PW" "$PLAIN_USER" "$PLAIN_PW" | chpasswd

docker run -d --name imgupd-ui-registry -p 127.0.0.1:5000:5000 "$REGISTRY_IMAGE" > /dev/null
i=0
until curl -fs "http://$REG/v2/" > /dev/null; do
    i=$((i + 1))
    [ "$i" -lt 60 ] || { echo "the registry does not answer" >&2; exit 1; }
    sleep 1
done
docker pull -q busybox:1.36.1 > /dev/null
docker pull -q busybox:1.37.0 > /dev/null
docker tag busybox:1.36.1 "$IMG"
docker push -q "$IMG" > /dev/null
mkdir -p "$DIR"
cat > "$DIR/compose.yml" <<EOF
services:
  app:
    image: $IMG
    command: ["sleep", "infinity"]
EOF
docker compose -p imgupd-ui -f "$DIR/compose.yml" up -d >&2
# A newer build under the same tag: in the registry, and pulled here, not rolled out yet.
docker tag busybox:1.37.0 "$IMG"
docker push -q "$IMG" > /dev/null
docker ps --format '{{.Names}}  {{.Image}}' >&2
echo "APP=imgupd-ui-app-1"
echo "OLD_ID=$(docker image inspect -f '{{.Id}}' busybox:1.36.1)"
echo "NEW_ID=$(docker image inspect -f '{{.Id}}' busybox:1.37.0)"

#!/bin/bash
# shellcheck disable=SC2034
# SPDX-License-Identifier: Apache-2.0
# End-to-end test of diun-update.py on a real Docker host, inside the
# throw-away compose projects "imgupd-test" and "imgupd-test-my". No other
# container, compose file, volume or dump is touched; everything created here
# is removed on exit.
#
# Needs: root, the helpers installed in /usr/lib/cockpit-image-updates, docker
# compose, and two DIFFERENT builds of the same postgres major:
#   OLD_IMAGE  (default postgres:16.14-alpine)  -> "installed"
#   NEW_IMAGE  (default postgres:16.15-alpine)  -> "pulled, not rolled out"
# Missing images are pulled. Both are tagged locally as imgupd-test/pg:16;
# re-tagging that name to NEW_IMAGE creates the "pulled" state without any
# registry access during the test.
# The MariaDB/MySQL part uses MARIADB_IMAGE (default mariadb:11) and
# MYSQL_IMAGE (default mysql:8); E2E_MYSQL=0 skips it. E2E_LIB runs the
# helpers from another folder (e.g. a branch before it is installed).
# The tag change (stage 2) needs choices from the radar: the test puts a
# record for its own image into versions.json and removes it again.
#
#   sudo OLD_IMAGE=postgres:16.14-alpine NEW_IMAGE=postgres:16.15-alpine bash test/e2e-server.sh
#
# The cleanup is checked only where it cannot reach other data: after an
# update it is limited to that project, and the global `cleanup` runs as
# --dry-run only.
#
# (SC2034 is off for the whole file: the check '…' strings run through eval,
# so shellcheck cannot see that the variables they read are used.)
set -u
LIB=${E2E_LIB:-/usr/lib/cockpit-image-updates}
H=$LIB/diun-update.py
INV=$LIB/diun-inventory.py
VERSIONS=/var/lib/diun-radar/versions.json
P=imgupd-test
P2=imgupd-test-my
D=${E2E_DIR:-/root/imgupd-test}
D2=$D-my
BK_DIR=/var/backups/image-updates
DUMPS=$BK_DIR/dumps
OPS=$BK_DIR/operations.jsonl
LOGS=$BK_DIR/logs
export DIUN_LANG=en

[ "$(id -u)" = 0 ] || { echo "ABORT: run as root"; exit 1; }
[ -x "$H" ] || { echo "ABORT: $H missing (install first)"; exit 1; }
for p in "$P" "$P2"; do
    if [ -n "$(docker ps -aq --filter label=com.docker.compose.project="$p")" ]; then
        echo "ABORT: compose project $p exists already"; exit 1
    fi
done
OLD_IMAGE=${OLD_IMAGE:-postgres:16.14-alpine}
NEW_IMAGE=${NEW_IMAGE:-postgres:16.15-alpine}
MARIADB_IMAGE=${MARIADB_IMAGE:-mariadb:11}
MYSQL_IMAGE=${MYSQL_IMAGE:-mysql:8}
E2E_MYSQL=${E2E_MYSQL:-1}
IMAGES="$OLD_IMAGE $NEW_IMAGE"
[ "$E2E_MYSQL" = 1 ] && IMAGES="$IMAGES $MARIADB_IMAGE $MYSQL_IMAGE"
for i in $IMAGES; do
    docker image inspect "$i" >/dev/null 2>&1 || docker pull -q "$i" >/dev/null || { echo "ABORT: $i not available"; exit 1; }
done
OLD=$(docker image inspect -f '{{.Id}}' "$OLD_IMAGE")
NEW=$(docker image inspect -f '{{.Id}}' "$NEW_IMAGE")
FAIL=0
check() { if eval "$2"; then echo "PASS: $1"; else echo "FAIL: $1"; FAIL=1; fi; }
result_of() { grep '^@@RESULT ' "$1" | tail -n1 | cut -c10-; }
jget() { python3 -c "import sys,json; d=json.loads(sys.argv[1]); print(eval(sys.argv[2]))" "$1" "$2"; }
img_of() { docker inspect -f '{{.Image}}' "$1"; }
pg_version() { docker image inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$1" | sed -n 's/^PG_VERSION=\([0-9.]*\).*/\1/p'; }
other_dumps() { find "$DUMPS" -name '*.sql.gz' ! -path "$DUMPS/$P/*" ! -path "$DUMPS/$P2/*" 2>/dev/null | wc -l; }
radar_unit() { systemctl cat diun-radar.service >/dev/null 2>&1; }
cfg_image() { docker inspect -f '{{.Config.Image}}' "$1"; }
sum_of() { sha256sum < "$1"; }
# The radar cannot list tags of the local-only test image, so the test offers
# $1 itself: a record in versions.json for the running version of
# imgupd-test-db (after any radar run started by an update has finished).
offer() {
    local v
    for _ in $(seq 120); do systemctl is-active --quiet diun-radar.service || break; sleep 1; done
    v=$(jget "$(python3 "$H" plan imgupd-test-db)" 'd["installed_version"]')
    python3 - "$VERSIONS" "$v" "$1" <<'PY'
import json, os, sys, tempfile
p, version, tag = sys.argv[1:4]
os.makedirs(os.path.dirname(p), exist_ok=True)
try:
    d = json.load(open(p))
except (OSError, ValueError):
    d = {"images": []}
d["images"] = [r for r in d.get("images") or [] if not (r.get("image") or "").startswith("imgupd-test/")]
d["images"].append({"image": "imgupd-test/pg", "tag": "16", "db": True,
                    "containers": ["imgupd-test-app", "imgupd-test-db"],
                    "versions": {version: {"status": "current", "choices": {
                        "pin": tag, "lines": [], "newer": [], "floating": []}}}})
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
with os.fdopen(fd, "w") as f:
    json.dump(d, f, ensure_ascii=False, indent=1)
os.chmod(tmp, 0o644)
os.replace(tmp, p)
PY
}

cleanup() {
    echo "=== Cleanup ==="
    docker compose -p "$P" -f "$D/docker-compose.yml" down -v --remove-orphans >/dev/null 2>&1
    docker compose -p "$P2" -f "$D2/docker-compose.yml" down -v --remove-orphans >/dev/null 2>&1
    # Docker refuses to drop the LAST tag of an image that running containers
    # use (e.g. other databases on the same build). -f then only removes our
    # tag - the image itself stays because it is in use.
    for t in $(docker image ls --format '{{.Repository}}:{{.Tag}}' | grep '^imgupd-test/'); do
        if docker rmi "$t" >/dev/null 2>&1; then
            echo "untagged $t"
        elif [ -n "$(docker ps -q --filter ancestor="$(docker image inspect -f '{{.Id}}' "$t")")" ]; then
            docker rmi -f "$t" >/dev/null && echo "untagged $t (last tag, image still running)"
        else
            echo "WARNING: $t not removed"
        fi
    done
    rm -rf "${DUMPS:?}/${P:?}" "${DUMPS:?}/${P2:?}" "${BK_DIR:?}/compose/${P:?}"
    rmdir "$BK_DIR/compose" 2>/dev/null
    rm -f "${LOGS:?}"/[0-9]*-imgupd-test-*.log
    if [ -f "$VERSIONS" ]; then
        python3 - "$VERSIONS" <<'PY'
import json, os, sys, tempfile
p = sys.argv[1]
d = json.load(open(p))
keep = [r for r in d.get("images") or [] if not (r.get("image") or "").startswith("imgupd-test/")]
if len(keep) != len(d.get("images") or []):
    d["images"] = keep
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, p)
    print("versions.json: test record removed")
PY
    fi
    if [ -f "$OPS" ]; then
        python3 - "$OPS" <<'PY'
import json, os, re, sys
p = sys.argv[1]
keep = [l for l in open(p) if not re.match(r"^\d{8}-\d{6}-imgupd-test-", json.loads(l).get("id", ""))]
if keep:
    open(p, "w").writelines(keep)
else:
    os.unlink(p)
print("operations.jsonl:", len(keep), "other entries kept")
PY
    fi
    rm -rf "${D:?}" "${D2:?}"
    echo "Left over: $(docker ps -a --format '{{.Names}}' | grep -c imgupd) containers," \
         "$(docker image ls | grep -c imgupd-test) tags," \
         "$(find "$DUMPS" -maxdepth 1 -name 'imgupd-test*' | wc -l) test dump folders," \
         "$(find "$LOGS" -maxdepth 1 -name '*-imgupd-test-*' 2>/dev/null | wc -l) test logs," \
         "$(find "$BK_DIR/compose" -maxdepth 1 -name 'imgupd-test*' 2>/dev/null | wc -l) test file backups"
}
trap cleanup EXIT

write_compose() {   # $1 = shell command of the "app" service, $2 = its restart policy (default "no")
    cat > "$D/docker-compose.yml" <<EOF
name: $P
services:
  db:
    image: imgupd-test/pg:16
    container_name: imgupd-test-db
    environment:
      POSTGRES_PASSWORD: test-only
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 2s
      retries: 30
  app:
    image: imgupd-test/pg:16
    container_name: imgupd-test-app
    entrypoint: ["sh", "-c"]
    command: ["$1"]
    restart: "${2:-no}"
  side:
    image: imgupd-test/side:1
    container_name: imgupd-test-side
    entrypoint: ["sh", "-c"]
    command: ["sleep infinity"]
    depends_on:
      db:
        condition: service_healthy
EOF
}

echo "OLD=$OLD"
echo "NEW=$NEW"
[ "$OLD" != "$NEW" ] || { echo "ABORT: no old/new image pair"; exit 1; }

echo "=== 1. Start the test project on the old image ==="
mkdir -p "$D"
write_compose "sleep infinity"
docker tag "$OLD" imgupd-test/pg:16
docker tag "$OLD" imgupd-test/side:1
docker compose -p "$P" -f "$D/docker-compose.yml" up -d --wait 2>&1 | tail -n 3
docker exec imgupd-test-db psql -U postgres -qc "create table e2e(x text); insert into e2e values ('hello-from-e2e');"
check "Containers run on OLD" '[ "$(img_of imgupd-test-db)" = "$OLD" ] && [ "$(img_of imgupd-test-app)" = "$OLD" ]'

echo "=== 2. 'Pull' a newer image (move the tag) + change compose (app exits) ==="
docker tag "$NEW" imgupd-test/pg:16
write_compose "echo Error: e2e app failure; exit 3"
PULLED=$(python3 $INV | python3 -c 'import sys,json; r=[json.loads(l) for l in sys.stdin if json.loads(l)["name"]=="imgupd-test-db"][0]; print(r["pulled"], r["managed"])')
check "Inventory reports pulled + managed" '[ "$PULLED" = "True True" ]'
PLAN=$(python3 $H plan imgupd-test-db)
echo "plan: $PLAN"
PCHK=$(jget "$PLAN" '(d["services"], d["config_changed"], d["pulled"], [x["name"] for x in d["dbs"]]) == (["app", "db"], True, True, ["imgupd-test-db"])')
check "Plan: both services, config changed, pulled, DB found" '[ "$PCHK" = True ]'
PV=$(jget "$PLAN" '(d["installed_version"], d["tag_digits"], d["jump_unknown"])')
WANT_PV="('$(pg_version "$OLD_IMAGE")', 1, False)"
check "Plan: installed version known, the tag fixes the major: $PV" '[ "$PV" = "$WANT_PV" ]'
REL=$(jget "$PLAN" 'd["dependents"] == [{"service": "side", "containers": ["imgupd-test-side"]}] and d["depends_on"] == [] and d["jobs"] == []')
check "Plan: the side service depends on the database (watched)" '[ "$REL" = True ]'

echo "=== 3. Update with dump – must fail (app exit 3) ==="
python3 $H update imgupd-test-db > /tmp/e2e-1.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-1.log
R1=$(result_of /tmp/e2e-1.log)
OP1=$(jget "$R1" 'd["op"]')
S1=$(jget "$R1" 'd["status"]'); RB1=$(jget "$R1" 'd["rollback"]'); DUMP=$(jget "$R1" 'd["dumps"][0]')
check "Exit 4 + status failed" '[ $RC = 4 ] && [ "$S1" = failed ]'
check "Dump contains the test data" 'zcat "$DUMPS/$DUMP" | grep -q hello-from-e2e'
check "Dump readable by root only (600)" '[ "$(stat -c %a "$DUMPS/$DUMP")" = 600 ]'
check "Rollback tag exists" 'docker image inspect "$RB1" >/dev/null 2>&1'
CAUSE=$(jget "$R1" '(d["reason"] or {}).get("container") == "imgupd-test-app" and d["reason"]["exit_code"] == 3 and d["reason"]["line"] == "Error: e2e app failure"')
check "Most likely cause in the result: the app and its error line" '[ "$CAUSE" = True ]'
check "Most likely cause in the output" 'grep -q "Most likely cause (imgupd-test-app): Error: e2e app failure" /tmp/e2e-1.log'
HCAUSE=$(OP=$OP1 python3 -c 'import json,os; o=[json.loads(l) for l in open("'"$OPS"'") if json.loads(l).get("id")==os.environ["OP"]][0]; print((o.get("reason") or {}).get("line"))')
check "Most likely cause in operations.jsonl" '[ "$HCAUSE" = "Error: e2e app failure" ]'
check "Output kept: root only, without @@RESULT" \
    '[ "$(stat -c %a "$LOGS/$OP1.log")" = 600 ] && grep -q "Most likely cause" "$LOGS/$OP1.log" && ! grep -q "^@@RESULT" "$LOGS/$OP1.log"'
NDIG=$(OP=$OP1 python3 -c 'import json,os; o=[json.loads(l) for l in open("'"$OPS"'") if json.loads(l).get("id")==os.environ["OP"]][0]; print(len(o.get("new_digests") or []))')
check "Registry digests of the new build in the history ($NDIG)" '[ "$NDIG" -ge 1 ]'

echo "=== 4. Repair compose, roll back ==="
write_compose "sleep infinity"
python3 $H rollback "$OP1" > /tmp/e2e-2.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-2.log
check "Rollback exit 0" '[ $RC = 0 ]'
check "Containers back on OLD" '[ "$(img_of imgupd-test-db)" = "$OLD" ] && [ "$(img_of imgupd-test-app)" = "$OLD" ]'
check "Test data still there" 'docker exec imgupd-test-db psql -U postgres -Atc "select x from e2e" | grep -q hello-from-e2e'
python3 $H rollback "$OP1" > /tmp/e2e-3.log 2>&1; RC=$?
check "Second rollback refused (3)" '[ $RC = 3 ]'
python3 $H log "$OP1" > /tmp/e2e-3b.log 2>&1; RC=$?
check "log: update and rollback output" '[ $RC = 0 ] && grep -q "Most likely cause" /tmp/e2e-3b.log && grep -q "^--- Rollback ---" /tmp/e2e-3b.log && grep -q "Rollback successful" /tmp/e2e-3b.log'

echo "=== 5. Roll out again without dump – must work; cleanup of this project ==="
# Three old dumps of the test database: the rules may delete them, never the
# dump of step 3 (newest) and never a dump of another project.
for i in 1 2 3; do
    printf -- '-- fake %s\n' "$i" | gzip > "$DUMPS/$P/20200101-00000${i}_imgupd-test-db.sql.gz"
    chmod 600 "$DUMPS/$P/20200101-00000${i}_imgupd-test-db.sql.gz"
done
KEEP=$(jget "$(python3 $H backups)" 'd["cleanup"]["keep_dumps"] or 0')
OTHER_BEFORE=$(other_dumps)
docker tag "$NEW" imgupd-test/pg:16
python3 $H update imgupd-test-db --no-dump > /tmp/e2e-4.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-4.log
R2=$(result_of /tmp/e2e-4.log)
S2=$(jget "$R2" '(d["status"], len(d["dumps"]))')
check "Exit 0 + status ok, no dump" '[ $RC = 0 ] && [ "$S2" = "('"'"'ok'"'"', 0)" ]'
check "Containers now on NEW" '[ "$(img_of imgupd-test-db)" = "$NEW" ] && [ "$(img_of imgupd-test-app)" = "$NEW" ]'
check "Test data there after the update" 'docker exec imgupd-test-db psql -U postgres -Atc "select x from e2e" | grep -q hello-from-e2e'
RB2=$(jget "$R2" 'd["rollback"]')
VERS=$(jget "$R2" '(d["from_version"], d["to_version"])')
WANT_VERS="('$(pg_version "$OLD_IMAGE")', '$(pg_version "$NEW_IMAGE")')"
check "Versions in the result: $VERS" '[ "$VERS" = "$WANT_VERS" ]'
HVERS=$(OP=$(jget "$R2" 'd["op"]') python3 -c 'import json,os; o=[json.loads(l) for l in open("'"$OPS"'") if json.loads(l).get("id")==os.environ["OP"]][0]; print((o["from_version"], o["to_version"]))')
check "Versions in operations.jsonl" '[ "$HVERS" = "$WANT_VERS" ]'
N_DUMPS=$(find "$DUMPS/$P" -name '*.sql.gz' | wc -l)
if [ "$KEEP" -gt 0 ] && [ "$KEEP" -lt 4 ]; then EXP_DUMPS=$KEEP; else EXP_DUMPS=4; fi
check "Cleanup after the update: $N_DUMPS of 4 dumps left (KEEP_DUMPS=$KEEP)" '[ "$N_DUMPS" = "$EXP_DUMPS" ] && [ -f "$DUMPS/$DUMP" ]'
check "Cleanup after the update: other projects untouched" '[ "$(other_dumps)" = "$OTHER_BEFORE" ]'
if radar_unit; then
    check "Radar started after the update" 'grep -q "Version radar started" /tmp/e2e-4.log'
fi
check "Dependent service watched and still running" 'grep -q "Dependent services keep running" /tmp/e2e-4.log && [ "$(docker inspect -f "{{.State.Running}}" imgupd-test-side)" = true ]'

echo "=== 5b. Rollback refused while the compose file names another image ==="
# The rollback only takes the old tag back: with an image line edited after
# the update, compose would start that image instead (up to 0.18.0 it did, and
# the rollback said "successful"). The compose file comes back unchanged.
OP2=$(jget "$R2" 'd["op"]')
sed -i 's#^    image: imgupd-test/pg:16$#    image: imgupd-test/pg:16-edited#' "$D/docker-compose.yml"
python3 $H rollback "$OP2" > /tmp/e2e-5b.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-5b.log
check "Rollback with another image in the compose file refused (5), nothing changed" \
    '[ $RC = 5 ] && grep -q "The compose files now give app: imgupd-test/pg:16-edited" /tmp/e2e-5b.log && [ "$(img_of imgupd-test-db)" = "$NEW" ] && [ "$(img_of imgupd-test-app)" = "$NEW" ]'
write_compose "sleep infinity"

echo "=== 6. Again – nothing newer ==="
python3 $H update imgupd-test-db --no-dump > /tmp/e2e-5.log 2>&1; RC=$?
R3=$(result_of /tmp/e2e-5.log)
S3=$(jget "$R3" 'd["status"]')
check "nochange, exit 0" '[ $RC = 0 ] && [ "$S3" = nochange ]'
check "No leftover rollback tag" '[ "$(docker image ls --format "{{.Tag}}" imgupd-test/pg | grep -c rollback)" = 2 ]'

echo "=== 7. Lock: two updates at once ==="
( python3 $H update imgupd-test-db --no-dump > /tmp/e2e-6.log 2>&1 ) &
sleep 0.5
python3 $H update imgupd-test-db --no-dump > /tmp/e2e-7.log 2>&1; RC=$?
wait
check "Second update gets 75" '[ $RC = 75 ]'

echo "=== 8. Backups overview, cleanup preview ==="
BK=$(python3 $H backups)
echo "$BK" | python3 -m json.tool | head -n 60
B1=$(jget "$BK" '(len([x for x in d["dumps"] if x["project"]=="imgupd-test"]), len([x for x in d["images"] if x["ref"].startswith("imgupd-test/")]))')
B2=$(OP1="$OP1" jget "$BK" '[(o.get("rolled_back"), o["can_rollback"]) for o in d["ops"] if o["id"]==__import__("os").environ["OP1"]][0]')
B3=$(RB2="$RB2" jget "$BK" '[o["can_rollback"] for o in d["ops"] if o.get("rollback")==__import__("os").environ["RB2"]][0]')
B4=$(jget "$BK" 'sorted(d["cleanup"]) == ["bytes", "dumps", "error", "file", "image_days", "images", "keep_dumps"]')
check "$EXP_DUMPS dumps, 2 rollback images" '[ "$B1" = "($EXP_DUMPS, 2)" ]'
check "History: OP1 rolled back, no longer rollable" '[ "$B2" = "(True, False)" ]'
check "History: second update rollable" '[ "$B3" = True ]'
check "Cleanup preview in the backups list" '[ "$B4" = True ]'
python3 $H cleanup --dry-run > /tmp/e2e-8.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-8.log
check "cleanup --dry-run: exit 0, rules named, nothing deleted" \
    '[ $RC = 0 ] && grep -q "^Rules (" /tmp/e2e-8.log && [ "$(find "$DUMPS/$P" -name "*.sql.gz" | wc -l)" = "$EXP_DUMPS" ]'

echo "=== 9. Delete ==="
python3 $H delete-dump "$DUMP"; RC=$?
check "Dump deleted" '[ $RC = 0 ] && [ ! -e "$DUMPS/$DUMP" ]'
python3 $H delete-dump "imgupd-test/../../../etc/20261012-000000_passwd.sql.gz" >/dev/null; RC=$?
check "Path trick refused (64)" '[ $RC = 64 ]'
python3 $H delete-image "$RB2" >/dev/null; RC=$?
check "Rollback image removed" '[ $RC = 0 ] && ! docker image inspect "$RB2" >/dev/null 2>&1'
python3 $H delete-image "imgupd-test/pg:16" >/dev/null; RC=$?
check "Normal image refused (64)" '[ $RC = 64 ]'

echo "=== 10. Version radar now ==="
if radar_unit; then
    GEN_BEFORE=$(python3 -c 'import json; print(json.load(open("/var/lib/diun-radar/versions.json")).get("generated", ""))' 2>/dev/null)
    python3 $H radar > /tmp/e2e-9.log 2>&1; RC=$?
    sed 's/^/  | /' /tmp/e2e-9.log
    GEN_AFTER=$(python3 -c 'import json; print(json.load(open("/var/lib/diun-radar/versions.json")).get("generated", ""))' 2>/dev/null)
    check "radar: exit 0, versions.json rewritten" '[ $RC = 0 ] && [ -n "$GEN_AFTER" ] && [ "$GEN_AFTER" != "$GEN_BEFORE" ]'
else
    python3 $H radar > /dev/null 2>&1; RC=$?
    check "radar without the unit: exit 2" '[ $RC = 2 ]'
fi

echo "=== 10b. Restart loop: must fail within seconds, not after the 180 s wait ==="
docker tag "$OLD" imgupd-test/pg:16
write_compose "echo Error: e2e restart loop; exit 4" always
T0=$(date +%s)
python3 $H update imgupd-test-db --no-dump > /tmp/e2e-10.log 2>&1; RC=$?
T1=$(date +%s)
sed 's/^/  | /' /tmp/e2e-10.log
R5=$(result_of /tmp/e2e-10.log)
OP5=$(jget "$R5" 'd["op"]')
LOOP=$(jget "$R5" 'd["status"] == "failed" and (d["reason"] or {}).get("container") == "imgupd-test-app" and d["reason"]["state"] == "restarting" and d["reason"]["line"] == "Error: e2e restart loop"')
check "Restart loop: exit 4 after $((T1 - T0)) s (limit 60)" '[ $RC = 4 ] && [ $((T1 - T0)) -lt 60 ]'
check "Restart loop: cause names the app, its state and error line" '[ "$LOOP" = True ]'
write_compose "sleep infinity"
python3 $H rollback "$OP5" > /tmp/e2e-11.log 2>&1; RC=$?
check "Restart loop: rollback, containers back on NEW" '[ $RC = 0 ] && [ "$(img_of imgupd-test-db)" = "$NEW" ] && [ "$(img_of imgupd-test-app)" = "$NEW" ]'

if [ "$E2E_MYSQL" = 1 ]; then
    echo "=== 11. MariaDB + MySQL: dumps with test data ==="
    mkdir -p "$D2"
    cat > "$D2/docker-compose.yml" <<EOF
name: $P2
services:
  mariadb:
    image: imgupd-test/mariadb:11
    container_name: imgupd-test-mariadb
    # options only, as in Nextcloud's example: the entrypoint starts mariadbd with them
    command: ["--transaction-isolation=READ-COMMITTED", "--log-bin=binlog", "--binlog-format=ROW"]
    environment:
      MARIADB_ROOT_PASSWORD: test-only
      MARIADB_DATABASE: e2e
    healthcheck:
      test: ["CMD", "healthcheck.sh", "--connect", "--innodb_initialized"]
      interval: 2s
      retries: 60
  mysql:
    image: imgupd-test/mysql:8
    container_name: imgupd-test-mysql
    environment:
      MYSQL_ROOT_PASSWORD: test-only
      MYSQL_DATABASE: e2e
    healthcheck:
      test: ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 -uroot -ptest-only --silent"]
      interval: 2s
      retries: 90
EOF
    docker tag "$MARIADB_IMAGE" imgupd-test/mariadb:11
    docker tag "$MYSQL_IMAGE" imgupd-test/mysql:8
    docker compose -p "$P2" -f "$D2/docker-compose.yml" up -d --wait 2>&1 | tail -n 3
    docker exec imgupd-test-mariadb mariadb -uroot -ptest-only e2e -e "create table t(x text); insert into t values ('hello-from-mariadb');"
    docker exec imgupd-test-mysql mysql -uroot -ptest-only e2e -e "create table t(x text); insert into t values ('hello-from-mysql');" 2>/dev/null
    PLAN=$(python3 $H plan imgupd-test-mariadb)
    KINDS=$(jget "$PLAN" 'sorted((x["name"], x["kind"]) for x in d["dbs"])')
    check "Plan: both databases as mysql" '[ "$KINDS" = "[('"'"'imgupd-test-mariadb'"'"', '"'"'mysql'"'"'), ('"'"'imgupd-test-mysql'"'"', '"'"'mysql'"'"')]" ]'
    # The local-only tag cannot be pulled: the dumps run, then "nothing newer".
    python3 $H update imgupd-test-mariadb > /tmp/e2e-10.log 2>&1; RC=$?
    sed 's/^/  | /' /tmp/e2e-10.log
    R4=$(result_of /tmp/e2e-10.log)
    S4=$(jget "$R4" '(d["status"], len(d["dumps"]))')
    check "Update with dumps: exit 0, 2 dumps" '[ $RC = 0 ] && [ "$S4" = "('"'"'nochange'"'"', 2)" ]'
    MD=$(jget "$R4" '[x for x in d["dumps"] if "_imgupd-test-mariadb" in x][0]')
    MY=$(jget "$R4" '[x for x in d["dumps"] if "_imgupd-test-mysql" in x][0]')
    check "MariaDB dump: test data + end marker" 'zcat "$DUMPS/$MD" | grep -q hello-from-mariadb && zcat "$DUMPS/$MD" | tail -n 3 | grep -q "^-- Dump completed"'
    check "MySQL dump: test data + end marker" 'zcat "$DUMPS/$MY" | grep -q hello-from-mysql && zcat "$DUMPS/$MY" | tail -n 3 | grep -q "^-- Dump completed"'

    echo "=== 12. Wrong password – must stop before any change ==="
    docker exec imgupd-test-mariadb mariadb -uroot -ptest-only -e "ALTER USER 'root'@'localhost' IDENTIFIED BY 'changed'; ALTER USER 'root'@'%' IDENTIFIED BY 'changed';"
    N_BEFORE=$(find "$DUMPS/$P2" -name '*.sql.gz' | wc -l)
    OPS_BEFORE=$(wc -l < "$OPS")
    TAGS_BEFORE=$(docker image ls --format '{{.Repository}}:{{.Tag}}' | grep -c '^imgupd-test/')
    python3 $H update imgupd-test-mysql > /tmp/e2e-11.log 2>&1; RC=$?
    sed 's/^/  | /' /tmp/e2e-11.log
    check "Exit 5, the failing database named" '[ $RC = 5 ] && grep -q "Dump of imgupd-test-mariadb failed" /tmp/e2e-11.log'
    check "Nothing left: no dump, no .part, no record, no tag" \
        '[ "$(find "$DUMPS/$P2" -name "*.sql.gz" | wc -l)" = "$N_BEFORE" ] && [ -z "$(find "$DUMPS/$P2" -name "*.part")" ] && [ "$(wc -l < "$OPS")" = "$OPS_BEFORE" ] && [ "$(docker image ls --format "{{.Repository}}:{{.Tag}}" | grep -c "^imgupd-test/")" = "$TAGS_BEFORE" ]'
fi

echo "=== 13. Tag change in the compose file, then rollback ==="
# The containers run on NEW (tag imgupd-test/pg:16) since 10b. The radar
# offers the running version's own tag, 16.15: both lines (db, app) change.
docker tag "$NEW" imgupd-test/pg:16.15
SUM0=$(sum_of "$D/docker-compose.yml")
offer 16.15
DRY=$(python3 $H retag imgupd-test-db 16.15 --dry-run)
FP=$(jget "$DRY" 'd["retag"]["fingerprint"]')
N=$(jget "$DRY" '(d["retag"]["possible"], len(d["retag"]["changes"]), d["retag"]["choice"])')
check "retag --dry-run: possible, two lines (db and app), a pin" '[ "$N" = "(True, 2, '"'"'pin'"'"')" ]'
python3 $H retag imgupd-test-db 16.15 --expect "$(printf '%064d' 0)" --no-dump > /tmp/e2e-12.log 2>&1; RC=$?
check "retag with another fingerprint: exit 5, file unchanged" '[ $RC = 5 ] && [ "$(sum_of "$D/docker-compose.yml")" = "$SUM0" ]'
python3 $H retag imgupd-test-db 16.15 --expect "$FP" --no-dump > /tmp/e2e-13.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-13.log
R6=$(result_of /tmp/e2e-13.log)
OP6=$(jget "$R6" 'd["op"]')
S6=$(jget "$R6" '(d["status"], d["image"])')
BK6=$(jget "$R6" 'd["backup"]')
check "retag: exit 0, the new tag in the result" '[ $RC = 0 ] && [ "$S6" = "('"'"'ok'"'"', '"'"'imgupd-test/pg:16.15'"'"')" ]'
check "retag: both containers carry the new tag" \
    '[ "$(cfg_image imgupd-test-db)" = imgupd-test/pg:16.15 ] && [ "$(cfg_image imgupd-test-app)" = imgupd-test/pg:16.15 ]'
check "retag: exactly those two lines changed" \
    '[ "$(grep -c "image: imgupd-test/pg:16.15$" "$D/docker-compose.yml")" = 2 ] && [ "$(sed "s|pg:16.15$|pg:16|" "$D/docker-compose.yml" | sha256sum)" = "$SUM0" ]'
check "retag: the old file kept byte-exact, root only" \
    '[ "$(sum_of "$BK6/1_docker-compose.yml")" = "$SUM0" ] && [ "$(stat -c %a "$BK6")" = 700 ] && [ "$(stat -c %a "$BK6/1_docker-compose.yml")" = 600 ]'
python3 $H rollback "$OP6" > /tmp/e2e-14.log 2>&1; RC=$?
check "retag rollback: exit 0, file byte-identical, containers on the old tag" \
    '[ $RC = 0 ] && [ "$(sum_of "$D/docker-compose.yml")" = "$SUM0" ] && [ "$(cfg_image imgupd-test-db)" = imgupd-test/pg:16 ] && [ "$(cfg_image imgupd-test-app)" = imgupd-test/pg:16 ]'

echo "=== 14. Tag change through a variable of the .env file, then rollback ==="
printf 'PG_TAG=16  # the e2e tag\n' > "$D/.env"
sed -i 's|image: imgupd-test/pg:16$|image: imgupd-test/pg:${PG_TAG}|' "$D/docker-compose.yml"
docker compose -p "$P" -f "$D/docker-compose.yml" up -d --force-recreate --wait 2>&1 | tail -n 2
SUM1=$(sum_of "$D/docker-compose.yml")
ENV0=$(sum_of "$D/.env")
offer 16.15
DRY=$(python3 $H retag imgupd-test-db 16.15 --dry-run)
FP=$(jget "$DRY" 'd["retag"]["fingerprint"]')
VAR=$(jget "$DRY" '(d["retag"]["possible"], d["retag"]["variable"], len(d["retag"]["changes"]), d["retag"]["reason"])')
check "retag --dry-run via .env: one line, the variable" '[ "$VAR" = "(True, '"'"'PG_TAG'"'"', 1, None)" ]'
python3 $H retag imgupd-test-db 16.15 --expect "$FP" --no-dump > /tmp/e2e-15.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-15.log
OP7=$(jget "$(result_of /tmp/e2e-15.log)" 'd["op"]')
check "retag via .env: exit 0, the .env line changed, the compose file not" \
    '[ $RC = 0 ] && grep -q "^PG_TAG=16.15  # the e2e tag$" "$D/.env" && [ "$(sum_of "$D/docker-compose.yml")" = "$SUM1" ]'
check "retag via .env: the containers carry the new tag" '[ "$(cfg_image imgupd-test-app)" = imgupd-test/pg:16.15 ]'
python3 $H rollback "$OP7" > /tmp/e2e-16.log 2>&1; RC=$?
check "retag via .env rollback: .env byte-identical, containers on the old tag" \
    '[ $RC = 0 ] && [ "$(sum_of "$D/.env")" = "$ENV0" ] && [ "$(cfg_image imgupd-test-app)" = imgupd-test/pg:16 ]'

echo "=== 15. Tag change refused: a shared variable, an anchor ==="
sed -i 's|image: imgupd-test/side:1$|image: imgupd-test/side:${PG_TAG}|' "$D/docker-compose.yml"
offer 16.15
REF=$(jget "$(python3 $H retag imgupd-test-db 16.15 --dry-run)" '(d["retag"]["possible"], d["retag"]["reason"])')
check "retag: a variable also used by another image -> refused" '[ "$REF" = "(False, '"'"'variable-shared'"'"')" ]'
rm -f "$D/.env"
write_compose "sleep infinity"
python3 - "$D/docker-compose.yml" <<'PY'
import sys
p = sys.argv[1]
text = open(p).read()
head = "x-pg: &pg\n  image: imgupd-test/pg:16\n\n"
text = text.replace("    image: imgupd-test/pg:16\n", "    <<: *pg\n")
open(p, "w").write(head + text)
PY
SUM2=$(sum_of "$D/docker-compose.yml")
REF=$(jget "$(python3 $H retag imgupd-test-db 16.15 --dry-run)" '(d["retag"]["possible"], d["retag"]["reason"])')
check "retag: the image under a YAML anchor -> refused, file unchanged" \
    '[ "$REF" = "(False, '"'"'anchors'"'"')" ] && [ "$(sum_of "$D/docker-compose.yml")" = "$SUM2" ]'
write_compose "sleep infinity"

echo "=== 16. Tag change in an override file, then rollback ==="
# The override file gives db and app their image; the base file's older lines
# are overridden and must stay as they are.
sed -i 's|image: imgupd-test/pg:16$|image: imgupd-test/pg:base|' "$D/docker-compose.yml"
printf 'services:\n  db:\n    image: imgupd-test/pg:16\n  app:\n    image: imgupd-test/pg:16\n' > "$D/docker-compose.override.yml"
docker compose -p "$P" -f "$D/docker-compose.yml" -f "$D/docker-compose.override.yml" up -d --force-recreate --wait 2>&1 | tail -n 2
SUMB=$(sum_of "$D/docker-compose.yml")
SUMO=$(sum_of "$D/docker-compose.override.yml")
offer 16.15
DRY=$(python3 $H retag imgupd-test-db 16.15 --dry-run)
FP=$(jget "$DRY" 'd["retag"]["fingerprint"]')
OV=$(jget "$DRY" '(d["retag"]["possible"], sorted({c["file"].rsplit("/", 1)[1] for c in d["retag"]["changes"]}), len(d["retag"]["changes"]))')
check "retag --dry-run with an override file: its two lines, none of the base file" \
    '[ "$OV" = "(True, ['"'"'docker-compose.override.yml'"'"'], 2)" ]'
python3 $H retag imgupd-test-db 16.15 --expect "$FP" --no-dump > /tmp/e2e-17.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-17.log
OP8=$(jget "$(result_of /tmp/e2e-17.log)" 'd["op"]')
check "retag with an override file: exit 0, its lines changed, the base file not, the new tag runs" \
    '[ $RC = 0 ] && [ "$(grep -c "image: imgupd-test/pg:16.15$" "$D/docker-compose.override.yml")" = 2 ] && [ "$(sum_of "$D/docker-compose.yml")" = "$SUMB" ] && [ "$(cfg_image imgupd-test-db)" = imgupd-test/pg:16.15 ] && [ "$(cfg_image imgupd-test-app)" = imgupd-test/pg:16.15 ]'
python3 $H rollback "$OP8" > /tmp/e2e-18.log 2>&1; RC=$?
check "retag with an override file, rollback: the override byte-identical, containers on the old tag" \
    '[ $RC = 0 ] && [ "$(sum_of "$D/docker-compose.override.yml")" = "$SUMO" ] && [ "$(sum_of "$D/docker-compose.yml")" = "$SUMB" ] && [ "$(cfg_image imgupd-test-db)" = imgupd-test/pg:16 ]'
rm -f "$D/docker-compose.override.yml"
write_compose "sleep infinity"

echo
[ $FAIL = 0 ] && echo "E2E: ALL CHECKS PASSED" || echo "E2E: FAILURES"

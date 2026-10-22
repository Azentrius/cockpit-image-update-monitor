#!/bin/bash
# shellcheck disable=SC2034
# SPDX-License-Identifier: Apache-2.0
# End-to-end test of diun-update.py on a real Docker host, inside the
# throw-away compose projects "imgupd-test", "imgupd-test-my" and
# "imgupd-test-reg". No other container, compose file, volume or dump is
# touched; everything created here is removed on exit, also after a failure or
# an interrupt. Run it on a throw-away host or VM, never next to production
# containers: the CI workflow runs it on a fresh GitHub runner.
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
# The database major (stage 5) runs in a project of its own, "imgupd-test-major":
# PostgreSQL 16 (OLD_IMAGE) to 17 (PG17_IMAGE, default postgres:17-alpine) with
# test data, the rollback and the release of the kept data, and majors stopped
# by SIGTERM and SIGKILL while they move the data (test/e2e-pause.py holds the
# helper after a rename until the signal is sent), each followed by the rollback.
# The rebuild of an image built on the server runs in "imgupd-test-build": a
# Dockerfile on a base image in the local registry, which gets a newer build;
# one service names its image, one does not (compose calls it <project>-<service>).
# Pulls that work, fail or find a newer image already here run against a local
# registry (REGISTRY_IMAGE, default registry:2.8.3, on 127.0.0.1 at
# E2E_REGISTRY_PORT, default 5000); the MariaDB/MySQL dumps use it too. Every
# dump the helper writes is restored into a fresh container of its own and
# checked table by table, object by object (README, "Restoring a dump").
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
trap 'exit 130' INT TERM HUP
LIB=${E2E_LIB:-/usr/lib/cockpit-image-updates}
HERE=$(cd "$(dirname "$0")" && pwd)
H=$LIB/diun-update.py
INV=$LIB/diun-inventory.py
VERSIONS=/var/lib/diun-radar/versions.json
STATUS=/var/lib/diun-radar/status.json
P=imgupd-test
P2=imgupd-test-my
P3=imgupd-test-reg
P4=imgupd-test-major
P5=imgupd-test-build
D=${E2E_DIR:-/root/imgupd-test}
D2=$D-my
D3=$D-reg
D4=$D-major
D5=$D-build
REG_PORT=${E2E_REGISTRY_PORT:-5000}
REG=localhost:$REG_PORT
REGISTRY_IMAGE=${REGISTRY_IMAGE:-registry:2.8.3}
LOCKF=/run/lock/diun-update.lock
BK_DIR=/var/backups/image-updates
DUMPS=$BK_DIR/dumps
OPS=$BK_DIR/operations.jsonl
LOGS=$BK_DIR/logs
export DIUN_LANG=en

[ "$(id -u)" = 0 ] || { echo "ABORT: run as root"; exit 1; }
[ -x "$H" ] || { echo "ABORT: $H missing (install first)"; exit 1; }
for p in "$P" "$P2" "$P3" "$P4" "$P5"; do
    if [ -n "$(docker ps -aq --filter label=com.docker.compose.project="$p")" ]; then
        echo "ABORT: compose project $p exists already"; exit 1
    fi
done
if [ -n "$(docker ps -aq --filter name='^imgupd-test-')" ]; then
    echo "ABORT: containers named imgupd-test-* exist already"; exit 1
fi
OLD_IMAGE=${OLD_IMAGE:-postgres:16.14-alpine}
NEW_IMAGE=${NEW_IMAGE:-postgres:16.15-alpine}
MARIADB_IMAGE=${MARIADB_IMAGE:-mariadb:11}
MYSQL_IMAGE=${MYSQL_IMAGE:-mysql:8}
PG17_IMAGE=${PG17_IMAGE:-postgres:17-alpine}
E2E_MYSQL=${E2E_MYSQL:-1}
IMAGES="$OLD_IMAGE $NEW_IMAGE $REGISTRY_IMAGE $PG17_IMAGE"
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
# true once "$@" succeeds, false after $1 seconds (no endless waits)
wait_for() {
    local end=$(( $(date +%s) + $1 )); shift
    until "$@" >/dev/null 2>&1; do
        [ "$(date +%s)" -ge "$end" ] && return 1
        sleep 1
    done
}
registry_up() { python3 -c 'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=2)' "http://127.0.0.1:$REG_PORT/v2/"; }

# The Postgres test data as one text: rows, view, function, index, sequence,
# role and grant. A restore is good only if it gives exactly this again.
pg_state() {
    docker exec "$1" psql -U postgres -d postgres -At -c "select string_agg(x, ',' order by x) from e2e" &&
    docker exec "$1" psql -U postgres -d e2e_app -At \
        -c "select string_agg(id || '|' || name || '|' || coalesce(price::text, 'NULL'), ';' order by id) from items" \
        -c "select item_count()" \
        -c "select string_agg(name, ',' order by name) from expensive" \
        -c "select string_agg(indexname, ',' order by indexname) from pg_indexes where tablename = 'items'" \
        -c "select last_value from items_id_seq" \
        -c "select has_table_privilege('imgupd_reader', 'items', 'select')" \
        -c "select rolcanlogin from pg_roles where rolname = 'imgupd_reader'"
}
# The same for MariaDB / MySQL ($2: the client, mariadb or mysql).
my_state() {
    docker exec "$1" "$2" -uroot -ptest-only -N -B -e "
        select group_concat(concat(id, '|', x, '|', ifnull(n, 'NULL')) order by id separator ';') from e2e.t;
        select e2e.f();
        select group_concat(x order by x) from e2e.v;
        select group_concat(concat(routine_type, ':', routine_name) order by routine_name)
          from information_schema.routines where routine_schema = 'e2e';
        select group_concat(trigger_name) from information_schema.triggers where trigger_schema = 'e2e';
        select group_concat(event_name) from information_schema.events where event_schema = 'e2e';
        select count(*) from mysql.user where user = 'imgupd_reader';" 2>/dev/null
}
# A dump ($1) into a fresh Postgres container ($2) of the image the test ran
# on, as README "Restoring a dump" says. Fails on any psql error but the one
# the dump always gives in a new instance: the role postgres exists already.
restore_pg() {
    docker run -d --name "$2" -e POSTGRES_PASSWORD=restore-only "$OLD_IMAGE" > /dev/null || return 1
    wait_for 120 sh -c "docker logs $2 2>&1 | grep -q 'init process complete' && docker exec $2 pg_isready -U postgres -q" ||
        { echo "no fresh Postgres"; return 1; }
    zcat "$1" | docker exec -i "$2" psql -U postgres -d postgres -q > /dev/null 2> "/tmp/e2e-$2.err"
    cat "/tmp/e2e-$2.err"
    ! grep -v 'role "postgres" already exists' "/tmp/e2e-$2.err" | grep -q ERROR
}
# A dump ($1) into a fresh MariaDB / MySQL container ($2, image $3, client $4,
# $5 MARIADB or MYSQL) with the root password of the dumped one, as the README
# says. The client stops at the first error with exit 1.
restore_my() {
    docker run -d --name "$2" -e "$5_ROOT_PASSWORD=test-only" "$3" > /dev/null || return 1
    wait_for 240 sh -c "docker logs $2 2>&1 | grep -q 'init process done' && docker exec $2 $4 -uroot -ptest-only -e 'select 1'" ||
        { echo "no fresh $4"; return 1; }
    zcat "$1" | docker exec -i "$2" sh -c "exec $4 -uroot -p\"\$$5_ROOT_PASSWORD\"" || return 1
    docker exec "$2" "$4" -uroot -ptest-only -e "flush privileges" 2>/dev/null
}
# Half a dump: what a broken copy or a full disk leaves.
half_dump() { zcat "$1" | head -c "$(( $(zcat "$1" | wc -c) / 2 ))" | gzip > "$2"; }
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
    local rc=$?     # the test's own result: anything it leaves behind turns a pass into a failure
    local n_c n_t n_d n_l n_b n_v
    trap '' INT TERM HUP
    echo "=== Cleanup ==="
    [ -n "${HOLDER:-}" ] && kill "$HOLDER" 2>/dev/null
    docker compose -p "$P" -f "$D/docker-compose.yml" down -v --remove-orphans >/dev/null 2>&1
    docker compose -p "$P2" -f "$D2/docker-compose.yml" down -v --remove-orphans >/dev/null 2>&1
    docker compose -p "$P3" -f "$D3/docker-compose.yml" down -v --remove-orphans >/dev/null 2>&1
    docker compose -p "$P4" -f "$D4/docker-compose.yml" down -v --remove-orphans >/dev/null 2>&1
    docker compose -p "$P5" -f "$D5/docker-compose.yml" down -v --remove-orphans >/dev/null 2>&1
    for v in $(docker volume ls -q --filter name='^imgupd-test-major_'); do
        docker volume rm "$v" >/dev/null 2>&1 && echo "removed volume $v"
    done
    for c in $(docker ps -aq --filter name='^imgupd-test-'); do
        docker rm -f -v "$c" >/dev/null 2>&1
    done
    # Docker refuses to drop the LAST tag of an image that running containers
    # use (e.g. other databases on the same build). -f then only removes our
    # tag - the image itself stays because it is in use.
    for t in $(docker image ls --format '{{.Repository}}:{{.Tag}}' | grep -v ':<none>$' |
               grep -E '(^|/)imgupd-test/|^imgupd-test-build-'); do
        if docker rmi "$t" >/dev/null 2>&1; then
            echo "untagged $t"
        elif [ -n "$(docker ps -q --filter ancestor="$(docker image inspect -f '{{.Id}}' "$t")")" ]; then
            docker rmi -f "$t" >/dev/null && echo "untagged $t (last tag, image still running)"
        else
            echo "WARNING: $t not removed"
        fi
    done
    # An image a build pulled keeps its repository by digest once its tag has
    # moved on (listed as localhost:5000/imgupd-test/postgres:<none>; the list
    # does not give that digest). Those references come from the images' own
    # RepoDigests and go one by one, the image itself only with its last one.
    for r in $(docker image ls -aq | sort -u | xargs -r docker image inspect -f '{{range .RepoDigests}}{{println .}}{{end}}' |
               grep -E '(^|/)imgupd-test/|^imgupd-test-build-' | sort -u); do
        if docker rmi "$r" >/dev/null 2>&1; then
            echo "removed $r"
        else
            echo "WARNING: $r not removed"
        fi
    done
    rm -rf "${DUMPS:?}/${P:?}" "${DUMPS:?}/${P2:?}" "${DUMPS:?}/${P4:?}" "${DUMPS:?}/${P5:?}" "${BK_DIR:?}/compose/${P:?}" "${BK_DIR:?}/compose/${P4:?}"
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
    # Every update wrote status.json: write it again without the test project.
    if [ -f "$STATUS" ]; then
        python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import diunstatus; diunstatus.refresh()' "$LIB" \
            && echo "status.json: written again without the test project"
    fi
    rm -rf "${D:?}" "${D2:?}" "${D3:?}" "${D4:?}" "${D5:?}" /tmp/e2e-hold.fifo /tmp/e2e-half-*.sql.gz /tmp/e2e-ready /tmp/e2e-go
    n_c=$(docker ps -a --format '{{.Names}}' | grep -c imgupd)
    n_t=$(docker image ls --format '{{.Repository}}' | grep -cE '(^|/)imgupd-test/|^imgupd-test-build-')
    n_d=$(find "$DUMPS" -maxdepth 1 -name 'imgupd-test*' 2>/dev/null | wc -l)
    n_l=$(find "$LOGS" -maxdepth 1 -name '*-imgupd-test-*' 2>/dev/null | wc -l)
    n_b=$(find "$BK_DIR/compose" -maxdepth 1 -name 'imgupd-test*' 2>/dev/null | wc -l)
    n_v=$(docker volume ls -q --filter name='^imgupd-test-major_' | wc -l)
    echo "Left over: $n_c containers, $n_t tags, $n_d test dump folders, $n_l test logs, $n_b test file backups," \
         "$n_v test volumes"
    if [ "$rc" = 0 ] && [ $((n_c + n_t + n_d + n_l + n_b + n_v)) != 0 ]; then
        echo "E2E: FAILURES (the test left something behind)"
        exit 1
    fi
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
# More than a table: a second database, a role with a grant, a view, a
# function, an index and a sequence. The restore must bring them all back.
docker exec imgupd-test-db psql -U postgres -q -v ON_ERROR_STOP=1 \
    -c "create role imgupd_reader login password 'reader-only'" -c "create database e2e_app"
docker exec imgupd-test-db psql -U postgres -d e2e_app -q -v ON_ERROR_STOP=1 -c "
    create table items(id serial primary key, name text not null, price numeric(10,2));
    insert into items(name, price) values ('Äpfel und Birnen', 1.50), ('kiwi', null), ('mango', 3.25);
    create index items_name on items(name);
    create view expensive as select name from items where price > 2;
    create function item_count() returns bigint language sql as 'select count(*) from items';
    grant select on items to imgupd_reader;"
PG_STATE0=$(pg_state imgupd-test-db)
echo "Postgres test data: $PG_STATE0" | tr '\n' ' '; echo
check "Postgres test data in place" '[ "$(echo "$PG_STATE0" | wc -l)" = 8 ] && echo "$PG_STATE0" | grep -q "^1|Äpfel und Birnen|1.50;2|kiwi|NULL;3|mango|3.25$"'
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
HCAUSE=$(OP=$OP1 python3 -c 'import json,os; o={}; [o.update(e) for e in map(json.loads, open("'"$OPS"'")) if e.get("id")==os.environ["OP"]]; print((o.get("reason") or {}).get("line"))')
check "Most likely cause in operations.jsonl" '[ "$HCAUSE" = "Error: e2e app failure" ]'
check "Output kept: root only, without @@RESULT" \
    '[ "$(stat -c %a "$LOGS/$OP1.log")" = 600 ] && grep -q "Most likely cause" "$LOGS/$OP1.log" && ! grep -q "^@@RESULT" "$LOGS/$OP1.log"'
NDIG=$(OP=$OP1 python3 -c 'import json,os; o={}; [o.update(e) for e in map(json.loads, open("'"$OPS"'")) if e.get("id")==os.environ["OP"]]; print(len(o.get("new_digests") or []))')
check "Registry digests of the new build in the history ($NDIG)" '[ "$NDIG" -ge 1 ]'
check "The pull of a tag only this host has failed, the newer image here was rolled out with a warning" \
    'grep -q "Warning: the pull failed" /tmp/e2e-1.log'

echo "=== 3a. The status for the Status Board after the failed update ==="
# The update wrote status.json anew; `status` reads it, also without root.
check "status.json written after the update, readable by all (644), without the project's folder" \
    '[ "$(stat -c %a "$STATUS")" = 644 ] && ! grep -q "$D" "$STATUS"'
ST=$(python3 $H status --lang en)
SF=$(jget "$ST" '(d["schema"], d["state"], [(i["state"], "imgupd-test-db" in i.get("detail", "")) for i in d["items"] if i["id"] == "failed"])')
check "status as root: the failed update as danger: $SF" '[ "$SF" = "(1, '"'"'danger'"'"', [('"'"'danger'"'"', True)])" ]'
if runuser -u nobody -- test -r "$H"; then
    STN=$(runuser -u nobody -- python3 "$H" status --lang de); RC=$?
    SN=$(jget "$STN" '(d["state"], [i["title"] for i in d["items"] if i["id"] == "failed"])')
    check "status without root: exit 0, German, the failure from the file: $SN" \
        '[ $RC = 0 ] && [ "$SN" = "('"'"'danger'"'"', ['"'"'Fehlgeschlagene Updates'"'"'])" ]'
else
    echo "SKIP: status without root ($H is not readable for nobody)"
fi

echo "=== 3b. Restore the dump into a fresh Postgres, as the README says ==="
restore_pg "$DUMPS/$DUMP" imgupd-test-restore-pg > /tmp/e2e-restore-pg.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-restore-pg.log
check "Postgres restore: no psql error but the role postgres that exists already" '[ $RC = 0 ]'
check "Postgres restore: rows, view, function, index, sequence, role and grant as before" \
    '[ "$(pg_state imgupd-test-restore-pg)" = "$PG_STATE0" ]'
half_dump "$DUMPS/$DUMP" /tmp/e2e-half-pg.sql.gz
restore_pg /tmp/e2e-half-pg.sql.gz imgupd-test-restore-pg-half > /dev/null 2>&1; RC=$?
check "Postgres restore of half the dump: caught (psql error or other data)" \
    '[ $RC != 0 ] || [ "$(pg_state imgupd-test-restore-pg-half)" != "$PG_STATE0" ]'
docker rm -f -v imgupd-test-restore-pg imgupd-test-restore-pg-half > /dev/null 2>&1

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
FD=$(jget "$(python3 $H status --lang en)" '[i.get("detail", "") for i in d["items"] if i["id"] == "failed"][0]')
check "status after the rollback: the update counts as failed no more ($FD)" \
    'case "$FD" in *imgupd-test-db*) false ;; *) true ;; esac'

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
check "The local-only tag cannot be pulled: the newer image here rolled out with a warning" \
    'grep -q "Warning: the pull failed" /tmp/e2e-4.log'
check "Containers now on NEW" '[ "$(img_of imgupd-test-db)" = "$NEW" ] && [ "$(img_of imgupd-test-app)" = "$NEW" ]'
check "Test data there after the update" 'docker exec imgupd-test-db psql -U postgres -Atc "select x from e2e" | grep -q hello-from-e2e'
RB2=$(jget "$R2" 'd["rollback"]')
VERS=$(jget "$R2" '(d["from_version"], d["to_version"])')
WANT_VERS="('$(pg_version "$OLD_IMAGE")', '$(pg_version "$NEW_IMAGE")')"
check "Versions in the result: $VERS" '[ "$VERS" = "$WANT_VERS" ]'
HVERS=$(OP=$(jget "$R2" 'd["op"]') python3 -c 'import json,os; o={}; [o.update(e) for e in map(json.loads, open("'"$OPS"'")) if e.get("id")==os.environ["OP"]]; print((o["from_version"], o["to_version"]))')
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

echo "=== 6. Again: the tag only this host has cannot be pulled, nothing newer here ==="
# Up to 0.18.0 this was "nochange": the pull ran with --ignore-pull-failures.
# A pull that fails says nothing about newer images: exit 5, nothing changed.
OPS_N=$(wc -l < "$OPS")
python3 $H update imgupd-test-db --no-dump > /tmp/e2e-5.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-5.log
check "Pull failed, nothing newer: exit 5, said so, no history entry" \
    '[ $RC = 5 ] && grep -qF "The pull failed and there is no newer image on the server – nothing changed." /tmp/e2e-5.log && [ "$(wc -l < "$OPS")" = "$OPS_N" ]'
check "No leftover rollback tag" '[ "$(docker image ls --format "{{.Tag}}" imgupd-test/pg | grep -c rollback)" = 2 ]'
check "Containers still on NEW" '[ "$(img_of imgupd-test-db)" = "$NEW" ] && [ "$(img_of imgupd-test-app)" = "$NEW" ]'

echo "=== 7. Lock: an update while another process holds it ==="
# A holder takes the lock and says so; no race with the speed of a pull.
rm -f /tmp/e2e-hold.fifo /tmp/e2e-held.txt
mkfifo /tmp/e2e-hold.fifo
python3 -c 'import fcntl, sys; f = open(sys.argv[1], "w"); fcntl.flock(f, fcntl.LOCK_EX); print("held", flush=True); sys.stdin.read()' \
    "$LOCKF" < /tmp/e2e-hold.fifo > /tmp/e2e-held.txt &
HOLDER=$!
exec 8> /tmp/e2e-hold.fifo
wait_for 30 grep -q held /tmp/e2e-held.txt
python3 $H update imgupd-test-db --no-dump > /tmp/e2e-7.log 2>&1; RC=$?
exec 8>&-
wait "$HOLDER"
HOLDER=
check "An update while the lock is held gets 75" '[ $RC = 75 ] && grep -q "already running" /tmp/e2e-7.log'
python3 $H update imgupd-test-db --no-dump > /tmp/e2e-7b.log 2>&1; RC=$?
check "Lock released: the next update runs (exit 5 again, nothing newer)" '[ $RC = 5 ]'

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

echo "=== 10c. Pulls against a local registry ==="
docker run -d --name imgupd-test-registry -p "127.0.0.1:$REG_PORT:5000" "$REGISTRY_IMAGE" > /dev/null
wait_for 60 registry_up
check "Local registry up on 127.0.0.1:$REG_PORT" 'registry_up'
docker tag "$OLD" "$REG/imgupd-test/reg:16"
docker push -q "$REG/imgupd-test/reg:16" > /dev/null
if [ "$E2E_MYSQL" = 1 ]; then
    docker tag "$MARIADB_IMAGE" "$REG/imgupd-test/mariadb:11"
    docker tag "$MYSQL_IMAGE" "$REG/imgupd-test/mysql:8"
    docker push -q "$REG/imgupd-test/mariadb:11" > /dev/null
    docker push -q "$REG/imgupd-test/mysql:8" > /dev/null
fi
mkdir -p "$D3"
cat > "$D3/docker-compose.yml" <<EOF
name: $P3
services:
  app:
    image: $REG/imgupd-test/reg:16
    container_name: imgupd-test-reg
    entrypoint: ["sh", "-c"]
    command: ["sleep infinity"]
EOF
docker compose -p "$P3" -f "$D3/docker-compose.yml" up -d --wait 2>&1 | tail -n 2
reg_rollbacks() { docker image ls --format '{{.Tag}}' "$REG/imgupd-test/reg" | grep -c rollback; }
python3 $H update imgupd-test-reg --no-dump > /tmp/e2e-reg1.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-reg1.log
S=$(jget "$(result_of /tmp/e2e-reg1.log)" 'd["status"]')
check "The pull works, the registry has the same image: nochange, exit 0, no rollback tag left" \
    '[ $RC = 0 ] && [ "$S" = nochange ] && [ "$(reg_rollbacks)" = 0 ]'
docker stop imgupd-test-registry > /dev/null
OPS_N=$(wc -l < "$OPS")
python3 $H update imgupd-test-reg --no-dump > /tmp/e2e-reg2.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-reg2.log
check "Registry down, nothing newer here: exit 5, said so, nothing changed" \
    '[ $RC = 5 ] && grep -qF "The pull failed and there is no newer image on the server – nothing changed." /tmp/e2e-reg2.log && [ "$(wc -l < "$OPS")" = "$OPS_N" ] && [ "$(reg_rollbacks)" = 0 ] && [ "$(img_of imgupd-test-reg)" = "$OLD" ]'
docker tag "$NEW" "$REG/imgupd-test/reg:16"
python3 $H update imgupd-test-reg --no-dump > /tmp/e2e-reg3.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-reg3.log
S=$(jget "$(result_of /tmp/e2e-reg3.log)" 'd["status"]')
check "Registry down, a newer image here: rolled out with a warning" \
    '[ $RC = 0 ] && [ "$S" = ok ] && grep -q "Warning: the pull failed" /tmp/e2e-reg3.log && [ "$(img_of imgupd-test-reg)" = "$NEW" ]'
docker start imgupd-test-registry > /dev/null
wait_for 60 registry_up
docker push -q "$REG/imgupd-test/reg:16" > /dev/null
python3 $H update imgupd-test-reg --no-dump > /tmp/e2e-reg4.log 2>&1; RC=$?
S=$(jget "$(result_of /tmp/e2e-reg4.log)" 'd["status"]')
check "Registry back with the new image: nochange" '[ $RC = 0 ] && [ "$S" = nochange ]'

echo "=== 10d. Update in two steps: pull only, then roll out ==="
# The registry gets the other image under the same tag; the local tag stays on what runs.
docker tag "$OLD" "$REG/imgupd-test/reg:16"
docker push -q "$REG/imgupd-test/reg:16" > /dev/null
docker tag "$NEW" "$REG/imgupd-test/reg:16"
OPS_N=$(wc -l < "$OPS")
# 10c keeps the rollback copy of its update with a warning; pull only adds none.
RB_N=$(reg_rollbacks)
python3 $H pull imgupd-test-reg > /tmp/e2e-reg5.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-reg5.log
S=$(jget "$(result_of /tmp/e2e-reg5.log)" 'd["status"]')
TAGID=$(docker image inspect -f '{{.Id}}' "$REG/imgupd-test/reg:16")
check "Pull only: exit 0, pulled, the tag on the other image, the container unchanged, no history, no new rollback tag" \
    '[ $RC = 0 ] && [ "$S" = pulled ] && [ "$TAGID" = "$OLD" ] && [ "$(img_of imgupd-test-reg)" = "$NEW" ] && [ "$(wc -l < "$OPS")" = "$OPS_N" ] && [ "$(reg_rollbacks)" = "$RB_N" ]'
python3 $H update imgupd-test-reg --no-dump > /tmp/e2e-reg6.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-reg6.log
S=$(jget "$(result_of /tmp/e2e-reg6.log)" 'd["status"]')
check "Then the roll-out: exit 0, ok, the container on the pulled image" \
    '[ $RC = 0 ] && [ "$S" = ok ] && [ "$(img_of imgupd-test-reg)" = "$OLD" ]'

if [ "$E2E_MYSQL" = 1 ]; then
    echo "=== 11. MariaDB + MySQL: dumps with test data ==="
    mkdir -p "$D2"
    cat > "$D2/docker-compose.yml" <<EOF
name: $P2
services:
  mariadb:
    image: $REG/imgupd-test/mariadb:11
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
    image: $REG/imgupd-test/mysql:8
    container_name: imgupd-test-mysql
    environment:
      MYSQL_ROOT_PASSWORD: test-only
      MYSQL_DATABASE: e2e
    healthcheck:
      test: ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 -uroot -ptest-only --silent"]
      interval: 2s
      retries: 90
EOF
    docker compose -p "$P2" -f "$D2/docker-compose.yml" up -d --wait 2>&1 | tail -n 3
    for db in mariadb mysql; do
        docker exec "imgupd-test-$db" "$db" -uroot -ptest-only e2e -e "
            create table t(id int primary key auto_increment, x varchar(100) not null, n decimal(10,2) null);
            insert into t(x, n) values ('hello-from-$db', 1.50), ('second', null);
            create view v as select x from t where n is not null;
            create function f() returns int deterministic reads sql data return (select count(*) from t);
            create procedure p() select count(*) from t;
            create trigger tr before insert on t for each row set new.x = trim(new.x);
            create event ev on schedule every 1 day do delete from t where id < 0;
            create user 'imgupd_reader'@'%' identified by 'reader-only';
            grant select on e2e.* to 'imgupd_reader'@'%';" 2>/dev/null
    done
    MD_STATE0=$(my_state imgupd-test-mariadb mariadb)
    MY_STATE0=$(my_state imgupd-test-mysql mysql)
    echo "MariaDB test data: $MD_STATE0" | tr '\n' ' '; echo
    echo "MySQL test data: $MY_STATE0" | tr '\n' ' '; echo
    check "MariaDB and MySQL test data in place (rows, view, function, procedure, trigger, event, user)" \
        '[ "$(echo "$MD_STATE0" | wc -l)" = 7 ] && [ "$(echo "$MY_STATE0" | wc -l)" = 7 ] && echo "$MD_STATE0" | grep -q "^1|hello-from-mariadb|1.50;2|second|NULL$" && echo "$MY_STATE0" | grep -q "^FUNCTION:f,PROCEDURE:p$"'
    PLAN=$(python3 $H plan imgupd-test-mariadb)
    KINDS=$(jget "$PLAN" 'sorted((x["name"], x["kind"]) for x in d["dbs"])')
    check "Plan: both databases as mysql" '[ "$KINDS" = "[('"'"'imgupd-test-mariadb'"'"', '"'"'mysql'"'"'), ('"'"'imgupd-test-mysql'"'"', '"'"'mysql'"'"')]" ]'
    # The registry has the same images: the dumps run, then "nothing newer"; the dumps stay.
    python3 $H update imgupd-test-mariadb > /tmp/e2e-10.log 2>&1; RC=$?
    sed 's/^/  | /' /tmp/e2e-10.log
    R4=$(result_of /tmp/e2e-10.log)
    S4=$(jget "$R4" '(d["status"], len(d["dumps"]))')
    check "Update with dumps: exit 0, 2 dumps" '[ $RC = 0 ] && [ "$S4" = "('"'"'nochange'"'"', 2)" ]'
    MD=$(jget "$R4" '[x for x in d["dumps"] if "_imgupd-test-mariadb" in x][0]')
    MY=$(jget "$R4" '[x for x in d["dumps"] if "_imgupd-test-mysql" in x][0]')
    check "MariaDB dump: test data + end marker" 'zcat "$DUMPS/$MD" | grep -q hello-from-mariadb && zcat "$DUMPS/$MD" | tail -n 3 | grep -q "^-- Dump completed"'
    check "MySQL dump: test data + end marker" 'zcat "$DUMPS/$MY" | grep -q hello-from-mysql && zcat "$DUMPS/$MY" | tail -n 3 | grep -q "^-- Dump completed"'

    echo "=== 11b. Restore both dumps into fresh MariaDB and MySQL containers, as the README says ==="
    restore_my "$DUMPS/$MD" imgupd-test-restore-mariadb "$REG/imgupd-test/mariadb:11" mariadb MARIADB > /tmp/e2e-restore-md.log 2>&1; RC=$?
    sed 's/^/  | /' /tmp/e2e-restore-md.log
    check "MariaDB restore: no error, everything as before" \
        '[ $RC = 0 ] && [ "$(my_state imgupd-test-restore-mariadb mariadb)" = "$MD_STATE0" ]'
    restore_my "$DUMPS/$MY" imgupd-test-restore-mysql "$REG/imgupd-test/mysql:8" mysql MYSQL > /tmp/e2e-restore-my.log 2>&1; RC=$?
    sed 's/^/  | /' /tmp/e2e-restore-my.log
    check "MySQL restore: no error, everything as before" \
        '[ $RC = 0 ] && [ "$(my_state imgupd-test-restore-mysql mysql)" = "$MY_STATE0" ]'
    half_dump "$DUMPS/$MD" /tmp/e2e-half-md.sql.gz
    restore_my /tmp/e2e-half-md.sql.gz imgupd-test-restore-md-half "$REG/imgupd-test/mariadb:11" mariadb MARIADB > /dev/null 2>&1; RC=$?
    check "MariaDB restore of half the dump: caught (client error or other data)" \
        '[ $RC != 0 ] || [ "$(my_state imgupd-test-restore-md-half mariadb)" != "$MD_STATE0" ]'
    docker rm -f -v imgupd-test-restore-mariadb imgupd-test-restore-mysql imgupd-test-restore-md-half > /dev/null 2>&1

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

echo "=== 17. Database major: PostgreSQL 16 to 17 by dump and restore, rollback, release ==="
# A project of its own: the database (its own user and database, a named volume)
# and an app that waits for it. Test data in two databases, an extension, a role.
V17=$(pg_version "$PG17_IMAGE")
docker tag "$OLD" imgupd-test/postgres:16
docker tag "$PG17_IMAGE" "imgupd-test/postgres:$V17"
docker tag "$OLD" imgupd-test/pgm-app:1
mkdir -p "$D4"
cat > "$D4/docker-compose.yml" <<EOF
name: $P4
services:
  db:
    image: imgupd-test/postgres:16
    container_name: imgupd-test-major-db
    environment:
      POSTGRES_USER: owner
      POSTGRES_PASSWORD: test-only
      POSTGRES_DB: appdb
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U owner -d appdb"]
      interval: 2s
      retries: 60
  app:
    image: imgupd-test/pgm-app:1
    container_name: imgupd-test-major-app
    entrypoint: ["sh", "-c"]
    command: ["sleep infinity"]
    depends_on:
      db:
        condition: service_healthy
volumes:
  pgdata:
EOF
docker compose -p "$P4" -f "$D4/docker-compose.yml" up -d --wait 2>&1 | tail -n 2
MDB=imgupd-test-major-db
q() { docker exec "$MDB" psql -X -U owner -d "$1" -At -c "$2"; }
q appdb "create table notes(id int primary key, t text); insert into notes select g, md5(g::text) from generate_series(1, 300) g;
         create role reader login password 'test-only'; grant select on notes to reader;" > /dev/null
q appdb "create database shop" > /dev/null
q shop "create extension pgcrypto; create table items(id serial primary key, name text, h text);
        insert into items(name, h) select 'item ' || g, encode(digest(g::text, 'sha256'), 'hex') from generate_series(1, 5000) g;
        create table empty_one(x int);" > /dev/null
DATA() { echo "$(q appdb "select count(*), md5(string_agg(t, ',' order by id)) from notes")/$(q shop "select count(*), md5(string_agg(h, ',' order by id)) from items")/$(q appdb "select count(*) from pg_roles where rolname = 'reader'")"; }
DATA0=$(DATA)
SUMM=$(sum_of "$D4/docker-compose.yml")
python3 - "$VERSIONS" "$(pg_version "$OLD")" "$V17" <<'PY'
import json, os, sys, tempfile
p, version, target = sys.argv[1:4]
try:
    d = json.load(open(p))
except (OSError, ValueError):
    d = {"images": []}
d["images"] = [r for r in d.get("images") or [] if r.get("image") != "imgupd-test/postgres"]
d["images"].append({"image": "imgupd-test/postgres", "tag": "16", "db": True, "containers": ["imgupd-test-major-db"],
                    "versions": {version: {"status": "major", "choices": {
                        "pin": None, "lines": [], "newer": [{"tag": target, "level": "major"}], "floating": []}}}})
os.makedirs(os.path.dirname(p), exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
with os.fdopen(fd, "w") as f:
    json.dump(d, f, ensure_ascii=False, indent=1)
os.chmod(tmp, 0o644)
os.replace(tmp, p)
PY
# A role with an MD5 password would not get in after the major (the new image
# allows scram-sha-256 over the network only): the plan refuses until it is set again.
q appdb "set password_encryption = 'md5'; create role legacy login password 'old-only'" > /dev/null
DRY=$(python3 $H dbmajor "$MDB" "$V17" --dry-run)
check "dbmajor --dry-run with a role that has an MD5 password: refused, the role named" \
    '[ "$(jget "$DRY" "(d[\"major\"][\"possible\"], d[\"major\"][\"reason\"], d[\"major\"][\"detail\"])")" = "(False, '"'"'major-md5'"'"', '"'"'legacy'"'"')" ]'
q appdb "set password_encryption = 'scram-sha-256'; alter role legacy password 'new-only'" > /dev/null
# The image's name ends in "postgres": the helper reads the installed version from PG_VERSION.
DRY=$(python3 $H dbmajor "$MDB" "$V17" --dry-run)
echo "  | plan: $(jget "$DRY" '(d["installed_version"], d["retag"]["reason"], d["major"]["possible"], d["major"]["reason"], d["major"]["detail"])')"
MJ=$(jget "$DRY" '(d["major"]["possible"], d["major"]["from_major"], d["major"]["to_major"], d["major"]["data"]["name"], d["major"]["stop"], d["major"]["extensions"])')
check "dbmajor --dry-run: possible, 16 to 17, the named volume, the app stopped meanwhile, pgcrypto" \
    '[ "$MJ" = "(True, 16, 17, '"'"'imgupd-test-major_pgdata'"'"', ['"'"'app'"'"'], ['"'"'pgcrypto'"'"'])" ]'
FPM=$(jget "$DRY" 'd["major"]["fingerprint"]')
python3 $H dbmajor "$MDB" "$V17" --expect "$(printf '%064d' 0)" > /tmp/e2e-m0.log 2>&1; RC=$?
check "dbmajor with another fingerprint: exit 5, nothing changed" \
    '[ $RC = 5 ] && [ "$(sum_of "$D4/docker-compose.yml")" = "$SUMM" ] && [ "$(q appdb "show server_version_num" | cut -c1-2)" = 16 ]'
python3 $H dbmajor "$MDB" "$V17" --expect "$FPM" > /tmp/e2e-m1.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-m1.log
RM=$(result_of /tmp/e2e-m1.log)
OPM=$(jget "$RM" 'd["op"]')
SM=$(jget "$RM" '(d["status"], d["image"])')
KEPT16=$(docker volume ls -q --filter name='^imgupd-test-major_pgdata-pg16-')
check "dbmajor: exit 0, ok, PostgreSQL 17 runs with the same data (counts, checksums, the role)" \
    '[ $RC = 0 ] && [ "$SM" = "('"'"'ok'"'"', '"'"'imgupd-test/postgres:'"$V17"''"'"')" ] && [ "$(q appdb "show server_version_num" | cut -c1-2)" = 17 ] && [ "$(DATA)" = "$DATA0" ]'
check "dbmajor: the tag line changed, the old data kept in a volume of its own (PostgreSQL 16)" \
    '[ "$(grep -c "image: imgupd-test/postgres:$V17$" "$D4/docker-compose.yml")" = 1 ] && [ -n "$KEPT16" ] && [ "$(cat "$(docker volume inspect -f "{{.Mountpoint}}" "$KEPT16")/PG_VERSION")" = 16 ]'
# The trial run comes before the data moves (its line before "… is now in …").
check "dbmajor: the trial run restored the dump into PostgreSQL 17 first, the login over TCP checked, no probe left" \
    'grep -q "Trial run: restored into PostgreSQL 17 and counted – the same." /tmp/e2e-m1.log && grep -q "Login over TCP with the password from the environment: OK." /tmp/e2e-m1.log && [ "$(grep -n "Trial run: restored" /tmp/e2e-m1.log | cut -d: -f1)" -lt "$(grep -n "is now in" /tmp/e2e-m1.log | head -n1 | cut -d: -f1)" ] && [ -z "$(docker ps -aq --filter label=image-updates.probe)" ]'
check "dbmajor: the app runs again, the extension is there, the history knows the run" \
    '[ "$(docker inspect -f "{{.State.Running}}" imgupd-test-major-app)" = true ] && [ "$(q shop "select extversion is not null from pg_extension where extname = '"'"'pgcrypto'"'"'")" = t ] && grep -q "\"id\": \"$OPM\".*\"kind\": \"dbmajor\"" "$OPS"'
BKM=$(python3 $H backups)
KM=$(jget "$BKM" '[ (o["kept"]["major"], o["kept"]["can_release"], o["can_rollback"]) for o in d["ops"] if o["id"] == "'"$OPM"'"]')
check "backups: the kept data of PostgreSQL 16, to release or to roll back to" '[ "$KM" = "[(16, True, True)]" ]'
python3 $H rollback "$OPM" > /tmp/e2e-m2.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-m2.log
KEPT17=$(docker volume ls -q --filter name='^imgupd-test-major_pgdata-pg17-')
check "dbmajor rollback: exit 0, PostgreSQL 16 with the old data, the file byte-identical, the app runs" \
    '[ $RC = 0 ] && [ "$(q appdb "show server_version_num" | cut -c1-2)" = 16 ] && [ "$(DATA)" = "$DATA0" ] && [ "$(sum_of "$D4/docker-compose.yml")" = "$SUMM" ] && [ "$(docker inspect -f "{{.State.Running}}" imgupd-test-major-app)" = true ]'
check "dbmajor rollback: the old data back in place, the new major's kept (PostgreSQL 17)" \
    '[ -z "$(docker volume ls -q --filter name="^$KEPT16\$")" ] && [ -n "$KEPT17" ] && [ "$(cat "$(docker volume inspect -f "{{.Mountpoint}}" "$KEPT17")/PG_VERSION")" = 17 ]'
python3 $H delete-olddata "$OPM" > /tmp/e2e-m3.log 2>&1; RC=$?
check "delete-olddata after the rollback: the new major's data released" \
    '[ $RC = 0 ] && [ -z "$(docker volume ls -q --filter name="^imgupd-test-major_pgdata-pg17-")" ]'

# Majors stopped while they move the data aside. test/e2e-pause.py runs the
# installed helper and holds it after a rename in the data volume until the
# signal is sent: a synchronisation point on the real data, no timing.
pause_run() {   # $1 log, $2 the rename to stop after, $3 the signal, the rest: the helper's arguments
    local log=$1 at=$2 sig=$3 pid
    shift 3
    rm -f /tmp/e2e-ready /tmp/e2e-go
    mkfifo /tmp/e2e-ready /tmp/e2e-go
    python3 "$HERE/e2e-pause.py" "$LIB" "$at" /volumes/imgupd-test-major_pgdata/ /tmp/e2e-ready /tmp/e2e-go "$@" > "$log" 2>&1 &
    pid=$!
    if [ "$(timeout 600 head -c1 /tmp/e2e-ready)" = r ]; then
        kill -s "$sig" "$pid"
        [ "$sig" = KILL ] || timeout 60 sh -c 'printf g > /tmp/e2e-go'
    fi
    wait "$pid"
}
op_json() { python3 -c 'import json, sys
ops = {}
for line in open(sys.argv[1]):
    e = json.loads(line)
    ops.setdefault(e["id"], {}).update(e)
print(json.dumps(ops.get(sys.argv[2], {})))' "$OPS" "$1"; }
last_major() { python3 -c 'import json, sys
ids = [json.loads(line)["id"] for line in open(sys.argv[1]) if "\"kind\": \"dbmajor\"" in line]
print(max(ids) if ids else "")' "$OPS"; }
# The entries of a data directory (postmaster.pid only while PostgreSQL runs).
top() { python3 -c 'import os, sys; print(" ".join(sorted(set(os.listdir(sys.argv[1])) - {"postmaster.pid"})))' "$1"; }
VOLDIR=$(docker volume inspect -f '{{.Mountpoint}}' imgupd-test-major_pgdata)
TOP0=$(top "$VOLDIR")
running() { docker inspect -f '{{.State.Running}}' "$1"; }

echo "=== 17b. Database major stopped by SIGTERM while it moves the data aside, then the rollback ==="
DRY=$(python3 $H dbmajor "$MDB" "$V17" --dry-run)
pause_run /tmp/e2e-m7.log 1 TERM dbmajor "$MDB" "$V17" --expect "$(jget "$DRY" 'd["major"]["fingerprint"]')"; RC=$?
sed 's/^/  | /' /tmp/e2e-m7.log | tail -n 6
OP7=$(jget "$(result_of /tmp/e2e-m7.log)" 'd["op"]')
J7=$(op_json "$OP7")
K7=$(jget "$J7" '(d.get("old_data") or {}).get("name") or ""')
check "SIGTERM while the major moves the data: the signal waited for the move and its record, exit 4, failed, no move open" \
    '[ $RC = 4 ] && grep -q "@@SIGNAL-WHILE-HELD True" /tmp/e2e-m7.log && [ "$(jget "$J7" "(d[\"status\"], d.get(\"move\"))")" = "('"'"'failed'"'"', None)" ] && [ -n "$K7" ]'
check "SIGTERM: all of the old data in the volume the history names, the data volume empty, the database stopped" \
    '[ -z "$(ls -A "$VOLDIR")" ] && [ "$(top "$(docker volume inspect -f "{{.Mountpoint}}" "$K7")")" = "$TOP0" ] && [ "$(running "$MDB")" = false ]'
python3 $H rollback "$OP7" > /tmp/e2e-m8.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-m8.log | tail -n 4
check "SIGTERM, then the rollback: exit 0, PostgreSQL 16 on all of its data, no kept volume left, the app runs" \
    '[ $RC = 0 ] && [ "$(q appdb "show server_version_num" | cut -c1-2)" = 16 ] && [ "$(DATA)" = "$DATA0" ] && [ "$(top "$VOLDIR")" = "$TOP0" ] && [ -z "$(docker volume ls -q --filter name="^imgupd-test-major_pgdata-pg16-")" ] && [ "$(running imgupd-test-major-app)" = true ]'

echo "=== 17c. Database major killed (SIGKILL) while it moves the data aside: the rollback finds the move ==="
DRY=$(python3 $H dbmajor "$MDB" "$V17" --dry-run)
pause_run /tmp/e2e-m9.log 2 KILL dbmajor "$MDB" "$V17" --expect "$(jget "$DRY" 'd["major"]["fingerprint"]')"; RC=$?
OP9=$(last_major)
J9=$(op_json "$OP9")
K9=$(jget "$J9" '(d.get("old_data") or {}).get("name") or ""')
check "SIGKILL while the major moves the data: killed, 'running' in the history with the move open, the data split, nothing started" \
    '[ $RC = 137 ] && [ "$OP9" != "$OP7" ] && [ "$(jget "$J9" "(d[\"status\"], (d.get(\"move\") or {}).get(\"step\"))")" = "('"'"'running'"'"', '"'"'aside'"'"')" ] && [ -n "$K9" ] && [ -n "$(ls -A "$VOLDIR")" ] && [ -n "$(ls -A "$(docker volume inspect -f "{{.Mountpoint}}" "$K9")")" ] && [ "$(running "$MDB")" = false ]'
python3 $H rollback "$OP9" > /tmp/e2e-m10.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-m10.log | tail -n 4
check "SIGKILL, then the rollback: it finishes the move back, exit 0, PostgreSQL 16 on all of its data, no kept volume left" \
    '[ $RC = 0 ] && [ "$(q appdb "show server_version_num" | cut -c1-2)" = 16 ] && [ "$(DATA)" = "$DATA0" ] && [ "$(top "$VOLDIR")" = "$TOP0" ] && [ -z "$(docker volume ls -q --filter name="^imgupd-test-major_pgdata-pg16-")" ] && [ "$(running imgupd-test-major-app)" = true ]'
DRY=$(python3 $H dbmajor "$MDB" "$V17" --dry-run)
python3 $H dbmajor "$MDB" "$V17" --expect "$(jget "$DRY" 'd["major"]["fingerprint"]')" > /tmp/e2e-m4.log 2>&1; RC=$?
OPM2=$(jget "$(result_of /tmp/e2e-m4.log)" 'd["op"]')
python3 $H delete-olddata "$OPM2" > /tmp/e2e-m5.log 2>&1; RC2=$?
check "dbmajor again, then delete-olddata: PostgreSQL 17 with the data, the old data released" \
    '[ $RC = 0 ] && [ $RC2 = 0 ] && [ "$(q appdb "show server_version_num" | cut -c1-2)" = 17 ] && [ "$(DATA)" = "$DATA0" ] && [ -z "$(docker volume ls -q --filter name="^imgupd-test-major_pgdata-pg16-")" ]'
python3 $H delete-olddata "$OPM2" > /tmp/e2e-m6.log 2>&1; RC=$?
check "delete-olddata twice: exit 2, nothing kept any more" '[ $RC = 2 ]'
CR=$(jget "$(python3 $H backups)" '[(o["can_rollback"], "kept" in o) for o in d["ops"] if o["id"] == "'"$OPM2"'"]')
check "backups after the release: no rollback offered any more, nothing kept" '[ "$CR" = "[(False, False)]" ]'

echo "=== 18. Rebuild of an image built on the server: a newer base from the registry, rollback ==="
# The base is OLD in the local registry; the build on it runs. Then the registry
# gets NEW under the same tag while the local tag stays on OLD: only a build
# with --pull brings NEW.
wait_for 60 registry_up
BASE_REF="$REG/imgupd-test/postgres:16"
docker tag "$OLD" "$BASE_REF"
docker push -q "$BASE_REF" > /dev/null
mkdir -p "$D5"
printf 'FROM %s\nLABEL de.azentrius.e2e=rebuild\n' "$BASE_REF" > "$D5/Dockerfile"
cat > "$D5/docker-compose.yml" <<EOF
name: $P5
services:
  app:
    build: .
    image: imgupd-test/built:1
    container_name: imgupd-test-build
    entrypoint: ["sh", "-c"]
    command: ["sleep infinity"]
  plain:
    build: .
    container_name: imgupd-test-plain
    entrypoint: ["sh", "-c"]
    command: ["sleep infinity"]
EOF
docker compose -p "$P5" -f "$D5/docker-compose.yml" up -d --build --wait 2>&1 | tail -n 2
B1=$(img_of imgupd-test-build)
check "Built on the server, on the base OLD" '[ "$(pg_version "$B1")" = "$(pg_version "$OLD")" ]'
check "An update refuses the built image" '[ "$(jget "$(python3 $H plan imgupd-test-build)" "d[\"reason\"]")" = built-locally ]'
docker tag "$NEW" "$BASE_REF"
docker push -q "$BASE_REF" > /dev/null
docker tag "$OLD" "$BASE_REF"
# The radar names the base (it cannot read the Dockerfile under /root itself).
for _ in $(seq 120); do systemctl is-active --quiet diun-radar.service || break; sleep 1; done
python3 - "$VERSIONS" "$REG/imgupd-test/postgres" "$(pg_version "$OLD")" "$(pg_version "$NEW")" <<'PY'
import json, os, sys, tempfile
p, base, version, now = sys.argv[1:5]
try:
    d = json.load(open(p))
except (OSError, ValueError):
    d = {"images": []}
d["images"] = [r for r in d.get("images") or [] if r.get("image") != "imgupd-test/built"]
d["images"].append({"image": "imgupd-test/built", "tag": "1", "status": "unlisted", "containers": ["imgupd-test-build"],
                    "base": [{"image": base, "tag": "16", "final": True, "version": version, "now": now, "newer": True}]})
os.makedirs(os.path.dirname(p), exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
with os.fdopen(fd, "w") as f:
    json.dump(d, f, ensure_ascii=False, indent=1)
os.chmod(tmp, 0o644)
os.replace(tmp, p)
PY
DRY=$(python3 $H rebuild imgupd-test-build --dry-run)
echo "  | plan: $(jget "$DRY" '(d["updatable"], d["reason"], d["build"], d["installed_version"], d["target_version"], d["jump"])')"
check "rebuild --dry-run: possible, the versions of the base" \
    '[ "$(jget "$DRY" "(d[\"updatable\"], d[\"build\"], d[\"installed_version\"], d[\"target_version\"])")" = "(True, True, '"'"'$(pg_version "$OLD")'"'"', '"'"'$(pg_version "$NEW")'"'"')" ]'
# A build that fails changes nothing: no history, the tag and the container as they were.
cp "$D5/Dockerfile" "$D5/Dockerfile.good"
echo 'RUN echo "e2e: this build fails" && false' >> "$D5/Dockerfile"
OPS_N=$(wc -l < "$OPS")
python3 $H rebuild imgupd-test-build --no-dump > /tmp/e2e-b1.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-b1.log | tail -n 6
check "A failing build: exit 5, said so, no history, the tag and the container unchanged, no rollback tag" \
    '[ $RC = 5 ] && grep -qF "The rebuild failed – nothing changed." /tmp/e2e-b1.log && [ "$(wc -l < "$OPS")" = "$OPS_N" ] && [ "$(docker image inspect -f "{{.Id}}" imgupd-test/built:1)" = "$B1" ] && [ "$(img_of imgupd-test-build)" = "$B1" ] && [ "$(docker image ls --format "{{.Tag}}" imgupd-test/built | grep -c rollback)" = 0 ]'
mv "$D5/Dockerfile.good" "$D5/Dockerfile"
python3 $H rebuild imgupd-test-build > /tmp/e2e-b2.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-b2.log
RB=$(result_of /tmp/e2e-b2.log)
OPB=$(jget "$RB" 'd["op"]')
B2=$(img_of imgupd-test-build)
check "rebuild: exit 0, ok, the container on a new build on the base NEW (pulled by the build)" \
    '[ $RC = 0 ] && [ "$(jget "$RB" "d[\"status\"]")" = ok ] && [ "$B2" != "$B1" ] && [ "$(pg_version "$B2")" = "$(pg_version "$NEW")" ]'
check "rebuild: the versions of the base in the result, a rebuild in the history, the old build as rollback copy" \
    '[ "$(jget "$RB" "(d[\"from_version\"], d[\"to_version\"])")" = "('"'"'$(pg_version "$OLD")'"'"', '"'"'$(pg_version "$NEW")'"'"')" ] && grep -q "\"id\": \"$OPB\".*\"kind\": \"rebuild\"" "$OPS" && [ "$(docker image inspect -f "{{.Id}}" "$(jget "$RB" "d[\"rollback\"]")")" = "$B1" ]'
check "rebuild: the output names the rebuild and its success" \
    'grep -q "Rebuilding imgupd-test/built:1 in project imgupd-test-build: services app" /tmp/e2e-b2.log && grep -q "Rebuild successful." /tmp/e2e-b2.log'
CRB=$(jget "$(python3 $H backups)" '[o["can_rollback"] for o in d["ops"] if o["id"] == "'"$OPB"'"]')
check "backups: the rebuild can be rolled back" '[ "$CRB" = "[True]" ]'
python3 $H rollback "$OPB" > /tmp/e2e-b3.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-b3.log | tail -n 4
check "rebuild rolled back: exit 0, the old build runs again" '[ $RC = 0 ] && [ "$(img_of imgupd-test-build)" = "$B1" ]'

echo "=== 18b. Rebuild and rollback of a build without image: (compose names it imgupd-test-build-plain) ==="
CFG5=$(docker compose -p "$P5" -f "$D5/docker-compose.yml" config --format json)
check "compose config gives the service no image, compose runs it as imgupd-test-build-plain" \
    '[ "$(jget "$CFG5" "\"image\" in d[\"services\"][\"plain\"]")" = False ] && [ "$(cfg_image imgupd-test-plain)" = imgupd-test-build-plain ]'
C1=$(img_of imgupd-test-plain)
python3 $H rebuild imgupd-test-plain --no-dump > /tmp/e2e-b4.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-b4.log | tail -n 4
RP=$(result_of /tmp/e2e-b4.log)
OPP=$(jget "$RP" 'd["op"]')
C2=$(img_of imgupd-test-plain)
check "rebuild without image: exit 0, ok, the container on a new build on the base NEW" \
    '[ $RC = 0 ] && [ "$(jget "$RP" "d[\"status\"]")" = ok ] && [ "$C2" != "$C1" ] && [ "$(pg_version "$C2")" = "$(pg_version "$NEW")" ]'
python3 $H rollback "$OPP" > /tmp/e2e-b5.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-b5.log | tail -n 4
check "rollback without image: exit 0, the container on the original image ID again" \
    '[ $RC = 0 ] && [ "$(img_of imgupd-test-plain)" = "$C1" ] && [ "$(docker image inspect -f "{{.Id}}" imgupd-test-build-plain)" = "$C1" ]'

echo
if [ "$FAIL" = 0 ]; then echo "E2E: ALL CHECKS PASSED"; else echo "E2E: FAILURES"; fi
exit "$FAIL"

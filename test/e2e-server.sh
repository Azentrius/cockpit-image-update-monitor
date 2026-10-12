#!/bin/bash
# End-to-end test of diun-update.py on a real Docker host, inside a
# throw-away compose project "imgupd-test". No other container, compose file
# or volume is touched; everything created here is removed on exit.
#
# Needs: root, the helpers installed in /usr/local/lib/diun-radar, docker
# compose, and two DIFFERENT builds of the same postgres major:
#   OLD_IMAGE  (default postgres:16.14-alpine)  -> "installed"
#   NEW_IMAGE  (default postgres:16.15-alpine)  -> "pulled, not rolled out"
# Missing images are pulled. Both are tagged locally as imgupd-test/pg:16;
# re-tagging that name to NEW_IMAGE creates the "pulled" state without any
# registry access during the test.
#
#   sudo OLD_IMAGE=postgres:16.14-alpine NEW_IMAGE=postgres:16.15-alpine bash test/e2e-server.sh
set -u
H=/usr/local/lib/diun-radar/diun-update.py
INV=/usr/local/lib/diun-radar/diun-inventory.py
P=imgupd-test
D=${E2E_DIR:-/root/imgupd-test}
OPS=/var/backups/image-updates/operations.jsonl
export DIUN_LANG=de

[ "$(id -u)" = 0 ] || { echo "ABBRUCH: bitte als root ausführen"; exit 1; }
[ -x "$H" ] || { echo "ABBRUCH: $H fehlt (erst installieren)"; exit 1; }
if [ -n "$(docker ps -aq --filter label=com.docker.compose.project=$P)" ]; then
    echo "ABBRUCH: Compose-Projekt $P existiert bereits"; exit 1
fi
OLD_IMAGE=${OLD_IMAGE:-postgres:16.14-alpine}
NEW_IMAGE=${NEW_IMAGE:-postgres:16.15-alpine}
for i in "$OLD_IMAGE" "$NEW_IMAGE"; do
    docker image inspect "$i" >/dev/null 2>&1 || docker pull -q "$i" >/dev/null || { echo "ABBRUCH: $i nicht verfügbar"; exit 1; }
done
OLD=$(docker image inspect -f '{{.Id}}' "$OLD_IMAGE")
NEW=$(docker image inspect -f '{{.Id}}' "$NEW_IMAGE")
FAIL=0
check() { if eval "$2"; then echo "PASS: $1"; else echo "FAIL: $1"; FAIL=1; fi; }
result_of() { grep '^@@RESULT ' "$1" | tail -n1 | cut -c10-; }
jget() { python3 -c "import sys,json; d=json.loads(sys.argv[1]); print(eval(sys.argv[2]))" "$1" "$2"; }
img_of() { docker inspect -f '{{.Image}}' "$1"; }

cleanup() {
    echo "=== Aufräumen ==="
    docker compose -p $P -f $D/docker-compose.yml down -v --remove-orphans >/dev/null 2>&1
    # Docker refuses to drop the LAST tag of an image that running containers
    # use (e.g. other databases on the same build). -f then only removes our
    # tag - the image itself stays because it is in use.
    for t in $(docker image ls --format '{{.Repository}}:{{.Tag}}' | grep '^imgupd-test/pg:'); do
        if docker rmi "$t" >/dev/null 2>&1; then
            echo "untagged $t"
        elif [ -n "$(docker ps -q --filter ancestor="$(docker image inspect -f '{{.Id}}' "$t")")" ]; then
            docker rmi -f "$t" >/dev/null && echo "untagged $t (letzter Tag, Image läuft weiter)"
        else
            echo "WARNUNG: $t nicht entfernt"
        fi
    done
    rm -rf /var/backups/image-updates/dumps/$P
    if [ -f $OPS ]; then
        python3 - "$OPS" <<'PY'
import json, os, sys
p = sys.argv[1]
keep = [l for l in open(p) if not json.loads(l).get("id", "").endswith("-imgupd-test-db")]
if keep:
    open(p, "w").writelines(keep)
else:
    os.unlink(p)
print("operations.jsonl:", len(keep), "fremde Einträge behalten")
PY
    fi
    rm -rf $D
    echo "Reste: $(docker ps -a --format '{{.Names}}' | grep -c imgupd) Container," \
         "$(docker image ls | grep -c imgupd-test) Tags," \
         "$(ls /var/backups/image-updates/dumps | wc -l) Dump-Ordner"
}
trap cleanup EXIT

write_compose() {   # $1 = shell command of the "app" service
    cat > $D/docker-compose.yml <<EOF
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
EOF
}

echo "OLD=$OLD"
echo "NEW=$NEW"
[ "$OLD" != "$NEW" ] || { echo "ABBRUCH: kein altes/neues Image-Paar"; exit 1; }

echo "=== 1. Testprojekt auf dem alten Image starten ==="
mkdir -p $D
write_compose "sleep infinity"
docker tag "$OLD" imgupd-test/pg:16
docker compose -p $P -f $D/docker-compose.yml up -d --wait 2>&1 | tail -n 3
docker exec imgupd-test-db psql -U postgres -qc "create table e2e(x text); insert into e2e values ('hello-from-e2e');"
check "Container laufen auf OLD" '[ "$(img_of imgupd-test-db)" = "$OLD" ] && [ "$(img_of imgupd-test-app)" = "$OLD" ]'

echo "=== 2. Neueres Image 'ziehen' (Tag umhängen) + Compose ändern (app bricht ab) ==="
docker tag "$NEW" imgupd-test/pg:16
write_compose "exit 3"
PULLED=$(python3 $INV | python3 -c 'import sys,json; r=[json.loads(l) for l in sys.stdin if json.loads(l)["name"]=="imgupd-test-db"][0]; print(r["pulled"], r["managed"])')
check "Inventar meldet pulled + managed" '[ "$PULLED" = "True True" ]'
PLAN=$(python3 $H plan imgupd-test-db)
echo "plan: $PLAN"
PCHK=$(jget "$PLAN" '(d["services"], d["config_changed"], d["pulled"], [x["name"] for x in d["dbs"]]) == (["app", "db"], True, True, ["imgupd-test-db"])')
check "Plan: beide Services, Config geändert, pulled, DB erkannt" '[ "$PCHK" = True ]'

echo "=== 3. Update mit Dump – muss scheitern (app exit 3) ==="
python3 $H update imgupd-test-db > /tmp/e2e-1.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-1.log
R1=$(result_of /tmp/e2e-1.log)
OP1=$(jget "$R1" 'd["op"]')
S1=$(jget "$R1" 'd["status"]'); RB1=$(jget "$R1" 'd["rollback"]'); DUMP=$(jget "$R1" 'd["dumps"][0]')
check "Exit 4 + status failed" '[ $RC = 4 ] && [ "$S1" = failed ]'
check "Dump enthält Testdaten" 'zcat /var/backups/image-updates/dumps/$DUMP | grep -q hello-from-e2e'
check "Dump nur für root lesbar (600)" '[ "$(stat -c %a /var/backups/image-updates/dumps/$DUMP)" = 600 ]'
check "Rollback-Tag existiert" 'docker image inspect "$RB1" >/dev/null 2>&1'

echo "=== 4. Compose reparieren, Rollback ==="
write_compose "sleep infinity"
python3 $H rollback "$OP1" > /tmp/e2e-2.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-2.log
check "Rollback exit 0" '[ $RC = 0 ]'
check "Container wieder auf OLD" '[ "$(img_of imgupd-test-db)" = "$OLD" ] && [ "$(img_of imgupd-test-app)" = "$OLD" ]'
check "Testdaten noch da" 'docker exec imgupd-test-db psql -U postgres -Atc "select x from e2e" | grep -q hello-from-e2e'
python3 $H rollback "$OP1" > /tmp/e2e-3.log 2>&1; RC=$?
check "Zweiter Rollback abgelehnt (3)" '[ $RC = 3 ]'

echo "=== 5. Erneut ausrollen ohne Dump – muss klappen ==="
docker tag "$NEW" imgupd-test/pg:16
python3 $H update imgupd-test-db --no-dump > /tmp/e2e-4.log 2>&1; RC=$?
sed 's/^/  | /' /tmp/e2e-4.log
R2=$(result_of /tmp/e2e-4.log)
S2=$(jget "$R2" '(d["status"], len(d["dumps"]))')
check "Exit 0 + status ok, kein Dump" '[ $RC = 0 ] && [ "$S2" = "('"'"'ok'"'"', 0)" ]'
check "Container jetzt auf NEW" '[ "$(img_of imgupd-test-db)" = "$NEW" ] && [ "$(img_of imgupd-test-app)" = "$NEW" ]'
check "Testdaten nach Update da" 'docker exec imgupd-test-db psql -U postgres -Atc "select x from e2e" | grep -q hello-from-e2e'
RB2=$(jget "$R2" 'd["rollback"]')

echo "=== 6. Nochmal – nichts Neueres ==="
python3 $H update imgupd-test-db --no-dump > /tmp/e2e-5.log 2>&1; RC=$?
R3=$(result_of /tmp/e2e-5.log)
S3=$(jget "$R3" 'd["status"]')
check "nochange, Exit 0" '[ $RC = 0 ] && [ "$S3" = nochange ]'
check "Keine Rollback-Tag-Leiche" '[ "$(docker image ls --format "{{.Tag}}" imgupd-test/pg | grep -c rollback)" = 2 ]'

echo "=== 7. Sperre: zwei Updates gleichzeitig ==="
( python3 $H update imgupd-test-db --no-dump > /tmp/e2e-6.log 2>&1 ) &
sleep 0.5
python3 $H update imgupd-test-db --no-dump > /tmp/e2e-7.log 2>&1; RC=$?
wait
check "Zweites Update bekommt 75" '[ $RC = 75 ]'

echo "=== 8. Backups-Übersicht ==="
BK=$(python3 $H backups)
echo "$BK" | python3 -m json.tool | head -n 60
B1=$(jget "$BK" '(len([x for x in d["dumps"] if x["project"]=="imgupd-test"]), len([x for x in d["images"] if x["ref"].startswith("imgupd-test/")]))')
B2=$(OP1="$OP1" jget "$BK" '[(o.get("rolled_back"), o["can_rollback"]) for o in d["ops"] if o["id"]==__import__("os").environ["OP1"]][0]')
B3=$(RB2="$RB2" jget "$BK" '[o["can_rollback"] for o in d["ops"] if o.get("rollback")==__import__("os").environ["RB2"]][0]')
check "1 Dump, 2 Rollback-Images" '[ "$B1" = "(1, 2)" ]'
check "Verlauf: OP1 zurückgerollt, nicht mehr rollbar" '[ "$B2" = "(True, False)" ]'
check "Verlauf: zweites Update rollbar" '[ "$B3" = True ]'

echo "=== 9. Löschen ==="
python3 $H delete-dump "$DUMP"; RC=$?
check "Dump gelöscht" '[ $RC = 0 ] && [ ! -e /var/backups/image-updates/dumps/$DUMP ]'
python3 $H delete-dump "imgupd-test/../../../etc/20261012-000000_passwd.sql.gz" >/dev/null; RC=$?
check "Pfad-Trick abgelehnt (64)" '[ $RC = 64 ]'
python3 $H delete-image "$RB2" >/dev/null; RC=$?
check "Rollback-Image entfernt" '[ $RC = 0 ] && ! docker image inspect "$RB2" >/dev/null 2>&1'
python3 $H delete-image "imgupd-test/pg:16" >/dev/null; RC=$?
check "Normales Image abgelehnt (64)" '[ $RC = 64 ]'

echo
[ $FAIL = 0 ] && echo "E2E: ALLE CHECKS OK" || echo "E2E: FEHLER"

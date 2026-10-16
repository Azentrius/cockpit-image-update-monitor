#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Dependency check / installer for Image Update Monitor, the Cockpit add-on.
#
# Called by the plugin via cockpit.spawn with a FIXED set of arguments only:
#   diun-deps.sh check            one JSON object on stdout (read-only)
#   diun-deps.sh install skopeo   install skopeo with apt-get or dnf
#   diun-deps.sh install diun     deploy Diun + read-only socket proxy to /opt/diun
#   diun-deps.sh start diun       start the existing /opt/diun stack
#   diun-deps.sh install radar    enable diun-radar.timer and run the radar once
#   diun-deps.sh install cleanup  enable diun-cleanup.timer
# Everything else is rejected. Existing files are never overwritten.
#
# Lives in /usr/lib/cockpit-image-updates (root-owned, from the package
# cockpit-image-update-monitor, never mounted into a container), like everything else
# Cockpit executes as root for this plugin. The units come with the package.
#
# Environment:
#   DIUN_LANG=de|en           language of this script's own messages
#   DIUN_DEPS_DRYRUN=1        print commands instead of running them; files
#                             are written to a temp dir and validated there
#   DIUN_DEPS_FORCE_FRESH=1   (dry-run only) pretend /opt/diun does not exist

set -eu
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

LIB=/usr/lib/cockpit-image-updates
DIUN_DIR=/opt/diun
DIUN_DATA=/var/lib/diun
DIUN_IMAGE=crazymax/diun:4.33.0
PROXY_IMAGE=ghcr.io/tecnativa/docker-socket-proxy:v0.4.1
UNIT_DIR=/usr/lib/systemd/system    # the units of the package

DRY=${DIUN_DEPS_DRYRUN:-0}
ROOT=""                      # write prefix; a temp dir in dry-run mode

msg() { if [ "${DIUN_LANG:-en}" = de ]; then printf '%s\n' "$1"; else printf '%s\n' "$2"; fi; }
die() { code=$1; shift; msg "$@" >&2; exit "$code"; }

run() {
    printf '+ %s\n' "$*"
    [ "$DRY" = 1 ] && return 0
    "$@"
}

# write_file PATH MODE  (content on stdin). Refuses to overwrite.
write_file() {
    dest="$ROOT$1"
    if [ -e "$dest" ]; then
        die 3 "Abbruch: $1 existiert bereits – nichts überschrieben." \
              "Aborted: $1 already exists – nothing overwritten."
    fi
    mkdir -p "$(dirname "$dest")"
    cat > "$dest"
    chmod "$2" "$dest"
    printf '  -> %s\n' "$dest"
}

json_str() { printf '"%s"' "$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr -d '\r\n')"; }
json_bool() { if [ "$1" = 1 ]; then printf true; else printf false; fi; }

docker_ok() { timeout 10 docker version --format '{{.Server.Version}}' >/dev/null 2>&1; }
compose_ok() { timeout 10 docker compose version >/dev/null 2>&1; }

# ---------------------------------------------------------------- check ----
do_check() {
    is_root=0; [ "$(id -u)" = 0 ] && is_root=1

    d_state=no-cli; d_ver=""; compose=0
    if command -v docker >/dev/null 2>&1; then
        d_ver=$(timeout 10 docker version --format '{{.Server.Version}}' 2>/dev/null || true)
        if [ -n "$d_ver" ]; then
            d_state=ok
            compose_ok && compose=1
        else
            d_state=no-daemon
        fi
    fi

    managed=0; [ -f "$DIUN_DIR/docker-compose.yml" ] && managed=1
    di_state=missing
    if [ "$d_state" = ok ]; then
        pat='(^|/)crazymax/diun(:|@|$)'
        if timeout 10 docker ps --format '{{.Image}}' 2>/dev/null | grep -Eq "$pat"; then
            di_state=running
        elif timeout 10 docker ps -a --format '{{.Image}}' 2>/dev/null | grep -Eq "$pat" \
             || [ "$managed" = 1 ]; then
            di_state=stopped
        fi
    elif [ "$managed" = 1 ]; then
        di_state=stopped
    fi
    events=0; [ -s "$DIUN_DATA/events.jsonl" ] && events=1
    di_install=0; di_start=0
    if [ "$d_state" = ok ] && [ "$compose" = 1 ]; then
        [ "$di_state" = missing ] && [ "$managed" = 0 ] && di_install=1
        [ "$di_state" = stopped ] && [ "$managed" = 1 ] && di_start=1
    fi

    sk_state=missing; sk_ver=""; sk_install=0
    if command -v skopeo >/dev/null 2>&1; then
        sk_state=ok
        sk_ver=$(skopeo --version 2>/dev/null | awk '{print $3}' || true)
    fi
    if command -v apt-get >/dev/null 2>&1 || command -v dnf >/dev/null 2>&1; then
        sk_install=1
    fi

    # Set up = the timer of the package is enabled.
    ra_state=missing; ra_running=0; ra_install=0
    if [ "$(systemctl is-enabled diun-radar.timer 2>/dev/null || true)" = enabled ]; then
        if [ "$(systemctl is-active diun-radar.timer 2>/dev/null || true)" = active ]; then
            ra_state=active
        else
            ra_state=inactive
        fi
    fi
    # A oneshot run in progress is "activating".
    if [ "$(systemctl is-active diun-radar.service 2>/dev/null || true)" = activating ]; then
        ra_running=1
    fi
    if [ -f "$LIB/diun-versions.py" ] && [ -f "$UNIT_DIR/diun-radar.timer" ] \
       && command -v python3 >/dev/null 2>&1; then
        ra_install=1
    fi

    cl_state=missing; cl_install=0
    if [ "$(systemctl is-enabled diun-cleanup.timer 2>/dev/null || true)" = enabled ]; then
        if [ "$(systemctl is-active diun-cleanup.timer 2>/dev/null || true)" = active ]; then
            cl_state=active
        else
            cl_state=inactive
        fi
    fi
    if [ -f "$LIB/diun-update.py" ] && [ -f "$UNIT_DIR/diun-cleanup.timer" ] \
       && command -v python3 >/dev/null 2>&1; then
        cl_install=1
    fi

    printf '{"root":%s,' "$(json_bool $is_root)"
    printf '"docker":{"state":%s,"version":%s,"compose":%s},' \
        "$(json_str $d_state)" "$(json_str "$d_ver")" "$(json_bool $compose)"
    printf '"diun":{"state":%s,"managed":%s,"events":%s,"can_install":%s,"can_start":%s},' \
        "$(json_str $di_state)" "$(json_bool $managed)" "$(json_bool $events)" \
        "$(json_bool $di_install)" "$(json_bool $di_start)"
    printf '"skopeo":{"state":%s,"version":%s,"can_install":%s},' \
        "$(json_str $sk_state)" "$(json_str "$sk_ver")" "$(json_bool $sk_install)"
    printf '"radar":{"state":%s,"running":%s,"can_install":%s},' \
        "$(json_str $ra_state)" "$(json_bool $ra_running)" "$(json_bool $ra_install)"
    printf '"cleanup":{"state":%s,"can_install":%s}}\n' \
        "$(json_str $cl_state)" "$(json_bool $cl_install)"
}

# ----------------------------------------------------------- installers ----
install_skopeo() {
    if command -v skopeo >/dev/null 2>&1; then
        msg "skopeo ist bereits installiert – nichts zu tun." \
            "skopeo is already installed – nothing to do."
        return 0
    fi
    if command -v apt-get >/dev/null 2>&1; then
        run env DEBIAN_FRONTEND=noninteractive apt-get update
        run env DEBIAN_FRONTEND=noninteractive apt-get install -y skopeo
    elif command -v dnf >/dev/null 2>&1; then
        run dnf install -y skopeo
    else
        die 2 "Kein unterstützter Paketmanager (apt-get oder dnf) gefunden." \
              "No supported package manager (apt-get or dnf) found."
    fi
    msg "skopeo installiert." "skopeo installed."
}

write_notifier() {
    write_file "$DIUN_DATA/diun-notify.sh" 755 <<'EOF'
#!/bin/sh
# Diun script notifier -> append ONE JSON line per event to a JSONL log.
# Runs inside the Diun container (alpine/sh) on each notification.
#
# Concurrency: Diun fires this script in parallel (watch.workers). The whole
# line is composed first, then written with a SINGLE printf == one write()
# syscall; under O_APPEND that append is atomic for lines < PIPE_BUF (4096B),
# so parallel invocations cannot interleave in the log.
set -eu

OUT="${DIUN_EVENTS_FILE:-/data/events.jsonl}"

case "${DIUN_ENTRY_STATUS:-}" in
    new|update) ;;
    *) exit 0 ;;
esac

esc() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\r//g'; }

ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
st=$(esc "${DIUN_ENTRY_STATUS:-}")
hn=$(esc "${DIUN_HOSTNAME:-}")
pv=$(esc "${DIUN_ENTRY_PROVIDER:-}")
im=$(esc "${DIUN_ENTRY_IMAGE:-}")
dg=$(esc "${DIUN_ENTRY_DIGEST:-}")
cr=$(esc "${DIUN_ENTRY_CREATED:-}")
pf=$(esc "${DIUN_ENTRY_PLATFORM:-}")
hl=$(esc "${DIUN_ENTRY_HUBLINK:-}")

mkdir -p "$(dirname "$OUT")"
printf '{"ts":"%s","status":"%s","hostname":"%s","provider":"%s","image":"%s","digest":"%s","created":"%s","platform":"%s","hub_link":"%s"}\n' \
    "$ts" "$st" "$hn" "$pv" "$im" "$dg" "$cr" "$pf" "$hl" >> "$OUT"
EOF
}

install_diun() {
    command -v docker >/dev/null 2>&1 && docker_ok || \
        die 2 "Docker ist nicht erreichbar." "Docker is not reachable."
    compose_ok || die 2 "Docker Compose fehlt." "Docker Compose is missing."
    fresh=0
    [ "$DRY" = 1 ] && [ "${DIUN_DEPS_FORCE_FRESH:-0}" = 1 ] && fresh=1
    if [ "$fresh" = 0 ]; then
        [ -e "$DIUN_DIR" ] && die 3 \
            "Abbruch: $DIUN_DIR existiert bereits – nichts überschrieben." \
            "Aborted: $DIUN_DIR already exists – nothing overwritten."
        if timeout 10 docker ps -a --format '{{.Image}}' | grep -Eq '(^|/)crazymax/diun(:|@|$)'; then
            die 3 "Abbruch: Es gibt bereits einen Diun-Container." \
                  "Aborted: a Diun container already exists."
        fi
    fi
    host=$(hostname -s)

    msg "Schreibe Diun-Konfiguration …" "Writing Diun configuration …"
    sed -e "s|@DIUN_IMAGE@|$DIUN_IMAGE|" -e "s|@PROXY_IMAGE@|$PROXY_IMAGE|" \
        -e "s|@HOSTNAME@|$host|" -e "s|@DIUN_DATA@|$DIUN_DATA|" <<'EOF' | write_file "$DIUN_DIR/docker-compose.yml" 644
name: diun

services:
  # Filtered Docker API. This is the ONLY container that touches the real
  # socket; it exposes a read-only, endpoint-restricted API on an internal
  # network. Port 2375 is never published to the host.
  socket-proxy:
    image: @PROXY_IMAGE@
    container_name: diun-socket-proxy
    environment:
      - CONTAINERS=1   # GET /containers/json + /containers/*/json
      - IMAGES=1       # GET /images/*/json (digest lookups)
      # Defaults keep the rest locked: POST=0 (read-only), only PING/VERSION/
      # EVENTS enabled, every other API section = 0.
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    security_opt:
      - no-new-privileges:true
    mem_limit: 64m
    restart: unless-stopped
    labels:
      - diun.enable=false   # don't let Diun watch the proxy itself

  diun:
    image: @DIUN_IMAGE@
    container_name: diun
    hostname: @HOSTNAME@
    command: serve
    restart: unless-stopped
    depends_on:
      - socket-proxy
    environment:
      - TZ=Europe/Berlin
      - LOG_LEVEL=info
    security_opt:
      - no-new-privileges:true
    volumes:
      - ./diun.yml:/etc/diun/diun.yml:ro
      - @DIUN_DATA@:/data
      # No docker.sock here: Diun reaches the API only through the
      # filtered socket-proxy (tcp://socket-proxy:2375, see diun.yml).
EOF

    write_file "$DIUN_DIR/diun.yml" 644 <<'EOF'
# Diun: Docker Image Update Notifier
# Config precedence: environment variables > this file > defaults.

db:
  path: /data/diun.db

watch:
  workers: 10
  schedule: "0 */6 * * *"     # every 6h; keeps us under Docker Hub anon pull limits
  firstCheckNotif: true       # emit a baseline on the first run so the plugin has data

notif:
  script:
    cmd: "/bin/sh"
    args:
      - "/data/diun-notify.sh"

providers:
  docker:
    endpoint: "tcp://socket-proxy:2375"   # via the filtered proxy, not the raw socket
    watchByDefault: true       # watch every running container
    # To stop watching a specific container later, add a label to it:
    #   diun.enable=false
EOF

    if [ -e "$ROOT$DIUN_DATA/diun-notify.sh" ]; then
        msg "$DIUN_DATA/diun-notify.sh existiert bereits – unverändert." \
            "$DIUN_DATA/diun-notify.sh already exists – left unchanged."
    else
        write_notifier
    fi

    if [ "$DRY" = 1 ]; then
        msg "Prüfe die erzeugte Compose-Datei (docker compose config) …" \
            "Validating the generated compose file (docker compose config) …"
        docker compose -f "$ROOT$DIUN_DIR/docker-compose.yml" config --quiet
        msg "Compose-Datei gültig." "Compose file is valid."
    fi
    msg "Starte Diun …" "Starting Diun …"
    run docker compose -f "$DIUN_DIR/docker-compose.yml" up -d
    msg "Diun läuft. Der erste Abgleich kann einige Minuten dauern." \
        "Diun is running. The first check may take a few minutes."
}

start_diun() {
    [ -f "$DIUN_DIR/docker-compose.yml" ] || die 2 \
        "Kein Diun-Stack in $DIUN_DIR gefunden." "No Diun stack found in $DIUN_DIR."
    run docker compose -f "$DIUN_DIR/docker-compose.yml" up -d
    msg "Diun gestartet." "Diun started."
}

# need_unit NAME: the unit must come with the package.
need_unit() {
    [ -f "$UNIT_DIR/$1" ] || die 2 \
        "Unit $UNIT_DIR/$1 fehlt (Paket cockpit-image-update-monitor neu installieren)." \
        "Unit $UNIT_DIR/$1 is missing (reinstall the package cockpit-image-update-monitor)."
}

install_radar() {
    [ -f "$LIB/diun-versions.py" ] || die 2 \
        "Radar-Skript $LIB/diun-versions.py fehlt." "Radar script $LIB/diun-versions.py is missing."
    command -v python3 >/dev/null 2>&1 || die 2 "python3 fehlt." "python3 is missing."
    command -v skopeo >/dev/null 2>&1 || msg \
        "Hinweis: skopeo fehlt noch – der Radar meldet bis dahin Registry-Fehler." \
        "Note: skopeo is still missing – the radar reports registry errors until it is installed."

    need_unit diun-radar.service
    need_unit diun-radar.timer
    run systemctl enable --now diun-radar.timer
    msg "Erster Radar-Lauf …" "First radar run …"
    run systemctl start diun-radar.service
    msg "Versions-Radar eingerichtet." "Version radar set up."
}

install_cleanup() {
    [ -f "$LIB/diun-update.py" ] || die 2 \
        "$LIB/diun-update.py fehlt." "$LIB/diun-update.py is missing."
    command -v python3 >/dev/null 2>&1 || die 2 "python3 fehlt." "python3 is missing."
    need_unit diun-cleanup.service
    need_unit diun-cleanup.timer
    run systemctl enable --now diun-cleanup.timer
    msg "Tägliches Aufräumen eingerichtet. Regeln: /etc/image-updates.conf (ohne Datei die Standardwerte)." \
        "Daily cleanup set up. Rules: /etc/image-updates.conf (defaults without the file)."
}

# ----------------------------------------------------------------- main ----
cmd="${1:-}"; arg="${2:-}"

if [ "$cmd" = check ] && [ -z "$arg" ]; then
    do_check
    exit 0
fi

[ "$(id -u)" = 0 ] || [ "$DRY" = 1 ] || \
    die 77 "Dafür werden Administratorrechte benötigt." "This requires administrator privileges."

# One installer at a time.
if [ "$DRY" != 1 ]; then
    exec 8>/run/lock/diun-deps.lock
    flock -n 8 || die 75 "Eine andere Installation läuft bereits." \
                         "Another installation is already running."
else
    ROOT=$(mktemp -d /tmp/diun-deps-dryrun.XXXXXX)
    trap 'rm -rf "$ROOT"' EXIT
    msg "Probelauf: nichts wird ausgeführt, Dateien landen in $ROOT." \
        "Dry run: nothing is executed, files go to $ROOT."
fi

case "$cmd:$arg" in
    install:skopeo) install_skopeo ;;
    install:diun)   install_diun ;;
    start:diun)     start_diun ;;
    install:radar)  install_radar ;;
    install:cleanup) install_cleanup ;;
    *) die 64 "Unbekannter Befehl: $cmd $arg" "Unknown command: $cmd $arg" ;;
esac

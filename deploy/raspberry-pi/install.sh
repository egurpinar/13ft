#!/usr/bin/env bash
# Install or update 13ft as a background service on a Raspberry Pi (Pi Zero W
# and up) or any Debian-based system with systemd.
#
#   sudo bash deploy/raspberry-pi/install.sh              install, or update to this copy
#   sudo bash deploy/raspberry-pi/install.sh --uninstall  remove it (your library is kept)
#
# Made to share a Pi with Pi-hole. 13ft runs at the lowest CPU and disk
# priority, may use at most 60 % of one core and 160 MB of memory, fetches one
# article at a time, and refuses to fetch while memory is short. Nothing is
# installed while the other Pi-hole (PEER_IP in /etc/nhrc-pi.conf, when that
# file exists) is not answering DNS, or with under 150 MB of memory free.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_DIR=/opt/13ft
DATA_DIR=/var/lib/13ft
ENV_FILE=/etc/13ft.env
UNIT_FILE=/etc/systemd/system/13ft.service
SERVICE=13ft
SERVICE_USER=thirteenft
MIN_FREE_MB=150                       # pip's peak on a Pi Zero, with room for Pi-hole
NHRC_CONF=/etc/nhrc-pi.conf
LOW=(nice -n 19 ionice -c 2 -n 7)     # behind Pi-hole and the camera

say() { printf '%s\n' "$*"; }
die() { printf '\nNot done: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run it with sudo"
command -v systemctl >/dev/null || die "this needs a system with systemd"
[ -f "$SRC/app/portable.py" ] || die "run it from inside a 13ft download ($SRC has no app/portable.py)"

if [ "${1:-}" = "--uninstall" ]; then
    systemctl disable --now "$SERVICE" 2>/dev/null || true
    rm -f "$UNIT_FILE"
    systemctl daemon-reload
    rm -rf /opt/13ft
    say "13ft removed. Kept: your library in $DATA_DIR and settings in $ENV_FILE (delete them by hand if you want them gone)."
    exit 0
fi

conf_value() {  # read KEY="value" from the Pi-hole package's settings without running that file
    sed -n "s/^$1=\"\{0,1\}\([^\"]*\)\"\{0,1\}\$/\1/p" "$NHRC_CONF" 2>/dev/null | head -1
}

port() {
    local p
    p="$(sed -n 's/^PORT=//p' "$ENV_FILE" 2>/dev/null | tail -1)"
    printf '%s' "${p:-5000}"
}

say "13ft: install or update from $SRC"

# --- preflight: the house keeps its DNS, this Pi keeps its memory ---------
PEER_IP=""
PEER_NAME="the other Pi-hole"
if [ -r "$NHRC_CONF" ]; then
    PEER_IP="$(conf_value PEER_IP)"
    name="$(conf_value PEER_NAME)"
    if [ -n "$name" ]; then PEER_NAME="$name"; fi
fi
if [ -n "$PEER_IP" ]; then
    command -v dig >/dev/null || die "dig is needed for the DNS check (sudo apt install dnsutils)"
    answer="$(dig @"$PEER_IP" pi.hole A +short +time=3 +tries=2 2>/dev/null || true)"
    grep -qE '^[0-9]+(\.[0-9]+){3}$' <<< "$answer" \
        || die "$PEER_NAME ($PEER_IP) is not answering DNS. While it is down this Pi is the house's only DNS server, so nothing was installed. Try again when it is back."
    say "  ok  $PEER_NAME ($PEER_IP) answers DNS"
fi
free_mb="$(awk '/^MemAvailable:/ {print int($2 / 1024)}' /proc/meminfo)"
[ "${free_mb:-0}" -ge "$MIN_FREE_MB" ] \
    || die "only ${free_mb:-?} MB of memory available (at least $MIN_FREE_MB wanted). A camera live view or timelapse may be running - try again in a few minutes."
say "  ok  ${free_mb} MB of memory available"
command -v python3 >/dev/null || die "python3 is missing (sudo apt install python3)"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "Python 3.9 or newer is needed"
PORT="$(port)"
listening="$(ss -ltnH "sport = :$PORT" 2>/dev/null || true)"
if [ -n "$listening" ] && ! systemctl is-active --quiet "$SERVICE"; then
    die "port $PORT is already used by another program. Put another port in $ENV_FILE (PORT=5080) and run this again."
fi

# --- 1: system package for Python virtual environments --------------------
say "1/5 system packages"
if ! python3 -c 'import ensurepip, venv' 2>/dev/null; then
    "${LOW[@]}" apt-get install -y --no-install-recommends python3-venv \
        || { "${LOW[@]}" apt-get update -qq && "${LOW[@]}" apt-get install -y --no-install-recommends python3-venv; }
fi

# --- 2: an account with no login and no rights outside its library --------
say "2/5 service account '$SERVICE_USER'"
id "$SERVICE_USER" >/dev/null 2>&1 \
    || useradd --system --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$DATA_DIR"

# --- 3: program files (owned by root, read-only for the service) ---------
say "3/5 program files in $APP_DIR"
install -d -m 0755 "$APP_DIR"
rm -rf "$APP_DIR/app.new"
cp -a "$SRC/app" "$APP_DIR/app.new"
rm -rf "$APP_DIR/app"
mv "$APP_DIR/app.new" "$APP_DIR/app"
install -m 0644 "$SRC/requirements.txt" "$SRC/logo.png" "$APP_DIR/"
chown -R root:root "$APP_DIR/app"

# --- 4: Python packages ---------------------------------------------------
say "4/5 Python packages (several minutes on a Pi Zero; it runs at low priority)"
[ -x "$APP_DIR/venv/bin/python" ] || "${LOW[@]}" python3 -m venv "$APP_DIR/venv"
# shellcheck disable=SC2054  # the commas belong to pip's --only-binary value
pip_args=(--disable-pip-version-check --no-cache-dir --prefer-binary --only-binary=lxml,regex,markupsafe)
case "$(uname -m)" in
    # Prebuilt ARMv6/ARMv7 packages: compiling lxml on a Pi Zero would take hours.
    armv6l|armv7l) pip_args+=(--extra-index-url https://www.piwheels.org/simple) ;;
esac
"${LOW[@]}" "$APP_DIR/venv/bin/pip" install "${pip_args[@]}" -r "$APP_DIR/requirements.txt"
# The service cannot write next to its code, so compile it once now.
"${LOW[@]}" "$APP_DIR/venv/bin/python" -m compileall -q "$APP_DIR/app" >/dev/null

# --- 5: settings and service ------------------------------------------------
say "5/5 settings ($ENV_FILE) and service"
if [ ! -f "$ENV_FILE" ]; then
    host="$(hostname)"
    cat > "$ENV_FILE" <<EOF
# 13ft settings. After a change: sudo systemctl restart 13ft
HOST=0.0.0.0
PORT=5000
DATA_DIR=$DATA_DIR
# Names you may type in the browser. IP addresses always work.
ALLOWED_HOSTS=read.lan,$host,$host.local
# Small-board limits: one article at a time, and only with this much memory free (MB).
FETCH_CONCURRENCY=1
MIN_FREE_MB=100
# Which sources to try, in order (default: googlebot,clean,wayback,archive_today):
# SOURCES=googlebot,clean,wayback,archive_today
EOF
    chmod 0644 "$ENV_FILE"
    say "  wrote $ENV_FILE"
else
    say "  kept your $ENV_FILE"
fi
install -m 0644 "$SRC/deploy/raspberry-pi/13ft.service" "$UNIT_FILE"
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"

PORT="$(port)"
say "  waiting for 13ft to answer (up to 2 minutes on a Pi Zero)..."
answered=""
for _ in $(seq 1 60); do
    if python3 - "$PORT" 2>/dev/null <<'PY'
import sys, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
opener.open(f"http://127.0.0.1:{sys.argv[1]}/robots.txt", timeout=3)
PY
    then answered=1; break; fi
    sleep 2
done
[ -n "$answered" ] || die "13ft did not start. See: sudo journalctl -u $SERVICE -n 50"

ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
mem_bytes="$(systemctl show -p MemoryCurrent --value "$SERVICE" 2>/dev/null || true)"
if [[ "$mem_bytes" =~ ^[0-9]+$ ]]; then
    mem_text="$((mem_bytes / 1048576)) MB"
else
    mem_text="(not reported by this kernel)"
fi
if grep -qw memory /sys/fs/cgroup/cgroup.controllers 2>/dev/null; then
    cap_text="active (160 MB)"
else
    cap_text="not available on this kernel; low priority and MIN_FREE_MB still apply"
fi

say ""
say "Done. 13ft is running and starts by itself after a restart."
say "  Open:            http://${ip:-this-pi}:$PORT   (from any device on your Wi-Fi)"
say "  Memory in use:   $mem_text"
say "  Memory cap:      $cap_text"
say "  Settings:        $ENV_FILE    Library: $DATA_DIR"
say "  Log:             sudo journalctl -u $SERVICE -n 50"
say "  Optional nicer name: in Pi-hole's Local DNS Records add read.lan -> ${ip:-the IP of this Pi},"
say "  then open http://read.lan:$PORT"

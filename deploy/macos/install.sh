#!/bin/bash
# Install or update 13ft on this Mac. It runs in the background, starts when
# you log in, and only this Mac can reach it: http://localhost:8013
#
#   bash deploy/macos/install.sh              install, or update to this copy
#   bash deploy/macos/install.sh --uninstall  remove it (your library is kept)
#
# Port 8013, not 5000: macOS's AirPlay Receiver already listens on 5000.
# Use another one with:  PORT=8014 bash deploy/macos/install.sh
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LABEL="local.13ft.reader"
PORT="${PORT:-8013}"
APP_HOME="$HOME/Library/Application Support/13ft"
DATA_DIR="$APP_HOME/data"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/13ft.log"
URL="http://localhost:$PORT"
DOMAIN="gui/$(id -u)"

say() { printf '%s\n' "$*"; }
die() { printf '\nNot done: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "this installer is for macOS. Elsewhere run: python3 app/portable.py"
[ "$(id -u)" -ne 0 ] || die "run it as yourself, without sudo"

stop_agent() { launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true; }

if [ "${1:-}" = "--uninstall" ]; then
    stop_agent
    rm -f "$PLIST"
    rm -rf "$APP_HOME/app" "$APP_HOME/venv"
    rm -f "$APP_HOME/requirements.txt" "$APP_HOME/logo.png" "$APP_HOME/install.sh"
    say "13ft removed. Your library is kept in: $DATA_DIR"
    exit 0
fi

[ -f "$SRC/app/portable.py" ] || die "run it from inside a 13ft download ($SRC has no app/portable.py)"

# Newest suitable Python: Homebrew or python.org first (newer TLS than Apple's), Apple's last.
find_python() {
    local candidate
    for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 \
        /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
        "$(command -v python3 || true)"; do
        [ -n "$candidate" ] && [ -x "$candidate" ] || continue
        if "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}
PY="$(find_python)" || die "Python 3.9 or newer is needed. Install it from https://www.python.org/downloads/macos/ (or run: xcode-select --install), then run this again."

say "13ft: install or update"
say "  Python: $PY ($("$PY" -c 'import platform; print(platform.python_version())'))"
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && ! launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    die "port $PORT is used by another program. Choose another: PORT=8014 bash \"$0\""
fi

say "1/4 program files -> $APP_HOME"
mkdir -p "$APP_HOME" "$DATA_DIR" "$(dirname "$PLIST")" "$(dirname "$LOG")"
rm -rf "$APP_HOME/app.new"
cp -R "$SRC/app" "$APP_HOME/app.new"
rm -rf "$APP_HOME/app"
mv "$APP_HOME/app.new" "$APP_HOME/app"
cp "$SRC/requirements.txt" "$SRC/logo.png" "$APP_HOME/"
cp "$SRC/deploy/macos/install.sh" "$APP_HOME/install.sh"   # kept for --uninstall

say "2/4 Python packages (a minute the first time)"
# A Python upgrade (e.g. by Homebrew) breaks an existing environment: rebuild it then.
if [ -e "$APP_HOME/venv" ] && ! "$APP_HOME/venv/bin/python" -c 'import sys' >/dev/null 2>&1; then
    rm -rf "$APP_HOME/venv"
fi
[ -x "$APP_HOME/venv/bin/python" ] || "$PY" -m venv "$APP_HOME/venv"
"$APP_HOME/venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip
"$APP_HOME/venv/bin/python" -m pip install --quiet --disable-pip-version-check -r "$APP_HOME/requirements.txt"

say "3/4 start at login"
"$PY" - "$PLIST" "$LABEL" "$APP_HOME" "$DATA_DIR" "$PORT" "$LOG" <<'PY'
import plistlib
import sys

plist, label, home, data, port, log = sys.argv[1:]
with open(plist, "wb") as handle:
    plistlib.dump({
        "Label": label,
        "ProgramArguments": [f"{home}/venv/bin/python", f"{home}/app/portable.py"],
        "WorkingDirectory": f"{home}/app",
        "EnvironmentVariables": {
            "HOST": "127.0.0.1",          # this Mac only
            "PORT": port,
            "DATA_DIR": data,
            "PYTHONUNBUFFERED": "1",
            "PYTHONWARNINGS": "ignore",  # e.g. urllib3's note about Apple's LibreSSL
        },
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},  # restart it if it ever crashes
        "ThrottleInterval": 30,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }, handle)
PY
stop_agent
launchctl enable "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
started=""
for _ in 1 2 3; do  # right after a bootout, macOS can briefly refuse the bootstrap
    if launchctl bootstrap "$DOMAIN" "$PLIST" >/dev/null 2>&1; then started=1; break; fi
    sleep 1
done
[ -n "$started" ] || die "macOS did not start it (launchctl bootstrap). Log: $LOG"

say "4/4 checking that it answers"
answered=""
for _ in $(seq 1 30); do
    if curl -fsS --noproxy '*' -o /dev/null --max-time 2 "$URL/robots.txt" 2>/dev/null; then answered=1; break; fi
    sleep 1
done
[ -n "$answered" ] || die "13ft did not start. Log: $LOG"

say ""
say "Done. 13ft runs in the background and starts when you log in."
say "  Open it:        $URL"
say "  Make it an app: Safari > File > Add to Dock  (Chrome: menu > Cast, save, and share > Install page as app)"
say "  Bookmarklet:    javascript:location.href='$URL/'+location.href"
say "  Library: $DATA_DIR"
say "  Log:     $LOG"
say "  Update: run the install command again.  Remove: bash \"$APP_HOME/install.sh\" --uninstall"
open "$URL" >/dev/null 2>&1 || true

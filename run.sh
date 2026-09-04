#!/usr/bin/env bash
# Dreamland floor projection: single entry point on the Raspberry Pi.
#
#   ./run.sh                 play the remembered show (initially: config.json)
#   ./run.sh tecfase         switch to the show in config-tecfase.json and remember it
#   ./run.sh default         switch back to config.json and remember it
#   ./run.sh loop            force loop mode; ./run.sh detect forces detect mode
#   SKIP_PULL=1 ./run.sh     do not touch git (offline)
#   FOREGROUND=1 ./run.sh    run in this terminal even if the boot service is installed
#
# What it does, in order: git pull (fast-forward only), install anything
# missing, point mpv at the Pi's own screen even when started over SSH, then
# start display.py. If the boot service from install-service.sh is installed,
# it restarts that service instead of starting a second copy.
set -euo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$REPO"

SKIP_PULL="${SKIP_PULL:-0}"
FOREGROUND="${FOREGROUND:-0}"
FROM_SERVICE="${DREAMLAND_FROM_SERVICE:-0}"
SERVICE_NAME="dreamland"
UNIT_FILE="/etc/systemd/system/$SERVICE_NAME.service"
SHOW_FILE="$REPO/.show"

log() { printf '[run.sh] %s\n' "$*"; }
die() { printf '[run.sh] ERROR: %s\n' "$*" >&2; exit 1; }

MODE=""
SHOW_ARG=""
for arg in "$@"; do
    case "$arg" in
        loop|detect)
            MODE="$arg"
            ;;
        *)
            [ -n "$SHOW_ARG" ] && die "more than one show given: '$SHOW_ARG' and '$arg'"
            SHOW_ARG="$arg"
            ;;
    esac
done

# 1. Update ------------------------------------------------------------------
if [ "$SKIP_PULL" != "1" ] && [ -d .git ]; then
    if git pull --ff-only; then
        log "repository at $(git rev-parse --short HEAD)"
    else
        log "git pull failed (offline?); running the local copy at $(git rev-parse --short HEAD)"
    fi
fi

# 2. Pick the show and read its config ---------------------------------------
list_shows() {
    printf 'default'
    local f
    for f in "$REPO"/config-*.json; do
        [ -f "$f" ] || continue
        printf ' %s' "$(basename "$f" .json | sed 's/^config-//')"
    done
}

if [ -n "$SHOW_ARG" ] && [ "$SHOW_ARG" != "default" ] && [ ! -f "$REPO/config-$SHOW_ARG.json" ]; then
    die "no such show '$SHOW_ARG'; available: $(list_shows)"
fi
if [ -n "$SHOW_ARG" ]; then
    printf '%s\n' "$SHOW_ARG" > "$SHOW_FILE"
fi
SHOW="$(cat "$SHOW_FILE" 2>/dev/null || true)"
SHOW="${SHOW:-default}"
if [ "$SHOW" != "default" ] && [ ! -f "$REPO/config-$SHOW.json" ]; then
    log "remembered show '$SHOW' no longer exists; using default"
    SHOW="default"
fi
CONFIG_FILE="config.json"
if [ "$SHOW" != "default" ]; then
    CONFIG_FILE="config-$SHOW.json"
fi

command -v python3 >/dev/null 2>&1 || die "python3 is missing: sudo apt-get install -y python3"

read_config() {
    python3 - "$CONFIG_FILE" "$1" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
for part in sys.argv[2].split("."):
    value = value.get(part, {}) if isinstance(value, dict) else {}
print("" if isinstance(value, dict) else value)
PY
}

CONFIG_MODE="$(read_config mode)"
EFFECTIVE_MODE="${MODE:-${CONFIG_MODE:-loop}}"
CAMERA="$(read_config detect.camera)"
CAMERA="${CAMERA:-opencv}"

# 3. Dependencies ------------------------------------------------------------
APT_PACKAGES=()
command -v mpv >/dev/null 2>&1 || APT_PACKAGES+=(mpv)
if [ "$EFFECTIVE_MODE" = "detect" ]; then
    python3 -c "import cv2" 2>/dev/null || APT_PACKAGES+=(python3-opencv)
    python3 -c "import numpy" 2>/dev/null || APT_PACKAGES+=(python3-numpy)
    if [ "$CAMERA" = "picamera2" ]; then
        python3 -c "import picamera2" 2>/dev/null || APT_PACKAGES+=(python3-picamera2)
    fi
fi
if [ "${#APT_PACKAGES[@]}" -gt 0 ]; then
    log "installing: ${APT_PACKAGES[*]}"
    sudo -n apt-get update -qq || die "sudo failed; run by hand: sudo apt-get install -y ${APT_PACKAGES[*]}"
    sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${APT_PACKAGES[@]}" \
        || die "apt-get install failed for: ${APT_PACKAGES[*]}"
fi
if [ "$EFFECTIVE_MODE" = "detect" ]; then
    if ! python3 -c "import tflite_runtime.interpreter" 2>/dev/null \
        && ! python3 -c "import ai_edge_litert.interpreter" 2>/dev/null \
        && ! python3 -c "import tensorflow.lite" 2>/dev/null; then
        log "installing tflite-runtime"
        python3 -m pip install --break-system-packages -r requirements-detect.txt \
            || python3 -m pip install -r requirements-detect.txt \
            || die "could not install tflite-runtime; see requirements-detect.txt"
    fi
fi

# 4. Find the Pi's own screen (matters when started over SSH or by systemd) --
find_display() {
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    if [ -z "${WAYLAND_DISPLAY:-}" ]; then
        local sock
        for sock in "$XDG_RUNTIME_DIR"/wayland-[0-9]*; do
            if [ -S "$sock" ]; then
                WAYLAND_DISPLAY="$(basename "$sock")"
                export WAYLAND_DISPLAY
                break
            fi
        done
    fi
    if [ -z "${DISPLAY:-}" ] && [ -S /tmp/.X11-unix/X0 ]; then
        export DISPLAY=:0
    fi
    [ -n "${WAYLAND_DISPLAY:-}" ] || [ -n "${DISPLAY:-}" ]
}

if [ "$FROM_SERVICE" = "1" ]; then
    # Booting: the desktop session may not be up yet. Wait for it.
    for _ in $(seq 1 60); do
        find_display && break
        sleep 2
    done
fi
if ! find_display; then
    export DISPLAY="${DISPLAY:-:0}"
    log "no Wayland or X11 socket found; trying DISPLAY=:0 (is the desktop logged in?)"
fi
log "screen: WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-} DISPLAY=${DISPLAY:-}"

# 5. Hand off to the boot service if it is installed --------------------------
if [ "$FROM_SERVICE" != "1" ] && [ -f "$UNIT_FILE" ]; then
    if [ "$FOREGROUND" = "1" ]; then
        log "stopping the $SERVICE_NAME service to run in the foreground"
        sudo -n systemctl stop "$SERVICE_NAME" || die "could not stop the service: sudo systemctl stop $SERVICE_NAME"
    else
        [ -n "$MODE" ] && log "note: the service uses the mode in config.json, not '$MODE'"
        log "restarting the $SERVICE_NAME service (show: $SHOW; logs: journalctl -u $SERVICE_NAME -f)"
        sudo -n systemctl restart "$SERVICE_NAME" || die "could not restart the service: sudo systemctl restart $SERVICE_NAME"
        sleep 2
        systemctl --no-pager --lines=5 status "$SERVICE_NAME" || true
        exit 0
    fi
fi

# 6. Start -------------------------------------------------------------------
if [ "$FROM_SERVICE" != "1" ]; then
    # A copy left over from an earlier manual start would fight over the screen.
    if pkill -f "python3 .*display\.py" 2>/dev/null; then
        sleep 1
    fi
fi
log "starting display.py (show: $SHOW, mode: $EFFECTIVE_MODE)"
exec python3 "$REPO/display.py" --config "$REPO/$CONFIG_FILE" ${MODE:+--mode "$MODE"}

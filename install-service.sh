#!/usr/bin/env bash
# Install or remove the systemd service that starts the display at boot.
#
#   ./install-service.sh            install, enable, start
#   ./install-service.sh --remove   stop, disable, delete
#
# After installing, ./run.sh restarts the service (after pulling) instead of
# starting a second copy; FOREGROUND=1 ./run.sh stops it and runs in a terminal.
set -euo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
SERVICE_NAME="dreamland"
UNIT_FILE="/etc/systemd/system/$SERVICE_NAME.service"
RUN_USER="${SUDO_USER:-$USER}"

if [ "${1:-}" = "--remove" ]; then
    sudo systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
    sudo rm -f "$UNIT_FILE"
    sudo systemctl daemon-reload
    echo "removed $SERVICE_NAME.service"
    exit 0
fi

[ -f "$REPO/dreamland.service" ] || { echo "dreamland.service template not found in $REPO" >&2; exit 1; }
[ -x "$REPO/run.sh" ] || chmod +x "$REPO/run.sh"

sed -e "s|__USER__|$RUN_USER|g" -e "s|__REPO__|$REPO|g" "$REPO/dreamland.service" | sudo tee "$UNIT_FILE" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo "installed $SERVICE_NAME.service (user $RUN_USER, repo $REPO)"
echo "  logs:  journalctl -u $SERVICE_NAME -f"
echo "  stop:  sudo systemctl stop $SERVICE_NAME"
echo "  start: sudo systemctl start $SERVICE_NAME"

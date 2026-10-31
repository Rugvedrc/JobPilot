#!/usr/bin/env bash
# Update the server after copying / pulling new code:  bash deploy/update.sh
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
sudo systemctl restart fresherpilot
echo "Restarted. Logs: journalctl -u fresherpilot -f"

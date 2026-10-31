#!/usr/bin/env bash
# One-time setup of FresherPilot on an Ubuntu 22.04/24.04 server (e.g. Oracle Cloud Always Free, ARM or x86).
# Run from the project folder:   bash deploy/setup_server.sh <CLOUDFLARE_TUNNEL_TOKEN>
# The token comes from Cloudflare Zero Trust -> Networks -> Tunnels -> Create tunnel (see docs/DEPLOY.md).
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_USER="$(whoami)"
TOKEN="${1:-}"

echo "==> Installing system packages"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip curl

echo "==> Python environment in $APP_DIR/.venv (the AI model download makes this take a few minutes)"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip wheel
if [ "$(uname -m)" = "x86_64" ]; then  # CPU-only PyTorch: much smaller than the default CUDA build
  "$APP_DIR/.venv/bin/pip" install torch --index-url https://download.pytorch.org/whl/cpu
fi
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "!! Created .env from .env.example: fill in GOOGLE_CLIENT_ID, FP_OWNER_EMAIL, FP_SECRET_KEY, ... then run:"
  echo "!!   sudo systemctl restart fresherpilot"
fi
if ! grep -qE '^FP_SECRET_KEY=[A-Za-z0-9_-]{20}' "$APP_DIR/.env"; then
  KEY="$("$APP_DIR/.venv/bin/python" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
  sed -i "s|^FP_SECRET_KEY=.*|FP_SECRET_KEY=$KEY|" "$APP_DIR/.env"
  echo "==> Generated FP_SECRET_KEY in .env (back up .env: losing it locks Google users' saved API keys)"
fi
grep -q '^FP_MODE=' "$APP_DIR/.env" && sed -i 's|^FP_MODE=.*|FP_MODE=cloud|' "$APP_DIR/.env" || echo "FP_MODE=cloud" >> "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

echo "==> systemd service (starts on boot, restarts if it crashes)"
sudo tee /etc/systemd/system/fresherpilot.service >/dev/null <<UNIT
[Unit]
Description=FresherPilot job search app
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONIOENCODING=utf-8
ExecStart=$APP_DIR/.venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now fresherpilot

echo "==> Daily backup of accounts + .env (keeps 14 days in ~/fresherpilot-backups)"
mkdir -p "$HOME/fresherpilot-backups"
CRON="15 3 * * * tar czf $HOME/fresherpilot-backups/fp-\$(date +\%F).tgz -C $APP_DIR users .env 2>/dev/null; find $HOME/fresherpilot-backups -name 'fp-*.tgz' -mtime +14 -delete"
( crontab -l 2>/dev/null | grep -v fresherpilot-backups ; echo "$CRON" ) | crontab -

if [ -n "$TOKEN" ]; then
  echo "==> Cloudflare Tunnel"
  ARCH="$(dpkg --print-architecture)"
  curl -fsSL -o /tmp/cloudflared.deb "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ARCH.deb"
  sudo dpkg -i /tmp/cloudflared.deb
  sudo cloudflared service install "$TOKEN" || echo "(cloudflared service already installed)"
else
  echo "!! No tunnel token given: run  bash deploy/setup_server.sh <TOKEN>  again once you have it."
fi

echo "==> Waiting for the app"
for i in $(seq 1 60); do
  if curl -fs http://127.0.0.1:8000/api/health >/dev/null; then echo "FresherPilot is running."; break; fi
  sleep 3
done
echo "Logs: journalctl -u fresherpilot -f     Restart: sudo systemctl restart fresherpilot"

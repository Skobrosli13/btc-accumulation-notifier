#!/usr/bin/env bash
# One-shot setup for the always-on BTC signal stack on Ubuntu (Lightsail 2GB).
# Layout expected (siblings):
#   /home/ubuntu/btc-accumulation-notifier   (this repo)
#   /home/ubuntu/btc-dashboard               (the Next.js dashboard)
# Run from inside the notifier directory:  bash setup_lightsail.sh
#
# Automates: Python venv + deps, Node 20, dashboard build, systemd services
# (api + dashboard), and the three crons. Cloudflare tunnel/Access, Litestream,
# and S3 are guided steps in DEPLOY.md (they need your accounts).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASH="$(cd "$HERE/../btc-dashboard" 2>/dev/null && pwd || true)"
cd "$HERE"

echo "==> System packages (sudo)"
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip curl cron
sudo systemctl enable --now cron || true

echo "==> Node 20 LTS"
if ! command -v node >/dev/null || [ "$(node -v | cut -d. -f1 | tr -d v)" -lt 20 ]; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  sudo apt-get install -y nodejs
fi
node -v

echo "==> Python venv + deps"
python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install lxml || echo "(lxml optional; ETF flows degrade gracefully without it)"

echo "==> .env"
[ -f .env ] || { cp .env.example .env; echo "  copied .env.example -> .env (EDIT IT: RESEND_API_KEY, EMAIL_TO, API_TOKEN)"; }

echo "==> Sanity dry-runs"
python -m app.collect_once --dry-run || echo "(collect dry-run issue; check above)"
python -m app.run_once --dry-run || echo "(run dry-run issue; check above)"

mkdir -p logs

echo "==> Build the dashboard"
if [ -n "$DASH" ] && [ -f "$DASH/package.json" ]; then
  ( cd "$DASH" && npm ci && npm run build )
  [ -f "$DASH/.env.local" ] || { cp "$DASH/.env.local.example" "$DASH/.env.local"; \
    echo "  created $DASH/.env.local (SET API_TOKEN to match this app's .env)"; }
else
  echo "  !! btc-dashboard not found next to this repo; clone/copy it then re-run."
fi

echo "==> Install systemd services (api + dashboard)"
for svc in btc-api btc-dashboard; do
  sudo cp "deploy/$svc.service" "/etc/systemd/system/$svc.service"
done
sudo systemctl daemon-reload
sudo systemctl enable --now btc-api
[ -n "$DASH" ] && sudo systemctl enable --now btc-dashboard || true
sleep 2
curl -fsS http://127.0.0.1:8000/api/health >/dev/null && echo "  api /health OK" || echo "  !! api not responding yet"

echo "==> Install crons (idempotent)"
PY="$HERE/.venv/bin/python"
CRONS=$(cat <<EOF
*/10 * * * * cd $HERE && $PY -m app.collect_once >> $HERE/logs/collect.log 2>&1
0 */6 * * * cd $HERE && $PY -m app.run_once >> $HERE/logs/run.log 2>&1
0 */8 * * * cd $HERE && $PY -m app.watchdog >> $HERE/logs/watchdog.log 2>&1
45 12 * * 1-5 cd $HERE && $PY -m scripts.send_digest >> $HERE/logs/digest.log 2>&1
EOF
)
# Pipefail-safe: an empty/absent crontab makes grep -v exit 1, which would abort
# under `set -euo pipefail` — guard both the read and the filter with `|| true`.
{ (crontab -l 2>/dev/null || true) | grep -vE 'app\.(collect_once|run_once|watchdog)|scripts\.send_digest' || true; echo "$CRONS"; } | crontab -
echo "  crons:"; crontab -l | grep -E 'app\.|scripts\.' | sed 's/^/    /'

echo ""
echo "==> Done with the automated parts. Remaining (see DEPLOY.md):"
echo "    1. Cloudflare Tunnel + Access (Google login) -> exposes the dashboard privately."
echo "    2. Litestream -> S3 backups (deploy/litestream.yml)."
echo "    3. Close inbound 80/443 in the Lightsail firewall (tunnel is outbound-only)."
echo "    4. Set RESEND_API_KEY + EMAIL_TO in .env; verify your Gmail in Resend; restart: sudo systemctl restart btc-api"

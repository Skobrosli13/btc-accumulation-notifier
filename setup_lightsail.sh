#!/usr/bin/env bash
# One-shot setup for an Ubuntu host (AWS Lightsail nano or any Ubuntu box).
# Run from inside the project directory after un-tarring:  bash setup_lightsail.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

echo "==> Installing system Python venv/pip (sudo)"
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip

echo "==> Creating virtualenv + installing dependencies"
python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
# Optional: lets the free ETF-flow scraper work via pandas.read_html
pip install lxml || echo "(lxml optional; ETF flows will degrade gracefully without it)"

echo "==> Ensuring .env exists"
if [ ! -f .env ]; then cp .env.example .env; echo "  copied .env.example -> .env (edit it!)"; fi

mkdir -p logs

echo "==> Sanity dry-run (no notify, no DB write)"
python -m app.run_once --dry-run || echo "(dry-run reported an issue; check output above)"

echo "==> Installing cron (every 6h UTC, idempotent)"
CRON_LINE="0 */6 * * * cd $HERE && $HERE/.venv/bin/python -m app.run_once >> $HERE/logs/run.log 2>&1"
( crontab -l 2>/dev/null | grep -v 'app.run_once' ; echo "$CRON_LINE" ) | crontab -
echo "  cron now contains:"
crontab -l | grep 'app.run_once' | sed 's/^/    /'

echo ""
echo "==> Done. Next:"
echo "    1. Subscribe to your ntfy topic on your phone (see DEPLOY.md)."
echo "    2. (Optional) add FRED_API_KEY / GLASSNODE_API_KEY to .env, then no restart needed."
echo "    3. Watch it work:  tail -f $HERE/logs/run.log"

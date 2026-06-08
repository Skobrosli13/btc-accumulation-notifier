# Deploy — always-on BTC signal stack (Lightsail 2GB)

This describes the **live** box. The system is:

```
Lightsail 2GB (Ubuntu, systemd) — STATIC IP
 ├─ cron */10  → python -m app.collect_once   # short-term swing (4h/1d) → candles/derivs/signals → alerts
 ├─ cron 0 */6 → python -m app.run_once        # long-term accumulation score
 ├─ cron hourly→ python -m app.watchdog        # dead-man's-switch (run >= as often as WATCHDOG_STALE_HOURS)
 ├─ systemd    → uvicorn app.api:app (127.0.0.1:8000)   # read-only JSON API, localhost only
 ├─ systemd    → next start (btc-dashboard, 127.0.0.1:3000)   # localhost only
 └─ nginx      → TLS (Let's Encrypt) + HTTP basic auth → reverse-proxy :3000

Public:  https://btc.riverviewweb.com  → nginx (basic auth) → dashboard
```

The API and dashboard both bind to **localhost**. The only thing reachable from
the internet is nginx on 80/443, which terminates TLS, enforces a shared HTTP
basic-auth password, and proxies to the dashboard on :3000. The dashboard reads
the API server-side over `127.0.0.1:8000` with the `API_TOKEN`, which never
reaches the browser.

> **Historical note.** An earlier design used a Cloudflare tunnel + Cloudflare
> Access for ingress and Litestream for S3 backup. The live box does **not** use
> these. The files `deploy/cloudflared-config.yml`, `deploy/btc-tunnel.service`,
> and `deploy/litestream.yml` are leftovers from that design and are **not wired
> in** — ignore them (or delete them) unless you deliberately revive that path.

## Prerequisites

- **Resend**: account + `RESEND_API_KEY`; verify `skobrosli@gmail.com` (sandbox can only
  email your verified address until you verify a domain).
- **AWS**: Lightsail **2GB** Ubuntu instance with a **static IP** attached.
- **DNS**: an A-record `btc.<yourdomain>` → the static IP.
- A long random `API_TOKEN`: `openssl rand -hex 32`.
- A private GitHub repo holding both sibling folders (`btc-accumulation-notifier/`
  and `btc-dashboard/`).

## 1. Put the code on the box (git, two sibling folders)

```bash
ssh -i <key.pem> ubuntu@<STATIC_IP>
# the two repos live as siblings directly under /home/ubuntu:
git clone https://github.com/Skobrosli13/btc-accumulation-notifier.git ~/btc-accumulation-notifier
git clone https://github.com/Skobrosli13/btc-dashboard.git ~/btc-dashboard
```

## 2. Configure secrets

`btc-accumulation-notifier/.env`:
```dotenv
RESEND_API_KEY=...           # from Resend
EMAIL_TO=skobrosli@gmail.com
EXCHANGE=okx
API_TOKEN=<openssl rand -hex 32>
PUBLIC_BASE_URL=https://btc.riverviewweb.com   # used to build the email unsubscribe link
# leave paid keys (GLASSNODE_API_KEY/COINGLASS_API_KEY/...) blank to run free
```
`btc-dashboard/.env.local`:
```dotenv
API_BASE_URL=http://127.0.0.1:8000
API_TOKEN=<the SAME token as above>
```

## 3. Install services + crons

`setup_lightsail.sh` installs the Python venv + deps, Node, builds the dashboard,
installs/starts the `btc-api` and `btc-dashboard` systemd services, and adds the
crons:
```bash
cd ~/btc-accumulation-notifier && bash setup_lightsail.sh
```

## 4. Public ingress: nginx + TLS + basic auth

Open inbound **80 and 443** in the Lightsail firewall, ensure the DNS A-record
points at the static IP, then:
```bash
cd ~/btc-accumulation-notifier
DOMAIN=btc.riverviewweb.com EMAIL=skobrosli@gmail.com bash deploy/setup_nginx_basicauth.sh
```
This provisions a Let's Encrypt cert (auto-renew via `certbot.timer`), sets up the
basic-auth password file, and reverse-proxies the dashboard. The script also adds
an **exact-match `location = /api/unsubscribe`** that bypasses basic auth and
proxies straight to the FastAPI on :8000 — without it the email unsubscribe link
(and the `List-Unsubscribe` header) would prompt for a password and then 404,
since the dashboard has no such route.

> If you edited nginx by hand on an existing box, make sure that
> `location = /api/unsubscribe { auth_basic off; proxy_pass http://127.0.0.1:8000; }`
> block is present, then `sudo nginx -t && sudo systemctl reload nginx`.

## 5. Verify

```bash
curl -s http://127.0.0.1:8000/api/health | python3 -m json.tool   # db_ok, layers, last_collect
systemctl status btc-api btc-dashboard --no-pager
tail -f ~/btc-accumulation-notifier/logs/collect.log          # next 10-min collect
```
Open `https://btc.riverviewweb.com` → basic-auth prompt → dashboard. Confirm
`API_TOKEN` never appears in the browser network tab (all API calls are
server-side). Click an unsubscribe link from a test email and confirm it loads
the confirmation page **without** a password prompt.

## Deploy updates (git-based)

```bash
# on your machine: push to the private repo, then on the box:
# backend code change:
cd ~/btc-accumulation-notifier && git pull && sudo systemctl restart btc-api
# dashboard change also needs a rebuild:
cd ~/btc-dashboard && git pull && npm run build && sudo systemctl restart btc-dashboard
```
Config changes: edit the **server's** `.env` and `sudo systemctl restart btc-api`
(the API caches config via `@lru_cache`; cron entrypoints pick up `.env` on their
next run without a restart). The local Windows `.env` is separate from prod.

## Notes

- **Binance is 451 from AWS/US** — the data layer uses **OKX** (primary) with a
  **Kraken** fallback and CoinGecko as a last-resort price source. No action needed.
- **Free-tier ceiling**: real liquidation-cascade + order-flow are paid
  (Coinglass/CryptoQuant); the free tier uses a funding/OI/volatility proxy. Add
  `GLASSNODE_API_KEY` / `COINGLASS_API_KEY` to `.env` later and restart `btc-api`
  — layers activate automatically.
- **Short-term cadence**: swing alerts evaluate on *closed* 4h/1d candles, so the
  fastest a swing alert fires is 4h-candle-close. The 10-min cron is for dashboard
  liveness + funding/OI freshness.
- **Alert recipients**: dashboard subscribers receive only the infrequent
  long-term tier/flash alerts; short-term swing alerts go to the owner
  (`EMAIL_TO`) / ntfy / Telegram only.
- **Costs**: ~$12–14/mo (2GB instance + domain). Resend/data APIs are free-tier.
```

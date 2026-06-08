# Deploy — always-on BTC signal stack (Lightsail 2GB)

This supersedes the old nano + single-6h-cron runbook. The system is now:

```
Lightsail 2GB (Ubuntu, systemd)
 ├─ cron */10  → python -m app.collect_once   # short-term swing (4h/1d) → candles/derivs/signals → alerts
 ├─ cron 0 */6 → python -m app.run_once        # long-term accumulation score
 ├─ cron 0 */8 → python -m app.watchdog        # dead-man's-switch (emails if pipeline stale)
 ├─ systemd    → uvicorn app.api:app (127.0.0.1:8000)   # read-only JSON API, localhost only
 ├─ systemd    → next start (btc-dashboard, 127.0.0.1:3000)
 ├─ systemd    → cloudflared tunnel            # OUTBOUND-only; no inbound ports
 └─ systemd    → litestream                    # continuous SQLite → S3 backup

Cloudflare Access (Google sign-in → your Gmail, long session) gates https://<host> → dashboard
You (any device) → one-click Google login → dashboard
```

The API and dashboard are bound to **localhost**. The only thing reachable from
the internet is the dashboard, **via the Cloudflare tunnel**, and **only by you**
via Cloudflare Access. No inbound ports are opened on the box.

## Prerequisites (gather these)

- **Resend**: account + `RESEND_API_KEY`; verify `skobrosli@gmail.com` (sandbox can only email
  your verified address until you verify a domain).
- **AWS**: Lightsail **2GB** Ubuntu instance; an **S3 bucket + credentials** for Litestream.
- **Cloudflare** (free): a domain on a Cloudflare zone; you'll create a Tunnel + an Access app.
- A long random `API_TOKEN`: `openssl rand -hex 32`.

## 1. Put the code on the box (two sibling folders)

```bash
scp -i <key.pem> btc-accumulation-notifier.tar.gz btc-dashboard.tar.gz ubuntu@<IP>:~
ssh -i <key.pem> ubuntu@<IP>
tar -xzf btc-accumulation-notifier.tar.gz   # -> ~/btc-accumulation-notifier
tar -xzf btc-dashboard.tar.gz               # -> ~/btc-dashboard  (sibling)
```

## 2. Configure secrets

`~/btc-accumulation-notifier/.env`:
```dotenv
RESEND_API_KEY=...           # from Resend
EMAIL_TO=skobrosli@gmail.com
EXCHANGE=okx
API_TOKEN=<openssl rand -hex 32>
# leave paid keys blank to run free; ntfy/telegram optional
```
`~/btc-dashboard/.env.local`:
```dotenv
API_BASE_URL=http://127.0.0.1:8000
API_TOKEN=<the SAME token as above>
```

## 3. Run the installer

```bash
cd ~/btc-accumulation-notifier && bash setup_lightsail.sh
```
This installs Python venv + deps, Node 20, builds the dashboard, installs and starts the
`btc-api` and `btc-dashboard` systemd services, and adds the three crons. It curls
`/api/health` as a smoke test.

## 4. Cloudflare Tunnel + Access (exposes the dashboard privately)

```bash
sudo mkdir -p /usr/local/bin && \
  curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /usr/local/bin/cloudflared && sudo chmod +x /usr/local/bin/cloudflared
cloudflared tunnel login
cloudflared tunnel create btc-dashboard          # note the TUNNEL_ID + creds path
cloudflared tunnel route dns btc-dashboard btc.<yourdomain>
```
Edit `deploy/cloudflared-config.yml` (fill `TUNNEL_ID`, creds path, and `hostname`), copy it to
`~/.cloudflared/config.yml`, then install the tunnel service:
```bash
cp deploy/btc-tunnel.service /tmp && sudo cp /tmp/btc-tunnel.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now btc-tunnel
```
In the **Cloudflare Zero Trust dashboard** → Access → Applications → Add a *self-hosted* app on
`btc.<yourdomain>`, with a policy **Allow → emails → skobrosli@gmail.com** and a session of e.g.
30 days. Now visiting the host prompts a one-click Google login (rare, thanks to the long session).

**Close inbound ports**: in the Lightsail networking tab, remove the 80/443 rules — the tunnel is
outbound-only, nothing needs to be open.

## 5. Litestream backups

```bash
# install litestream (.deb from github.com/benbjohnson/litestream/releases), then:
sudo cp deploy/litestream.yml /etc/litestream.yml      # edit YOUR_BUCKET_NAME
echo 'AWS_ACCESS_KEY_ID=...'      | sudo tee /etc/default/litestream
echo 'AWS_SECRET_ACCESS_KEY=...'  | sudo tee -a /etc/default/litestream
sudo systemctl enable --now litestream
```

## 6. Verify

```bash
curl -s http://127.0.0.1:8000/api/health | python3 -m json.tool   # db_ok, layers, last_collect
systemctl status btc-api btc-dashboard btc-tunnel --no-pager
tail -f ~/btc-accumulation-notifier/logs/collect.log              # next 10-min collect
```
Open `https://btc.<yourdomain>` → Google login → dashboard. Confirm `API_TOKEN` never appears in
the browser network tab (all API calls are server-side).

## Notes

- **Binance is 451 from AWS/US** — the data layer uses **OKX** (primary) with a **Kraken** fallback
  and CoinGecko as a last-resort price source. No action needed.
- **Free-tier ceiling**: real liquidation-cascade + order-flow are paid (Coinglass/CryptoQuant);
  free tier uses a funding/OI/volatility proxy. Add `GLASSNODE_API_KEY` / `COINGLASS_API_KEY` to
  `.env` later and restart `btc-api` — layers activate automatically.
- **Short-term cadence**: swing alerts evaluate on *closed* 4h/1d candles, so the fastest a swing
  alert fires is 4h-candle-close. The 10-min cron is for dashboard liveness + funding/OI freshness.
- **Costs**: ~$12–14/mo (2GB instance + S3 + domain). Cloudflare/Resend/data APIs are free-tier.

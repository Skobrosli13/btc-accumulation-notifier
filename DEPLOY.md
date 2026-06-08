# Deploy to AWS Lightsail (or any Ubuntu host)

This package is ready to run. Notifications are pre-wired to a private **ntfy**
topic; the on-chain/derivatives paid layers stay off until you add keys.

## Your ntfy topic

```
btc-accum-76ed1d7da866
```

Subscribe to it **before** the first alert so you receive it:

- Phone: install the **ntfy** app (iOS/Android) → **+** → Subscribe → topic name
  `btc-accum-76ed1d7da866` (server `https://ntfy.sh`).
- Or in a browser: <https://ntfy.sh/btc-accum-76ed1d7da866>

> Anyone who knows the topic name can read its alerts (ntfy topics are
> unauthenticated). The random suffix keeps it private-by-obscurity; rotate it any
> time by editing `NTFY_TOPIC` in `.env`.

## 1. Create the instance

AWS Lightsail → **Create instance** → Linux/Ubuntu, **nano** plan (~$5/mo).

## 2. Copy the code up

From the machine that has the tarball (this Windows box):

```powershell
scp -i <your-key.pem> C:\tmp\btc-accumulation-notifier.tar.gz ubuntu@<INSTANCE_IP>:~
```

## 3. SSH in and run the one-shot setup

```bash
ssh -i <your-key.pem> ubuntu@<INSTANCE_IP>
tar -xzf btc-accumulation-notifier.tar.gz
cd btc-accumulation-notifier
bash setup_lightsail.sh
```

That script installs Python venv + deps, does a dry run, and installs the 6-hourly
cron job (`0 */6 * * *` UTC). Crypto trades 24/7, so there is no market-hours logic.

## 4. Confirm it's live

```bash
tail -f ~/btc-accumulation-notifier/logs/run.log     # watch the next run
sqlite3 ~/btc-accumulation-notifier/btc.db 'select run_ts,composite,tier from runs;'  # the ledger
```

You'll get a phone push only when the **tier changes** (no spam), plus an
independent capitulation-flash alert (debounced) on sharp sell-offs.

## Optional upgrades (no redeploy, no restart — just edit `.env`)

| Add to `.env` | Effect |
|---|---|
| `FRED_API_KEY=...` | Activates the **macro/liquidity** layer (free key) |
| `GLASSNODE_API_KEY=...` | Activates the **on-chain valuation** layer — the single biggest accuracy upgrade |
| `COINGLASS_API_KEY=...` | Richer **derivatives** (liquidations, OI) |
| `SOSOVALUE_API_KEY=...` | Cleaner **ETF flows** (vs. the Farside scrape) |

The next cron run picks them up automatically: the new categories activate and the
composite re-weights.

## Calibration (run manually any time)

```bash
cd ~/btc-accumulation-notifier && . .venv/bin/activate
python -m scripts.backtest
```

## Note on price data from a US/AWS IP

Binance geo-blocks many cloud/US IPs (HTTP 451). The app **automatically falls
back to CoinGecko** for price, so it still runs — but CoinGecko's free tier caps
history at ~365 days, so the **200-week MA is unavailable on the fallback** and the
price category leans on the Mayer Multiple (200-day MA) until you either add an
on-chain key or run from a Binance-reachable region. This is handled gracefully and
noted in the alert's data-tier line.

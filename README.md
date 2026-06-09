# BTC Signal System — long-term accumulation + short-term swings

Monitors Bitcoin on two horizons and pushes **email alerts** (via Resend), with a
**Next.js dashboard** for live state and alert history:

- **Long-term (buy-only accumulation):** a 0–100 confidence score from on-chain /
  price-structure / macro / sentiment / derivatives. Tiered alerts (Watch →
  Accumulate → Deep Value) on a tier change, plus an acute-capitulation flash.
- **Short-term (two-sided swing, 4h/1d):** RSI / MACD / EMA-cross / Bollinger / ATR
  plus funding/OI signals, producing a signed bias and discrete BUY/SELL triggers.

**Alert-only. No order execution. Not financial advice.** It surfaces signals; you decide.

> Long-term answers *"is this a zone to accumulate?"* (don't need to nail the bottom).
> Short-term answers *"is now a decent moment to enter/exit a swing?"* They're kept
> visually and structurally distinct — a short-term SELL never means the long-term
> accumulation thesis is broken.

## Architecture

A short, idempotent **collector** runs every 10 min (short-term) and the existing
**run** every 6 h (long-term), both writing to SQLite (WAL). A localhost **read-only
API** serves a co-hosted **Next.js dashboard**, exposed privately via a **Cloudflare
Tunnel + Access** (no open ports). A **watchdog** emails if the pipeline goes stale.
See [DEPLOY.md](DEPLOY.md) and [the plan](../../). 

```
collector(10m) ─┐
run_once(6h) ───┼─→ SQLite (WAL) ──→ read-only API (:8000) ──→ Next.js dashboard (:3000)
watchdog(8h) ───┘                                                   └─ Cloudflare Access (you only)
        └────────────────→ Resend email (primary) + optional ntfy/Telegram
```

## Data sources (free, no keys)

- **Price / klines / funding / OI:** **OKX** primary, **Kraken** fallback (Binance is
  HTTP-451 from US/AWS), **CoinGecko** last-resort price. See [app/sources/exchange.py](app/sources/exchange.py).
- **Sentiment:** alternative.me Fear & Greed.
- **Macro:** FRED (free key) — `M2`, real yields, HY spread.
- **On-chain valuation:** **bitcoin-data.com / BGeometrics** (free, no key) — MVRV-Z, NUPL, SOPR,
  Puell, realized price. The biggest long-term lever, now active on the free tier. Disable with
  `ONCHAIN_FREE=false`. See [app/sources/onchain.py](app/sources/onchain.py).
- **Long-term OI flush:** derived free from the OKX open-interest the collector already stores.
- **Paid drop-ins (optional upgrades, auto-activate when a key is set):** Glassnode (7d-smoothed
  on-chain), Coinglass (real liquidations / OI-weighted funding), CryptoQuant.

> **Free-tier ceiling:** only true liquidation-cascade + real-time order-flow remain paid (Coinglass).
> The free tier uses a funding/OI/volatility *proxy* for those; the dashboard health bar states this
> honestly. On-chain valuation is now fully available for free.

## Notifications

Email (Resend) is primary; ntfy/Telegram optional. You receive:
- **Long-term:** tier-change alerts (Watch/Accumulate/Deep Value) + acute-capitulation flash.
- **Short-term:** BUY and SELL/exit triggers on closed 4h/1d candles (EMA cross, RSI bounce/rollover,
  MACD cross, Bollinger reclaim/reject, funding spike, volume flush), cooldown-debounced.
- **Health:** a dead-man's-switch heartbeat if collection goes stale.

Counter-trend triggers (e.g. a BUY in a bearish regime) are flagged as lower-confidence in the alert.

## Quick start (local, free tier)

Backend:
```bash
python3 -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env                              # set EXCHANGE=okx; RESEND/keys optional
python -m app.collect_once --dry-run              # short-term, prints signals
python -m app.run_once --dry-run                  # long-term
uvicorn app.api:app --port 8000                   # read-only API
```
Dashboard (separate `btc-dashboard/` folder):
```bash
cd ../btc-dashboard && npm install
cp .env.local.example .env.local                  # API_BASE_URL=http://127.0.0.1:8000
npm run dev                                        # http://localhost:3000
```

## Calibration / backtests

```bash
python -m scripts.backtest             # long-term thresholds at 2015/2018/2022 bottoms
python -m scripts.backtest_shortterm   # short-term trigger hit-rates over OKX history
```
Both print honest, small-sample caveats — favor economic logic over fitted numbers.

## Tests

```bash
pip install pytest && python -m pytest -q
```
Covers scoring, short-term indicators/triggers/composite, alert cooldown, the store, the API, and
config parsing (incl. the `.env` inline-comment fix).

## Project layout

```
app/
  config.py        env -> Config (presence of a key toggles its layer)
  sources/         exchange (OKX/Kraken), price, funding, sentiment, macro, onchain, derivatives
  scoring.py       long-term accumulation score (0-100, tiers)
  shortterm.py     short-term indicators + two-sided triggers + signed bias composite
  alerting.py      tier/flash decisions + short-term cooldown + message builders
  notify.py        email (Resend) primary; ntfy/Telegram secondary
  store.py         SQLite (WAL): runs, candles, derivs, st_signals, st_alerts
  run_once.py      long-term entrypoint (cron 6h)
  collect_once.py  short-term collector (cron 10m)
  watchdog.py      dead-man's-switch (cron 8h)
  api.py           read-only JSON API for the dashboard
scripts/           backtest.py, backtest_shortterm.py
deploy/            systemd units, cloudflared + litestream configs
tests/             pytest suite
```
See [DEPLOY.md](DEPLOY.md) for the Lightsail 2GB deployment (~$12–14/mo all-in on free data).

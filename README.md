# BTC Long-Term Accumulation-Zone Notifier

Monitors Bitcoin for a **long-term cyclical accumulation zone** and sends tiered push
notifications when conditions historically associated with cycle bottoms align. You place
spot buys manually on alert.

**Alert-only. No order execution. Not financial advice.** It surfaces a detection signal; you
decide whether, how much, and where to buy.

> The goal is **zone detection and clear tiered alerts**, not predicting an exact bottom — the
> design deliberately makes precise bottom-calling unnecessary. *Approximately-right beats a
> precise call you cannot make.*

---

## What it does

Every run computes a composite **Accumulation Confidence** score (0–100) from orthogonal
indicator categories, maps it to a tier, and notifies you **only when the tier changes** (no
spam). An independent **acute-capitulation flash** can fire on a sharp sell-off regardless of
tier. Every run is persisted to a SQLite ledger for later calibration.

### Categories & weights

| Category | Weight | Indicators | Data tier |
|---|---|---|---|
| On-chain valuation | 0.35 | MVRV Z-Score, Realized-price ratio, NUPL, SOPR (7d), Puell Multiple | **Paid** (Glassnode / CryptoQuant) |
| Price structure | 0.20 | Price / 200-week MA, Mayer Multiple | **Free** |
| Macro / liquidity | 0.20 | M2 YoY, HY credit spread, 10Y real yield, ETF net flows | **Free** (FRED; ETF best-effort) |
| Sentiment | 0.10 | Fear & Greed Index | **Free** |
| Derivatives | 0.15 | Funding rate (7d), OI deleveraging, Liquidation cascade | Free funding proxy / **Paid** Coinglass |

**Graceful degradation is mandatory.** Every source is optional except free price data. If a
source/key is missing, its indicators are skipped, the remaining **category weights are
renormalized to sum to 1.0**, and the alert says so. The bot never crashes because a paid key
is absent.

### Tiers

- **Neutral** `< 40` — log only, no alert.
- **Watch** `40–60` — indicators starting to align.
- **Accumulate** `60–80` — meaningful confluence; begin laddering.
- **Deep Value** `≥ 80` **and price ≤ 200-week MA** — strongest confluence; heaviest tranches.

### Acute-capitulation flash (independent of tier)

Fires a one-off "consider a tranche" alert when **all** hold: a capitulation signal (large 24h
liquidations on the paid tier, or the free funding/OI-flush proxy), **and** Fear & Greed ≤ 10,
**and** price down more than the configured % over 24–48h. Debounced separately (default once
per 3 days).

---

## Quick start (free tier — zero paid keys)

```bash
python3 -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env          # set NTFY_TOPIC and (free) FRED_API_KEY; leave paid keys blank

# one run, printing the decision without sending or persisting:
python -m app.run_once --dry-run

# a real run (sends on tier change, writes the ledger):
python -m app.run_once
```

It runs end-to-end with an empty `.env`. With no `FRED_API_KEY` the macro layer is skipped;
with no notification transport configured, the alert is logged instead of sent.

> **Honesty note:** on the free tier the MVRV / realized-price / NUPL / SOPR layer is inactive —
> the *highest-signal* layer for bottoms. Free-tier confidence leans on price-structure + macro
> + sentiment. **A Glassnode key is the single biggest accuracy upgrade.**

---

## Configuration (`.env`)

See [.env.example](.env.example) for the full list. Highlights:

- **Notifications:** `NTFY_TOPIC` (default transport; subscribe to it in the ntfy app), or
  `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`. Both optional — unconfigured = log only.
- **Free data:** `FRED_API_KEY` (free from fredaccount.stlouisfed.org) turns on the macro layer.
- **Paid drop-ins:** `GLASSNODE_API_KEY` (on-chain layer — biggest lever), `COINGLASS_API_KEY`
  (richer derivatives), `SOSOVALUE_API_KEY` (ETF flows). Presence of a key is what activates
  its layer — there is no separate enable flag.
- **Signal config:** category weights, tier thresholds, flash parameters, and cycle context
  (`ATH_DATE`, `PEAK_TO_TROUGH_DAYS`).

Per-indicator thresholds (the `(neutral, extreme)` bands) live in
[`app/scoring.py`](app/scoring.py) `THRESHOLDS` — one place to tune, and what the backtest reads.

> ETF flows via Farside scraping need an HTML parser (`lxml` or `html5lib`) for
> `pandas.read_html`. Without one, the ETF indicator silently degrades to "unavailable" rather
> than failing — install `lxml` if you want it, or set `SOSOVALUE_API_KEY`.

---

## Calibration / backtest

```bash
python -m scripts.backtest        # from the project root
```

Prints what each indicator read at the **2015 / 2018 / 2022** bottoms, reports each free
indicator's false-positive rate (how often it entered its "bottom zone" away from a real
bottom), and suggests a threshold floor — **clearly flagged as n=3 and overfit-prone**. The
live `runs` ledger also accumulates real data, so thresholds can be revisited as the zone
develops.

---

## Tests

```bash
pip install pytest
python -m pytest -q
```

Covers the scoring math, the weight-renormalization graceful-degradation path, tier logic
(including the Deep-Value 200-WMA gate), the flash conditions, and alert debouncing.

---

## Deploy (AWS Lightsail, ~$5/mo)

1. Create a Lightsail instance: Ubuntu, nano.
2. SSH in; install Python 3.11+; `git clone` into `/home/ubuntu/btc-accumulation-notifier`.
3. `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`.
4. `cp .env.example .env`; set `NTFY_TOPIC` and (free) `FRED_API_KEY`. Leave paid keys blank to start.
5. `mkdir -p logs`; add cron (every 6h, UTC):
   ```cron
   0 */6 * * * cd /home/ubuntu/btc-accumulation-notifier && /home/ubuntu/btc-accumulation-notifier/.venv/bin/python -m app.run_once >> logs/run.log 2>&1
   ```
6. Subscribe to the ntfy topic on your phone. Watch `btc.db` and `logs/run.log` accumulate.
7. To upgrade accuracy later: add `GLASSNODE_API_KEY` (biggest lever) and/or `COINGLASS_API_KEY`,
   then redeploy — the on-chain / derivatives categories activate automatically and reweight.

Crypto trades 24/7, so there is **no market-hours/clock logic** — the job is a short, idempotent
run on a schedule, not a long-running process.

---

## Honesty & guardrails

- **n=3 cycles.** Any threshold tuned to three bottoms is fragile. Indicators are chosen by
  economic logic, not curve-fit to three lows.
- **Regime shift.** ETFs and macro liquidity now drive timing more than the halving; this
  drawdown is shallower than past cycles so far. **Do not assume** on-chain metrics must reach
  their 2018 depths before a bottom — calibrate, don't dogmatically wait.
- **Overshoot risk.** BTC can fall below every indicator's "bottom zone," or the cycle may not
  rhyme. No single indicator is a trigger; the signal is the **confluence** of independent ones.
- **Zone, not point.** The bot flags a *zone* of elevated probability, not a precise low.
- **Not financial advice; alert-only; manual execution.** The build's job ends at the alert.
  Treat the tiers as a ladder: buy a bit more as confidence deepens and price sinks further
  below the 200-week MA and realized price, and keep dry powder for the capitulation flushes —
  that discipline is for you, not logic in the system.

---

## Project layout

```
app/
  config.py        load + validate env; presence of optional keys toggles paid sources
  sources/         price, funding, sentiment, macro, etf_flows, onchain, derivatives
  scoring.py       linear_score, per-indicator sub-scores, category + composite, tiers
  alerting.py      tier-transition + acute-flash decision, debounce, message builder
  notify.py        ntfy / Telegram (no-op if unconfigured)
  store.py         SQLite: init, last_tier, last_flash_at, record_run
  run_once.py      entrypoint: fetch -> score -> decide -> notify -> persist
scripts/backtest.py  one-off historical calibration
tests/test_scoring.py
```

"""Offline calibration (run MANUALLY) — emits app/calibration.json + app/track_record.json.

Percentile-rank only makes sense where we have deep, multi-cycle history. So this
calibrates the indicators that have it:
  * price_to_wma200 — deep weekly closes (Kraken ~2013) -> 200-week MA.
  * m2_yoy / hy_spread / real_yield — FRED full history.
It then backtests that price+macro backbone with EXPANDING-window percentiles
(no look-ahead) and reports a forward-return hit-rate vs the base rate.

On-chain (bitcoin-data.com, ~2022+ = ONE cycle), sentiment and derivatives KEEP
their economic-logic thresholds — percentile on one cycle would mislead — so they
are deliberately NOT calibrated and NOT in the historical track record.

    python -m scripts.calibrate        # from the project root

The live path only reads the committed app/calibration.json; re-run this to refresh.
"""
from __future__ import annotations

import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import scoring                      # noqa: E402
from app.config import load_config           # noqa: E402
from app.sources import exchange, macro      # noqa: E402

COINBASE = "https://api.exchange.coinbase.com/products/BTC-USD/candles"

PROBS = [0.0, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 1.0]
APP_DIR = Path(__file__).resolve().parents[1] / "app"
FORWARD_DAYS = [90, 180, 365]   # forward-return horizons (daily frame)
MIN_HISTORY = 60                # don't calibrate an indicator with fewer points
# Must reach back to here to percentile-calibrate (so the history spans the 2022
# bottom + its run-up — otherwise percentile-rank on one regime misleads).
SPAN_CUTOFF = pd.Timestamp("2021-06-01")


# --- data pulls --------------------------------------------------------------

def _coinbase_daily() -> pd.DataFrame:
    """Deep daily BTC-USD closes from Coinbase (since 2015-07, reachable from AWS),
    paginated 300/req. Returns [date, close]; empty on failure."""
    start = datetime(2015, 7, 20, tzinfo=timezone.utc)
    end_all = datetime.now(timezone.utc)
    rows: list[list] = []
    cur = start
    while cur < end_all:
        win_end = min(cur + timedelta(days=290), end_all)
        try:
            r = requests.get(COINBASE, params={"granularity": 86400,
                             "start": cur.isoformat(), "end": win_end.isoformat()},
                             headers={"User-Agent": "btc-calibrate"}, timeout=30)
            if r.status_code == 200:
                rows += r.json()      # [time, low, high, open, close, volume]
        except Exception:  # noqa: BLE001
            pass
        cur = win_end
        time.sleep(0.25)              # be polite to the public endpoint
    if not rows:
        return pd.DataFrame(columns=["date", "close"])
    df = pd.DataFrame(rows, columns=["t", "low", "high", "open", "close", "volume"])
    df["date"] = (pd.to_datetime(df["t"], unit="s", utc=True).dt.tz_localize(None)
                  .dt.normalize().astype("datetime64[ns]"))
    df["close"] = df["close"].astype(float)
    return df.drop_duplicates("date").sort_values("date").reset_index(drop=True)[["date", "close"]]


def _price_history() -> pd.DataFrame:
    """Deep daily price -> date, close, mayer (price/200DMA), price_to_wma200
    (price/200-week MA from weekly resample). Coinbase deep daily; falls back to the
    shallower OKX/Kraken weekly if Coinbase is unreachable."""
    daily = _coinbase_daily()
    if daily.empty:
        df = exchange.klines_history("1w", 800, "BTC-USDT")
        out = df[["open_time", "close"]].copy()
        out["date"] = (pd.to_datetime(out["open_time"]).dt.tz_localize(None)
                       .dt.normalize().astype("datetime64[ns]"))
        out = out.sort_values("date").reset_index(drop=True)
        out["mayer"] = np.nan
        out["price_to_wma200"] = out["close"] / out["close"].rolling(200, min_periods=104).mean()
        return out[["date", "close", "mayer", "price_to_wma200"]]

    daily = daily.set_index("date")
    daily["dma200"] = daily["close"].rolling(200, min_periods=200).mean()
    daily["mayer"] = daily["close"] / daily["dma200"]
    weekly = daily["close"].resample("1W").last()
    wma200 = weekly.rolling(200, min_periods=150).mean()
    p2w = (weekly / wma200).reindex(daily.index, method="ffill")
    daily["price_to_wma200"] = p2w
    return daily.reset_index()[["date", "close", "mayer", "price_to_wma200"]]


def _fred_series(sid: str, key: str) -> pd.DataFrame:
    rows = macro._series(sid, key, limit=0)   # oldest->newest full history
    if not rows:
        return pd.DataFrame(columns=["date", sid])
    df = pd.DataFrame(rows, columns=["date", sid])
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[ns]")
    return df.sort_values("date").reset_index(drop=True)


def _macro_history(cfg) -> dict[str, pd.DataFrame]:
    """Per-indicator [date, value] frames (empty dict if no FRED key). Merged onto
    the weekly spine separately so daily spikes (e.g. HY blowouts) aren't resampled away."""
    if not cfg.fred_api_key:
        print("  (no FRED_API_KEY — macro will not be calibrated)")
        return {}
    key = cfg.fred_api_key
    out: dict[str, pd.DataFrame] = {}
    m2 = _fred_series("M2SL", key)
    if not m2.empty:
        m2["m2_yoy"] = m2["M2SL"].pct_change(12) * 100.0
        out["m2_yoy"] = m2[["date", "m2_yoy"]].dropna().reset_index(drop=True)
    hy = _fred_series("BAMLH0A0HYM2", key)
    if not hy.empty:
        out["hy_spread"] = hy.rename(columns={"BAMLH0A0HYM2": "hy_spread"})
    ry = _fred_series("DFII10", key)
    if not ry.empty:
        out["real_yield"] = ry.rename(columns={"DFII10": "real_yield"})
    return out


# --- calibration breakpoints -------------------------------------------------

def _breakpoints(values) -> list[float] | None:
    arr = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=float)
    if arr.size < MIN_HISTORY:
        return None
    bp = [float(x) for x in np.quantile(arr, PROBS)]
    for i in range(1, len(bp)):   # enforce monotonic non-decreasing
        bp[i] = max(bp[i], bp[i - 1])
    return bp


def _emit_calibration(raw: dict[str, pd.DataFrame]) -> dict:
    """raw: {indicator -> DataFrame[date, v]} at native frequency (deep as available).
    Calibrates only indicators with enough points AND history spanning the cutoff."""
    inds = {}
    for name, df in raw.items():
        df = df.dropna(subset=["v"])
        earliest = df["date"].min() if not df.empty else None
        bp = _breakpoints(df["v"].tolist())
        if bp is None or earliest is None or earliest > SPAN_CUTOFF:
            since = earliest.date() if earliest is not None and pd.notna(earliest) else None
            print(f"  skip {name}: shallow history (n={len(df)}, since={since}) -> economic threshold")
            continue
        inds[name] = {"direction": scoring.DIRECTION[name], "n": int(len(df)),
                      "since": str(earliest.date()), "breakpoints": bp}
        print(f"  calibrated {name}: n={len(df)} since={earliest.date()} "
              f"bp={[round(x, 3) for x in bp]}")
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "probs": PROBS, "indicators": inds}


# --- track record (expanding-window, no look-ahead) --------------------------

def _track_record(weekly: pd.DataFrame, cfg, calibrated: list[str]) -> dict:
    rows = weekly.dropna(subset=["price_to_wma200"]).reset_index(drop=True)
    hist: dict[str, list[float]] = {k: [] for k in calibrated}
    comps, tiers, closes = [], [], []
    for _, r in rows.iterrows():
        sub: dict[str, float | None] = {}
        for name in calibrated:
            v = r.get(name)
            if v is None or not np.isfinite(v):
                continue
            hist[name].append(float(v))
            sub[name] = scoring.rank_score(hist[name], float(v), scoring.DIRECTION[name])
        cats = scoring.category_scores(sub)
        comp, _ = scoring.composite(cats, cfg.weights, 1.0)  # timing-neutral for the test
        wma = r["close"] / r["price_to_wma200"] if r["price_to_wma200"] else None
        tiers.append(scoring.tier(comp, r["close"], wma,
                                  cfg.tier_watch, cfg.tier_accumulate, cfg.tier_deepvalue))
        comps.append(comp)
        closes.append(float(r["close"]))

    def winrate(idxs, h):
        idxs = [i for i in idxs if i + h < len(closes)]
        if not idxs:
            return None
        return round(sum(1 for i in idxs if closes[i + h] > closes[i]) / len(idxs), 3)

    signal_idx = [i for i, t in enumerate(tiers) if t in ("ACCUMULATE", "DEEP_VALUE")]

    # Independent EPISODES: consecutive signal days are autocorrelated, so collapse
    # each run of signal days to one (its start). Daily hit-rate overstates n;
    # episodes are the honest sample size for the confidence interval.
    episodes, in_ep = [], False
    for i, t in enumerate(tiers):
        sig = t in ("ACCUMULATE", "DEEP_VALUE")
        if sig and not in_ep:
            episodes.append(i)
        in_ep = sig

    def bootstrap_ci(outcomes, iters=2000):
        if len(outcomes) < 3:
            return None
        rnd = random.Random(42)
        n = len(outcomes)
        rates = sorted(sum(outcomes[rnd.randrange(n)] for _ in range(n)) / n for _ in range(iters))
        return [round(rates[int(0.05 * iters)], 3), round(rates[int(0.95 * iters)], 3)]

    horizons = {}
    for h in FORWARD_DAYS:
        ep_outcomes = [1 if closes[s + h] > closes[s] else 0 for s in episodes if s + h < len(closes)]
        horizons[f"{h}d"] = {
            "signal_hit_rate": winrate(signal_idx, h),
            "base_rate": winrate(list(range(len(closes))), h),
            "episode_hit_rate": round(sum(ep_outcomes) / len(ep_outcomes), 3) if ep_outcomes else None,
            "ci": bootstrap_ci(ep_outcomes),   # 90% bootstrap CI on the episode hit-rate
        }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "price+macro backbone, expanding-window percentile, timing-neutral",
        "days": len(closes),
        "from": str(rows["date"].iloc[0].date()) if len(rows) else None,
        "to": str(rows["date"].iloc[-1].date()) if len(rows) else None,
        "signal_days": len(signal_idx),
        "signal_episodes": len(episodes),
        "horizons": horizons,
        "caveats": [
            "Backbone only: price-structure (200WMA/Mayer) + macro — the deep multi-cycle indicators.",
            "On-chain (~2022+), sentiment, derivatives use economic thresholds and are NOT in this test.",
            "Timing multiplier neutralized; cycle context excluded.",
            "Past behavior is not a forecast.",
        ],
    }


def main() -> int:
    cfg = load_config()
    print("Calibrating (deep multi-cycle indicators only)...")
    px = _price_history()
    print(f"  daily closes: {len(px)} "
          f"({px['date'].min().date()} -> {px['date'].max().date()})")
    macro = _macro_history(cfg)

    # Breakpoints from each indicator's full native-frequency history (deep as available).
    raw = {"price_to_wma200": px[["date", "price_to_wma200"]].rename(columns={"price_to_wma200": "v"}),
           "mayer": px[["date", "mayer"]].rename(columns={"mayer": "v"})}
    for name, df in macro.items():
        raw[name] = df.rename(columns={name: "v"})
    calib = _emit_calibration(raw)
    (APP_DIR / "calibration.json").write_text(json.dumps(calib, indent=2))
    print(f"  wrote app/calibration.json ({len(calib['indicators'])} indicators)")

    # Track record uses ONLY the calibrated price/macro backbone.
    px = px.sort_values("date").reset_index(drop=True)
    for name, df in macro.items():
        px = pd.merge_asof(px, df.sort_values("date"), on="date", direction="backward")
    calibrated = [k for k in calib["indicators"] if k in px.columns]
    track = _track_record(px, cfg, calibrated)
    (APP_DIR / "track_record.json").write_text(json.dumps(track, indent=2))
    print(f"  wrote app/track_record.json: {track['horizons']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

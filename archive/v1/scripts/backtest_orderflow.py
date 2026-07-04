"""Order-flow edge backtest from FREE Binance Vision tick data.

Reconstructs, per 4h candle, the order-flow features the user wants to drive the
short-term score — CVD delta, footprint absorption (buying at lows vs selling at
highs), and OI change — then tests whether they actually predict forward returns
(top-quintile up-rate vs base rate, with a Wilson CI, + Spearman corr). No
look-ahead: each candle's feature is known at its close; the forward return is
measured after.

Per-day candle aggregations are cached so the window can grow incrementally.

    python -m scripts.backtest_orderflow <n_days>   # n_days back from END_DATE

FINDING (2026-06-19, 120 days / 720 candles): every feature (CVD delta, footprint
absorption, OI change, combined) has corr ~0 with forward returns and a
top-quintile up-rate CI that straddles the base rate -> NO edge at the 4h swing
horizon. A 30-day pilot's +0.15 corr was one-regime noise. So these are confluence
/ context, not score drivers. Does NOT test order-book resting liquidity (no free
history) or scalping-horizon footprint (a different system).
"""
import io
import math
import os
import sys
import tempfile
import time
import zipfile
from datetime import date, timedelta

import numpy as np
import pandas as pd
import requests

SYMBOL = "BTCUSDT"
END_DATE = date(2026, 6, 17)
TF_MS = 4 * 3600 * 1000
CACHE = os.path.join(tempfile.gettempdir(), "btc_of_cache")
AGG_URL = "https://data.binance.vision/data/futures/um/daily/aggTrades/{s}/{s}-aggTrades-{d}.zip"
MET_URL = "https://data.binance.vision/data/futures/um/daily/metrics/{s}/{s}-metrics-{d}.zip"
os.makedirs(CACHE, exist_ok=True)


def _read_zip_csv(url, **kw):
    r = None
    for attempt in range(5):
        try:
            r = requests.get(url, timeout=180)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            break
        except Exception:
            r = None
            time.sleep(2 * (attempt + 1))   # back off; Binance Vision throttles bursts
    if r is None:
        return None
    z = zipfile.ZipFile(io.BytesIO(r.content))
    name = z.namelist()[0]
    with z.open(name) as f:
        head = f.read(120).decode("utf-8", "ignore").lower()
    has_hdr = "price" in head or "open_interest" in head or "create_time" in head
    with z.open(name) as f:
        return pd.read_csv(f, header=0 if has_hdr else None, **kw)


def _day_candles(d: str) -> pd.DataFrame | None:
    """One day -> 4h candles with order-flow features (cached)."""
    cf = f"{CACHE}/{d}.csv"
    if os.path.exists(cf):
        return pd.read_csv(cf)
    agg = _read_zip_csv(AGG_URL.format(s=SYMBOL, d=d))
    if agg is None or agg.empty:
        return None
    cols = [str(c).strip().lower() for c in agg.columns]
    if "price" not in cols:  # no header -> assign futures aggTrades schema
        agg.columns = ["agg_trade_id", "price", "quantity", "first_trade_id",
                       "last_trade_id", "transact_time", "is_buyer_maker"]
    else:
        agg.columns = cols
    a = agg[["price", "quantity", "transact_time", "is_buyer_maker"]].copy()
    a["price"] = a["price"].astype(float)
    a["quantity"] = a["quantity"].astype(float)
    a["transact_time"] = a["transact_time"].astype("int64")
    bm = a["is_buyer_maker"].astype(str).str.lower().isin(["true", "1"])
    a["signed"] = np.where(bm, -a["quantity"], a["quantity"])   # taker buy +, taker sell -
    a["ts"] = (a["transact_time"] // TF_MS) * TF_MS
    g = a.groupby("ts")
    cand = g.agg(open=("price", "first"), high=("price", "max"),
                 low=("price", "min"), close=("price", "last"),
                 vol=("quantity", "sum"), delta=("signed", "sum"))
    # Footprint zones: signed volume in the bottom third vs top third of the bar range.
    lh = g["price"].agg(low2="min", high2="max")
    a = a.join(lh, on="ts")
    rng = (a["high2"] - a["low2"]).replace(0, np.nan)
    zone = (a["price"] - a["low2"]) / rng
    a["low_delta"] = np.where(zone <= 0.34, a["signed"], 0.0)
    a["high_delta"] = np.where(zone >= 0.66, a["signed"], 0.0)
    cand = cand.join(a.groupby("ts")[["low_delta", "high_delta"]].sum())

    met = _read_zip_csv(MET_URL.format(s=SYMBOL, d=d))
    if met is not None and not met.empty:
        met.columns = [str(c).strip().lower() for c in met.columns]
        tcol = "create_time" if "create_time" in met.columns else met.columns[0]
        oicol = next((c for c in met.columns if "open_interest" in c and "value" not in c), None)
        if oicol:
            ms = pd.to_datetime(met[tcol]).astype("datetime64[ms]").astype("int64")  # pandas3 res-proof
            met["ts"] = (ms // TF_MS) * TF_MS
            oi = met.groupby("ts")[oicol].last().rename("oi")
            cand = cand.join(oi)
    cand = cand.reset_index()
    cand.to_csv(cf, index=False)
    return cand


def wilson(wins, n, z=1.96):
    if n <= 0:
        return 0.0, 1.0
    p = wins / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / d
    return max(0.0, c - m), min(1.0, c + m)


def main():
    n_days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    frames, miss = [], 0
    for i in range(n_days):
        d = (END_DATE - timedelta(days=i)).isoformat()
        c = _day_candles(d)
        if c is None:
            miss += 1
        else:
            frames.append(c)
    if not frames:
        print("no data"); return 1
    p = pd.concat(frames).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    p["delta_pct"] = p["delta"] / p["vol"]
    p["absorption"] = (p["low_delta"] - p["high_delta"]) / p["vol"]   # buy-at-lows minus sell-at-highs
    p["oi_chg"] = p["oi"].pct_change() * 100 if "oi" in p.columns else np.nan
    z = lambda s: (s - s.mean()) / (s.std() or 1)
    p["of_score"] = z(p["delta_pct"].fillna(0)) + z(p["absorption"].fillna(0)) + z(p["oi_chg"].fillna(0))

    print(f"candles: {len(p)}  days_fetched: {n_days - miss}  missing: {miss}")
    print(f"span: {pd.to_datetime(p['ts'].iloc[0], unit='ms').date()} .. {pd.to_datetime(p['ts'].iloc[-1], unit='ms').date()}\n")
    closes = p["close"].reset_index(drop=True)
    feats = ["delta_pct", "absorption", "oi_chg", "of_score"]
    for h in (1, 3, 6):
        fwd = closes.shift(-h) / closes - 1.0
        base_up = float((fwd > 0).mean())
        print(f"--- horizon {h} bar ({h*4}h)   base up-rate={base_up:.2f} ---")
        for f in feats:
            d = pd.DataFrame({"f": p[f].reset_index(drop=True), "fwd": fwd}).dropna()
            if len(d) < 40:
                print(f"  {f:11} n={len(d)} too few"); continue
            corr = d["f"].rank().corr(d["fwd"].rank())  # Spearman = Pearson on ranks (scipy-free)
            q = d["f"].quantile(0.8)
            top = d[d["f"] >= q]
            wins = int((top["fwd"] > 0).sum())
            lo, hi = wilson(wins, len(top))
            edge = lo > base_up
            print(f"  {f:11} corr={corr:+.3f}  top20%_up={wins/len(top):.2f} (n={len(top)}) "
                  f"CI[{lo:.2f},{hi:.2f}]  {'EDGE' if edge else '~base'}")
        print()
    print("EDGE only if the top-quintile up-rate CI clears the base up-rate. "
          "One venue (Binance perp), ~4h candles, small sample. Not a forecast.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

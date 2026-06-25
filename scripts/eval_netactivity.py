"""Offline edge-evaluation for the free network-activity context reads.

The "search for edge" step from the data-source plan. Pulls full BTC price + on-chain
activity history from blockchain.com charts (free, no key, back to 2009 — covers the
2018/2022 bottoms), computes each metric's trailing-90d z-score per day, then reports
forward 30/90d BTC returns bucketed by that z (depressed / normal / elevated) against
the all-days baseline. If an extreme bucket beats the baseline on BOTH mean return and
hit-rate, that metric is a candidate to PROMOTE into scoring (a separate change +
calibration regen). Otherwise: no edge — keep it display-only.

Heuristic scan, not a proof: blockchain.com's full-range series is coarsely sampled
(~weekly) and forward-filled to daily here, and a handful of cycles is not a dataset.
Read it as a smell test, not a fitted parameter.

    python -m scripts.eval_netactivity          # from the project root
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# Allow running as `python scripts/eval_netactivity.py` as well as `-m scripts...`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BC = "https://api.blockchain.info/charts"
_Z_WINDOW = 90
_Z_MINP = 60
METRICS = [("n-unique-addresses", "Active addresses"),
           ("n-transactions", "Transaction count")]


def _chart_df(chart: str, col: str) -> pd.DataFrame:
    r = requests.get(f"{BC}/{chart}", params={"timespan": "all", "format": "json"}, timeout=40)
    r.raise_for_status()
    recs = [(pd.to_datetime(int(v["x"]), unit="s"), float(v["y"]))
            for v in r.json().get("values", [])
            if isinstance(v, dict) and v.get("y") is not None and v.get("x")]
    return pd.DataFrame(recs, columns=["date", col])


def _daily(df: pd.DataFrame, col: str) -> pd.Series:
    """Date-indexed series resampled to daily and forward-filled (coarse source)."""
    s = df.set_index("date")[col].sort_index()
    s = s[~s.index.duplicated(keep="last")]
    return s.asfreq("D").ffill()


def _bucket(sub: pd.DataFrame) -> str:
    if sub.empty:
        return f"{'n=0':>34}"
    return (f"n={len(sub):>5}  "
            f"30d {sub['fwd30'].mean():>+6.1%} ({(sub['fwd30'] > 0).mean():>4.0%} up)  "
            f"90d {sub['fwd90'].mean():>+6.1%} ({(sub['fwd90'] > 0).mean():>4.0%} up)")


def report(price_daily: pd.Series, fwd: pd.DataFrame, chart: str, label: str) -> None:
    try:
        md = _daily(_chart_df(chart, "value"), "value")
    except Exception as exc:  # noqa: BLE001
        print(f"\n{label}\n  fetch failed: {exc}")
        return
    z = (md - md.rolling(_Z_WINDOW, min_periods=_Z_MINP).mean()) / md.rolling(_Z_WINDOW, min_periods=_Z_MINP).std()
    df = fwd.join(z.rename("z"), how="inner").dropna(subset=["z", "fwd90"])
    if df.empty:
        print(f"\n{label}\n  (no overlapping price+metric history)")
        return
    print(f"\n{label}   ({df.index.min().date()} -> {df.index.max().date()}, n={len(df)})")
    print(f"  {'z <= -1 (depressed)':<22} {_bucket(df[df['z'] <= -1])}")
    print(f"  {'-1 < z < +1 (normal)':<22} {_bucket(df[(df['z'] > -1) & (df['z'] < 1)])}")
    print(f"  {'z >= +1 (elevated)':<22} {_bucket(df[df['z'] >= 1])}")
    print(f"  {'ALL days (baseline)':<22} {_bucket(df)}")


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"Network-activity context edge-evaluation  ({stamp})")
    print("Pulling full BTC price + activity history from blockchain.com (free, no key)...")
    try:
        price = _chart_df("market-price", "close")
    except Exception as exc:  # noqa: BLE001
        print(f"  price fetch failed: {exc}")
        return 0
    if price.empty:
        print("  no price history returned — aborting")
        return 0
    # Drop the 2009-2010 sub-$1 dust: tiny denominators turn forward % returns into
    # +inf and swamp the bucket means. Analysis effectively starts once BTC >= $1.
    price = price[price["close"] >= 1.0]
    pdaily = _daily(price, "close")
    fwd = pd.DataFrame({"close": pdaily})
    fwd["fwd30"] = (fwd["close"].shift(-30) / fwd["close"] - 1.0).replace([np.inf, -np.inf], np.nan)
    fwd["fwd90"] = (fwd["close"].shift(-90) / fwd["close"] - 1.0).replace([np.inf, -np.inf], np.nan)
    print(f"  got {len(price)} price points ({price['date'].min().date()} -> {price['date'].max().date()})")

    print("\n" + "=" * 72)
    print("Forward BTC return by metric z-score bucket  (search for edge)")
    print("=" * 72)
    print("Each metric's latest value vs its trailing-90d distribution. An extreme\n"
          "bucket that beats the baseline on BOTH mean return and hit-rate is a\n"
          "promote candidate; otherwise keep it display-only.")
    for chart, label in METRICS:
        report(pdaily, fwd, chart, label)

    print("\nReminder: coarse (~weekly) source ffilled to daily over a few cycles —\n"
          "a signal here is a hypothesis to calibrate, not a proven edge. Do not wire\n"
          "into scoring on this alone; the live runs ledger also accumulates real data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

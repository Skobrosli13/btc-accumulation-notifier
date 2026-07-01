"""Offline edge-evaluation for the free network-activity context reads.

The "search for edge" step from the data-source plan. Pulls full BTC price +
on-chain activity history from blockchain.com charts (free, no key), computes each
metric's z-score per day with the LIVE definition (app/sources/netactivity: value
vs the trailing 90d window EXCLUDING it, population std, >=20 baseline points),
then reports forward 30/90d BTC returns for depressed / normal / elevated buckets.

Statistics are decision-grade, not eyeball-grade: bucket days arrive in contiguous
clumps with ~30-90x overlapping forward windows, so days are collapsed to
NON-OVERLAPPING episodes (>= horizon apart) before the hit-rate and its Wilson 95%
CI; the baseline is the same-window every-horizon-days sample. The analysis window
starts 2017-01-01 — a 2011-2016 hypergrowth baseline dominates every mean-return
comparison independent of any real edge (the window still spans the 2018 and 2022
bottoms).

PRE-REGISTERED PROMOTION RULE (do not judge by eye): a metric's extreme bucket is
a promote candidate ONLY if its non-overlapping episode hit-rate's Wilson CI LOW
end clears the same-window base rate at the PRIMARY 30d horizon, with at least
12 episodes. The script prints PASS/FAIL per bucket; anything else stays
display-only. Promotion itself is still a separate change + calibration regen.

Residual honesty: blockchain.com's full-range series is coarsely sampled
(~weekly) and forward-filled to daily here, while the live layer reads genuinely
daily Coin Metrics data (the community API only serves ~200d — too short to
evaluate) — bucket membership can differ at the margin. na_transfers and
na_addr_balance have NO free deep-history source here and therefore NO promotion
path until one exists.

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

from scripts.st_validation import wilson_interval  # noqa: E402

BC = "https://api.blockchain.info/charts"
_Z_WINDOW = 90          # trailing days, EXCLUDING the current point (live definition)
_Z_MINP = 20            # matches app/sources/netactivity._MIN_BASELINE
ERA_START = pd.Timestamp("2017-01-01")   # comparable-regime analysis window
HORIZONS = [30, 90]
PRIMARY_HORIZON = 30    # the pre-registered promotion horizon
MIN_EPISODES = 12       # fewer non-overlapping episodes than this can never PASS
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


def _zscore(md: pd.Series, window: int = _Z_WINDOW, minp: int = _Z_MINP) -> pd.Series:
    """Per-day z of the value vs the TRAILING window that EXCLUDES it, population
    std — the exact live mapping (netactivity._latest_and_z: series[-(N+1):-1],
    pstdev, >=20 points). The old include-current sample-std z measured different
    buckets than the live layer would ever emit (train/serve skew). Flat baselines
    (std 0, common on ffilled coarse data) yield NaN, mirroring live's z=None."""
    base = md.shift(1)
    mean = base.rolling(window, min_periods=minp).mean()
    std = base.rolling(window, min_periods=minp).std(ddof=0)
    return (md - mean) / std.replace(0.0, np.nan)


def _episode_dates(dates, gap_days: int) -> list:
    """First-of-run subset: keep a date only when >= ``gap_days`` after the last
    kept one, so forward windows never overlap. Bucket days come in contiguous
    clumps — a printed n of thousands of days is really a handful of episodes."""
    kept, last = [], None
    for d in dates:
        if last is None or (d - last).days >= gap_days:
            kept.append(d)
            last = d
    return kept


def _bucket_stats(sub: pd.DataFrame, h: int) -> dict | None:
    """Non-overlapping episode hit-rate + Wilson 95% CI + mean return for a bucket."""
    col = f"fwd{h}"
    sub = sub.dropna(subset=[col])
    if sub.empty:
        return None
    eps = _episode_dates(list(sub.index), h)
    outs = sub.loc[eps, col]
    wins = int((outs > 0).sum())
    lo, hi = wilson_interval(wins, len(outs))
    return {"episodes": len(outs), "hit": (wins / len(outs) if len(outs) else None),
            "lo": lo, "hi": hi, "mean": float(outs.mean())}


def report(fwd: pd.DataFrame, chart: str, label: str) -> None:
    try:
        md = _daily(_chart_df(chart, "value"), "value")
    except Exception as exc:  # noqa: BLE001
        print(f"\n{label}\n  fetch failed: {exc}")
        return
    z = _zscore(md)
    df = fwd.join(z.rename("z"), how="inner").dropna(subset=["z"])
    df = df[df.index >= ERA_START]
    if df.empty:
        print(f"\n{label}\n  (no overlapping post-{ERA_START.year} price+metric history)")
        return
    print(f"\n{label}   ({df.index.min().date()} -> {df.index.max().date()}, days={len(df)})")
    buckets = [("z <= -1 (depressed)", df[df["z"] <= -1]),
               ("-1 < z < +1 (normal)", df[(df["z"] > -1) & (df["z"] < 1)]),
               ("z >= +1 (elevated)", df[df["z"] >= 1]),
               ("base (all days)", df)]
    stats: dict[tuple[str, int], dict | None] = {}
    for name, sub in buckets:
        parts = [f"  {name:<22}"]
        for h in HORIZONS:
            s = _bucket_stats(sub, h)
            stats[(name, h)] = s
            if s is None:
                parts.append(f"{h}d: ep=0")
            else:
                parts.append(f"{h}d: ep={s['episodes']:>3} hit={s['hit']:.0%} "
                             f"CI[{s['lo']:.2f},{s['hi']:.2f}] mean={s['mean']:+.1%}")
        print("   ".join(parts))
    base = stats.get(("base (all days)", PRIMARY_HORIZON))
    for name in ("z <= -1 (depressed)", "z >= +1 (elevated)"):
        s = stats.get((name, PRIMARY_HORIZON))
        if s is None or base is None or base["hit"] is None:
            print(f"  PROMOTE({PRIMARY_HORIZON}d) {name}: no data -> FAIL (display-only)")
            continue
        ok = s["episodes"] >= MIN_EPISODES and s["lo"] > base["hit"]
        why = (f"CI-low {s['lo']:.2f} vs base {base['hit']:.2f}, "
               f"episodes {s['episodes']} (need >={MIN_EPISODES})")
        verdict = "PASS (promote candidate)" if ok else "FAIL (display-only)"
        print(f"  PROMOTE({PRIMARY_HORIZON}d) {name}: {why} -> {verdict}")


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
    print(f"Forward BTC return by z-score bucket (post-{ERA_START.year} window)")
    print("=" * 72)
    print("z uses the LIVE definition (trailing 90d EXCLUDING the day, pstdev).\n"
          "Hit-rates + Wilson CIs are over NON-OVERLAPPING episodes (>= horizon\n"
          "apart); 'base' is the same-window every-horizon-days sample. The\n"
          f"pre-registered promotion rule: CI-low > base at {PRIMARY_HORIZON}d "
          f"with >={MIN_EPISODES} episodes.")
    for chart, label in METRICS:
        report(fwd, chart, label)

    print("\nReminder: coarse (~weekly) source ffilled to daily vs the live layer's\n"
          "genuinely daily Coin Metrics reads — bucket membership can differ at the\n"
          "margin. na_transfers / na_addr_balance have NO promotion path (no free\n"
          "deep history). A PASS here is a promote CANDIDATE, not a wiring change:\n"
          "promotion into scoring is a separate change + calibration regen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

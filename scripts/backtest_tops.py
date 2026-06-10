"""Validate TOP_THRESHOLDS (the froth/overheat sell-side) against history.

Run MANUALLY (like scripts.calibrate):  python -m scripts.backtest_tops

For every historical day with data, computes the froth score exactly as the live
path would (renormalizing over whatever indicators are present) and reports it
at the known cycle tops and bottoms, plus the distribution, plus per-indicator
sub-scores at each reference date so laggard thresholds are visible.

Honest about data depth:
  * price structure (price_to_wma200, mayer): Coinbase daily 2015+ — covers the
    Dec-2017, Apr-2021 and Nov-2021 tops and every bottom since 2015.
  * sentiment (fng): alternative.me 2018+ — covers both 2021 tops.
  * on-chain (mvrv_z, nupl, sopr, puell, realized_ratio): bitcoin-data.com full
    daily history (rate-limited ~10 req/hr — this script makes 5 such calls and
    must NOT run from any live path). Depth is whatever the API serves.
  * funding/OI: no free deep history — excluded here; live froth renormalizes
    over what's present, so the backtest mirrors a funding-less read.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import scoring                                  # noqa: E402
from app.sources import onchain                          # noqa: E402
from scripts.calibrate import _fng_history, _price_history  # noqa: E402

# Reference dates: documented cycle tops and bottoms (UTC days).
TOPS = ["2017-12-17", "2021-04-14", "2021-11-08"]
BOTTOMS = ["2018-12-15", "2020-03-16", "2022-11-21"]

_ONCHAIN_SLUGS = {
    "mvrv_z": ("mvrv-zscore", "mvrvZscore"),
    "nupl": ("nupl", "nupl"),
    "sopr": ("sopr", "sopr"),
    "puell": ("puell-multiple", "puellMultiple"),
    "_realized_price": ("realized-price", "realizedPrice"),
}


def _onchain_frame(slug: str, field: str) -> pd.DataFrame:
    rows = onchain.history(slug)
    out = []
    for r in rows:
        try:
            out.append((pd.Timestamp(r["d"]), float(r[field])))
        except (KeyError, TypeError, ValueError):
            continue
    if not out:
        return pd.DataFrame(columns=["date", slug])
    df = pd.DataFrame(out, columns=["date", field])
    # Match calibrate.py's datetime64[ns] spine (pd.Timestamp parses to [us] in
    # newer pandas, and merge_asof requires identical key dtypes).
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[ns]")
    return df.drop_duplicates("date").sort_values("date").reset_index(drop=True)


def build_frame() -> pd.DataFrame:
    px = _price_history()  # date, close, mayer, price_to_wma200 (2015+)
    print(f"  price: {len(px)} days ({px['date'].min().date()} -> {px['date'].max().date()})")
    df = px.copy()

    fng = _fng_history()
    if not fng.empty:
        print(f"  fng:   {len(fng)} days (since {fng['date'].min().date()})")
        df = pd.merge_asof(df.sort_values("date"), fng.rename(columns={"v": "fng"}),
                           on="date", direction="backward", tolerance=pd.Timedelta("3D"))

    for key, (slug, field) in _ONCHAIN_SLUGS.items():
        oc = _onchain_frame(slug, field)
        if oc.empty:
            print(f"  {key}: no history from bitcoin-data.com")
            continue
        print(f"  {key}: {len(oc)} days (since {oc['date'].min().date()})")
        col = key if key != "_realized_price" else "_realized_price"
        oc = oc.rename(columns={field: col})
        df = pd.merge_asof(df.sort_values("date"), oc, on="date",
                           direction="backward", tolerance=pd.Timedelta("3D"))

    if "_realized_price" in df.columns:
        df["realized_ratio"] = df["close"] / df["_realized_price"]
    return df


_FROTH_KEYS = list(scoring.TOP_THRESHOLDS)


def froth_row(row: pd.Series) -> tuple[float | None, dict]:
    readings = {k: (None if k not in row or pd.isna(row[k]) else float(row[k]))
                for k in _FROTH_KEYS}
    out = scoring.froth_score(readings)
    return out["score"], out["subscores"]


_CACHE = Path(__file__).with_name("_tops_cache.csv")


def main() -> int:
    # bitcoin-data.com allows ~10 req/hr and one build costs 5 — cache the joined
    # frame so threshold iteration doesn't burn the budget. Delete the file to refresh.
    if _CACHE.exists():
        print(f"Using cached frame {_CACHE.name} (delete to refetch)")
        df = pd.read_csv(_CACHE, parse_dates=["date"])
    else:
        print("Building joined history...")
        df = build_frame()
        df.to_csv(_CACHE, index=False)
    scores, subs_list = [], []
    for _, row in df.iterrows():
        s, subs = froth_row(row)
        scores.append(s)
        subs_list.append(subs)
    df["froth"] = scores

    have = df.dropna(subset=["froth"])
    print(f"\nFroth computed on {len(have)} days "
          f"({have['date'].min().date()} -> {have['date'].max().date()})")

    def at(date_str: str) -> None:
        d = pd.Timestamp(date_str)
        idx = (df["date"] - d).abs().idxmin()
        row = df.loc[idx]
        subs = subs_list[idx]
        lit = [k for k, v in subs.items() if v is not None and v >= scoring.IN_ZONE_THRESHOLD]
        parts = ", ".join(f"{k}={v:.2f}" for k, v in subs.items() if v is not None)
        raws = ", ".join(f"{k}={row[k]:.3f}" for k in _FROTH_KEYS
                         if k in row and not pd.isna(row[k]))
        print(f"  {row['date'].date()}  close=${row['close']:>9,.0f}  "
              f"froth={row['froth'] if row['froth'] is not None else float('nan'):5.1f}  "
              f"lit={lit or '-'}\n      subs: {parts}\n      raws: {raws}")

    print("\n=== TOPS (want HIGH froth) ===")
    for d in TOPS:
        at(d)
    # The most recent cycle top: highest close in the data (cycle compression
    # means each top is weaker on these ratios — the key modern test case).
    recent = df.loc[df["close"].idxmax()]
    print(f"--- most recent cycle top by price (max close) ---")
    at(str(recent["date"].date()))

    print("\n=== BOTTOMS (want ~0 froth) ===")
    for d in BOTTOMS:
        at(d)

    print("\n=== TODAY ===")
    at(str(df["date"].max().date()))

    print("\n=== Per-indicator extremes by cycle-top window (95th pct / max of raw) ===")
    windows = {"2017H2": ("2017-07-01", "2018-01-15"),
               "2021": ("2021-01-01", "2021-12-31"),
               "2024H2-2025": ("2024-07-01", "2026-01-31")}
    for name, (lo, hi) in windows.items():
        w = df[(df["date"] >= lo) & (df["date"] <= hi)]
        parts = []
        for k in _FROTH_KEYS:
            if k in w.columns and w[k].notna().any():
                parts.append(f"{k}: p95={w[k].quantile(0.95):.3f} max={w[k].max():.3f}")
        print(f"  {name}:")
        for p in parts:
            print(f"      {p}")

    print("\n=== Distribution ===")
    f = have.set_index("date")["froth"]
    for yr, grp in f.groupby(f.index.year):
        print(f"  {yr}: max={grp.max():5.1f}  days>=50: {(grp >= 50).sum():4d}  days>=75: {(grp >= 75).sum():4d}")
    print(f"  overall: days>=50 {(f >= 50).mean() * 100:.1f}%  days>=75 {(f >= 75).mean() * 100:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Offline edge-evaluation for the CoinDesk/CryptoCompare context reads.

The "search for edge" step from the data-source plan. Pulls long daily history for
BTC price + each network-activity / social metric from the legacy CryptoCompare
endpoints, computes each metric's trailing-90d z-score per day, then reports
forward 30/90d BTC returns bucketed by that z (extreme-low / mid / extreme-high)
against the all-days baseline. If an extreme bucket shows a materially better mean
forward return AND hit-rate than baseline, that metric is a candidate to PROMOTE
into scoring (a separate change + a calibration.json regen). Otherwise: no edge —
keep it display-only.

This is a heuristic scan, not a proof: daily rows are treated as contiguous for the
forward-return shift, and three-ish cycles is not a dataset. Read it as a smell
test, not a fitted parameter.

Needs COINDESK_API_KEY. Prints a clear message and exits 0 if the key or data is
absent (so it's safe to wire into CI / a Makefile).

    python -m scripts.eval_coindesk          # from the project root
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

# Allow running as `python scripts/eval_coindesk.py` as well as `-m scripts.eval_coindesk`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config  # noqa: E402
from app.sources.coindesk import BASE, BTC_COIN_ID  # noqa: E402

_PAGES = 4          # 4 x 2000-day pages ~= 20y, far beyond CC's BTC history (covers it all)
_LIMIT = 2000       # CryptoCompare per-call max
_Z_WINDOW = 90      # trailing days for the rolling z-score
_Z_MINP = 60        # min periods before a z is emitted

# (endpoint path, base params, [(field, label), ...]). v2/histoday + blockchain nest
# their list under Data.Data; social puts it directly under Data — _extract_rows copes.
PRICE = ("/data/v2/histoday", {"fsym": "BTC", "tsym": "USD"})
METRICS = [
    ("/data/blockchain/histo/day", {"fsym": "BTC"}, [
        ("active_addresses", "Active addresses"),
        ("large_transaction_count", "Large-txn count (whales)"),
        ("new_addresses", "New addresses"),
        ("transaction_count", "Transaction count"),
    ]),
    ("/data/social/coin/histo/day", {"coinId": BTC_COIN_ID}, [
        ("reddit_active_users", "Reddit active users"),
    ]),
]


def _get(url: str, params: dict, key: str) -> dict | None:
    try:
        r = requests.get(url, params=params,
                         headers={"authorization": f"Apikey {key}"}, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001
        print(f"  fetch failed ({url}): {exc}")
        return None


def _extract_rows(data) -> list[dict]:
    body = data.get("Data") if isinstance(data, dict) else None
    rows = body.get("Data") if isinstance(body, dict) else body
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _paginate(path: str, base_params: dict, key: str) -> list[dict]:
    """Page backward via ``toTs`` to assemble the full daily history, oldest-first."""
    by_time: dict[int, dict] = {}
    to_ts: int | None = None
    for _ in range(_PAGES):
        params = dict(base_params, limit=_LIMIT)
        if to_ts is not None:
            params["toTs"] = to_ts
        rows = _extract_rows(_get(f"{BASE}{path}", params, key))
        if not rows:
            break
        times = [int(r["time"]) for r in rows if r.get("time") is not None]
        if not times:
            break
        for r in rows:
            if r.get("time") is not None:
                by_time[int(r["time"])] = r
        nxt = min(times) - 1
        if to_ts is not None and nxt >= to_ts:   # no progress -> history exhausted
            break
        to_ts = nxt
    return [by_time[t] for t in sorted(by_time)]


def _series_df(rows: list[dict], field: str, col: str) -> pd.DataFrame:
    """[date, col] frame, dropping None/zero (CryptoCompare pads missing days with 0)."""
    recs = []
    for r in rows:
        t, v = r.get("time"), r.get(field)
        if t is None or v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v == 0:
            continue
        recs.append((pd.to_datetime(int(t), unit="s"), v))
    if not recs:
        return pd.DataFrame(columns=["date", col])
    return (pd.DataFrame(recs, columns=["date", col])
            .drop_duplicates("date").sort_values("date").reset_index(drop=True))


def _bucket_stats(sub: pd.DataFrame) -> str:
    if sub.empty:
        return f"{'n=0':>34}"
    return (f"n={len(sub):>4}  "
            f"30d {sub['fwd30'].mean():>+6.1%} ({(sub['fwd30'] > 0).mean():>4.0%} up)  "
            f"90d {sub['fwd90'].mean():>+6.1%} ({(sub['fwd90'] > 0).mean():>4.0%} up)")


def report(price: pd.DataFrame, rows: list[dict], field: str, label: str) -> None:
    metric = _series_df(rows, field, "value")
    if metric.empty:
        print(f"\n{label}\n  (no data)")
        return
    metric["z"] = ((metric["value"] - metric["value"].rolling(_Z_WINDOW, min_periods=_Z_MINP).mean())
                   / metric["value"].rolling(_Z_WINDOW, min_periods=_Z_MINP).std())
    df = price.merge(metric[["date", "z"]], on="date", how="inner").dropna(subset=["z", "fwd90"])
    if df.empty:
        print(f"\n{label}\n  (no overlapping price+metric history)")
        return
    print(f"\n{label}   ({df['date'].min().date()} -> {df['date'].max().date()}, n={len(df)})")
    print(f"  {'z <= -1 (depressed)':<22} {_bucket_stats(df[df['z'] <= -1])}")
    print(f"  {'-1 < z < +1 (normal)':<22} {_bucket_stats(df[(df['z'] > -1) & (df['z'] < 1)])}")
    print(f"  {'z >= +1 (elevated)':<22} {_bucket_stats(df[df['z'] >= 1])}")
    print(f"  {'ALL days (baseline)':<22} {_bucket_stats(df)}")


def main() -> int:
    cfg = load_config()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"CoinDesk/CryptoCompare context edge-evaluation  ({stamp})")
    if not cfg.coindesk_api_key:
        print("\n(no COINDESK_API_KEY set — nothing to evaluate; set the key and re-run)")
        return 0
    key = cfg.coindesk_api_key

    print("Pulling daily BTC price history (CryptoCompare histoday)...")
    price = _series_df(_paginate(*PRICE, key), "close", "close")
    if price.empty:
        print("  no price history returned — aborting")
        return 0
    price["fwd30"] = price["close"].shift(-30) / price["close"] - 1.0
    price["fwd90"] = price["close"].shift(-90) / price["close"] - 1.0
    print(f"  got {len(price)} daily closes "
          f"({price['date'].min().date()} -> {price['date'].max().date()})")

    print("\n" + "=" * 72)
    print("Forward BTC return by metric z-score bucket  (search for edge)")
    print("=" * 72)
    print("Each metric's latest value vs its trailing-90d distribution. An extreme\n"
          "bucket that beats the baseline on BOTH mean return and hit-rate is a\n"
          "promote candidate; otherwise keep it display-only.")
    for path, base_params, fields in METRICS:
        rows = _paginate(path, base_params, key)
        for field, label in fields:
            report(price, rows, field, label)

    print("\nReminder: heuristic scan over ~one-to-few cycles, daily rows treated as\n"
          "contiguous for the shift. A signal here is a hypothesis to calibrate, not\n"
          "a proven edge — do not wire into scoring on this alone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

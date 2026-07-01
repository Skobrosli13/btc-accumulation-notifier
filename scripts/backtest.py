"""One-off historical calibration (run manually).

Pulls long daily price history (CoinGecko, free) and, if a Glassnode key is set,
on-chain history too, then:

  1. Prints what each indicator read at the 2015 / 2018 / 2022 cycle bottoms,
     to sanity-check the default thresholds in app/scoring.py.
  2. Reports each indicator's false-positive behavior — how often it entered its
     "bottom zone" away from an actual bottom.
  3. Outputs a suggested threshold set, flagged n=3 and OVERFIT-PRONE.

Indicators are chosen by economic logic, NOT curve-fit to three lows. The live
`runs` ledger also accumulates real data, so thresholds can be revisited as the
zone develops.

    python -m scripts.backtest          # from the project root
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests

# Allow running as `python scripts/backtest.py` as well as `-m scripts.backtest`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import scoring  # noqa: E402
from app.config import load_config  # noqa: E402

COINGECKO = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
GLASSNODE_BASE = "https://api.glassnode.com/v1/metrics"

# Approximate cycle-bottom dates (the n=3 we have).
BOTTOMS = {
    "2015": date(2015, 1, 14),
    "2018": date(2018, 12, 15),
    "2022": date(2022, 11, 21),
}
NEAR_BOTTOM_WINDOW_DAYS = 90  # +/- window treated as "near a real bottom"


def _coingecko_daily() -> pd.DataFrame:
    """Daily close history (oldest->newest).

    Tries the full history (days='max', needs a CoinGecko key on most plans),
    then falls back to the free-tier window (365d). With only 365d the 2015/2018
    bottoms have no data and are reported as "insufficient history" rather than
    silently dropped -- for a true multi-cycle calibration, run this where long
    history is available (a CoinGecko key, or Binance from an unrestricted region).
    """
    prices = []
    for days in ("max", "365"):
        r = requests.get(COINGECKO, params={"vs_currency": "usd", "days": days}, timeout=30)
        if r.status_code == 200:
            prices = r.json().get("prices", [])
            if days == "365":
                print("  NOTE: only 365d of free history available; "
                      "older bottoms will show as insufficient.")
            break
    if not prices:
        raise RuntimeError("CoinGecko returned no price history (max and 365d both failed)")
    df = pd.DataFrame(prices, columns=["ts", "close"])
    df["date"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_localize(None).dt.normalize()
    df = df.groupby("date", as_index=False)["close"].last()
    return df


def _price_structure_series(daily: pd.DataFrame) -> pd.DataFrame:
    """Add rolling 200DMA, 200WMA (1400d ~= 200w), Mayer, price/200WMA columns.

    The 200WMA requires the FULL 1400-day window (min_periods=1400): the live path
    (app/sources/price.py) returns None below 200 weekly closes, so a short-history
    "200WMA" here would be a materially different indicator mislabeled as the one
    the live scorer measures. Bottoms without the full window print
    "(insufficient history)" instead — honest degradation over a fabricated read."""
    df = daily.copy().sort_values("date").reset_index(drop=True)
    df["dma200"] = df["close"].rolling(200, min_periods=200).mean()
    df["wma200"] = df["close"].rolling(1400, min_periods=1400).mean()  # full 200 weeks only
    df["mayer"] = df["close"] / df["dma200"]
    df["price_to_wma200"] = df["close"] / df["wma200"]
    return df


def _row_on_or_before(df: pd.DataFrame, when: date) -> pd.Series | None:
    ts = pd.Timestamp(when)
    sub = df[df["date"] <= ts]
    return sub.iloc[-1] if not sub.empty else None


def _glassnode_history(path: str, api_key: str) -> pd.DataFrame:
    r = requests.get(f"{GLASSNODE_BASE}/{path}",
                     params={"a": "BTC", "i": "24h"},
                     headers={"X-Api-Key": api_key}, timeout=30)
    r.raise_for_status()
    rows = r.json()
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["t"], unit="s", utc=True).dt.tz_localize(None).dt.normalize()
    df = df.rename(columns={"v": "value"})[["date", "value"]]
    return df


def _print_header(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def report_price_structure(df: pd.DataFrame) -> None:
    _print_header("Price-structure indicators at the three bottoms (FREE tier)")
    th = scoring.THRESHOLDS
    print(f"{'bottom':<8} {'price':>12} {'mayer':>8} {'mayer_sc':>9} "
          f"{'p/200wma':>9} {'wma_sc':>8}")
    for name, when in BOTTOMS.items():
        row = _row_on_or_before(df, when)
        if row is None or pd.isna(row.get("mayer")) or pd.isna(row.get("price_to_wma200")):
            print(f"{name:<8} {'(insufficient history)':>40}")
            continue
        mayer_sc = scoring.linear_score(row["mayer"], th["mayer"]["neutral"], th["mayer"]["extreme"])
        wma_sc = scoring.linear_score(row["price_to_wma200"],
                                      th["price_to_wma200"]["neutral"],
                                      th["price_to_wma200"]["extreme"])
        print(f"{name:<8} {row['close']:>12,.0f} {row['mayer']:>8.2f} {mayer_sc:>9.2f} "
              f"{row['price_to_wma200']:>9.2f} {wma_sc:>8.2f}")


def report_false_positives(df: pd.DataFrame) -> None:
    _print_header("False-positive behavior (free price-structure indicators)")
    print("Fraction of days an indicator's sub-score >= "
          f"{scoring.IN_ZONE_THRESHOLD} while NOT within "
          f"+/-{NEAR_BOTTOM_WINDOW_DAYS}d of a real bottom.\n")

    near = pd.Series(False, index=df.index)
    for when in BOTTOMS.values():
        lo = pd.Timestamp(when) - pd.Timedelta(days=NEAR_BOTTOM_WINDOW_DAYS)
        hi = pd.Timestamp(when) + pd.Timedelta(days=NEAR_BOTTOM_WINDOW_DAYS)
        near |= (df["date"] >= lo) & (df["date"] <= hi)

    th = scoring.THRESHOLDS
    for key in ("mayer", "price_to_wma200"):
        valid = df[df[key].notna()]
        if valid.empty:
            print(f"{key:<16} (no data)")
            continue
        sc = valid[key].apply(lambda v: scoring.linear_score(
            v, th[key]["neutral"], th[key]["extreme"]))
        in_zone = sc >= scoring.IN_ZONE_THRESHOLD
        away = in_zone & ~near.loc[valid.index]
        denom = (~near.loc[valid.index]).sum()
        rate = (away.sum() / denom) if denom else 0.0
        print(f"{key:<16} in-zone days: {int(in_zone.sum()):>5}  "
              f"false-positive rate (away from bottoms): {rate:>6.1%}")


def report_onchain(cfg) -> None:
    if not cfg.glassnode_api_key:
        print("\n(no GLASSNODE_API_KEY set - skipping on-chain historical readings)")
        return
    _print_header("On-chain indicators at the three bottoms (Glassnode)")
    metrics = {
        "mvrv_z": "market/mvrv_z_score",
        "nupl": "indicators/net_unrealized_profit_loss",
        "sopr": "indicators/sopr",
        "puell": "indicators/puell_multiple",
    }
    th = scoring.THRESHOLDS
    for key, path in metrics.items():
        try:
            hist = _glassnode_history(path, cfg.glassnode_api_key)
        except Exception as exc:  # noqa: BLE001
            print(f"{key:<10} fetch failed: {exc}")
            continue
        line = [f"{key:<10}"]
        for name, when in BOTTOMS.items():
            row = _row_on_or_before(hist, when)
            if row is None:
                line.append(f"{name}=n/a")
                continue
            sc = scoring.linear_score(row["value"], th[key]["neutral"], th[key]["extreme"])
            line.append(f"{name}={row['value']:.3f}(sc {sc:.2f})")
        print("  ".join(line))


def suggest_thresholds(df: pd.DataFrame) -> None:
    _print_header("Suggested thresholds  --  n=3, OVERFIT-PRONE, sanity-check only")
    print("Indicator values observed AT the three bottoms (use as a rough 'extreme' floor,\n"
          "NOT as a fitted parameter - favor economic logic over these three points):\n")
    for key in ("mayer", "price_to_wma200"):
        vals = []
        for when in BOTTOMS.values():
            row = _row_on_or_before(df, when)
            if row is not None and not pd.isna(row.get(key)):
                vals.append(float(row[key]))
        if vals:
            print(f"  {key:<16} bottoms={[round(v, 2) for v in vals]}  "
                  f"min={min(vals):.2f} median={sorted(vals)[len(vals)//2]:.2f}  "
                  f"(current extreme={scoring.THRESHOLDS[key]['extreme']})")
    print("\nReminder: three cycles is not a dataset. ETFs + macro liquidity have shifted the\n"
          "regime; do not assume metrics must reach 2018 depths before a bottom. Calibrate,\n"
          "don't dogmatically wait.")


def main() -> int:
    cfg = load_config()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"BTC accumulation-zone backtest / calibration  ({stamp})")
    print("Pulling long daily history from CoinGecko (free)...")
    try:
        daily = _coingecko_daily()
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to fetch price history: {exc}")
        return 1
    print(f"  got {len(daily)} daily closes "
          f"({daily['date'].min().date()} -> {daily['date'].max().date()})")

    df = _price_structure_series(daily)
    report_price_structure(df)
    report_false_positives(df)
    report_onchain(cfg)
    suggest_thresholds(df)
    return 0


if __name__ == "__main__":
    sys.exit(main())

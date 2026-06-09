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
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import scoring                      # noqa: E402
from app.config import load_config           # noqa: E402
from app.sources import exchange, macro      # noqa: E402

PROBS = [0.0, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 1.0]
APP_DIR = Path(__file__).resolve().parents[1] / "app"
FORWARD_WEEKS = [13, 26, 52]   # ~3m / 6m / 12m
MIN_HISTORY = 60               # don't calibrate an indicator with fewer points
# Must reach back to here to percentile-calibrate (so the history spans the 2022
# bottom + its run-up — otherwise percentile-rank on one regime misleads).
SPAN_CUTOFF = pd.Timestamp("2021-06-01")


# --- data pulls --------------------------------------------------------------

def _weekly_price() -> pd.DataFrame:
    """Deep weekly closes -> date, close, price_to_wma200 (200-week MA)."""
    df = exchange.klines_history("1w", 800, "BTC-USDT")
    out = df[["open_time", "close"]].copy()
    out["date"] = (pd.to_datetime(out["open_time"]).dt.tz_localize(None)
                   .dt.normalize().astype("datetime64[ns]"))
    out = out.sort_values("date").reset_index(drop=True)
    out["wma200"] = out["close"].rolling(200, min_periods=104).mean()
    out["price_to_wma200"] = out["close"] / out["wma200"]
    return out[["date", "close", "price_to_wma200"]]


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

    horizons = {}
    signal_idx = [i for i, t in enumerate(tiers) if t in ("ACCUMULATE", "DEEP_VALUE")]
    for h in FORWARD_WEEKS:
        horizons[f"{h}w"] = {
            "signal_hit_rate": winrate(signal_idx, h),
            "base_rate": winrate(list(range(len(closes))), h),
        }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "price+macro backbone, expanding-window percentile, timing-neutral",
        "weeks": len(closes),
        "from": str(rows["date"].iloc[0].date()) if len(rows) else None,
        "to": str(rows["date"].iloc[-1].date()) if len(rows) else None,
        "signal_weeks": len(signal_idx),
        "horizons": horizons,
        "caveats": [
            "Backbone only: price-vs-200WMA + macro (the deep multi-cycle indicators).",
            "On-chain (~2022+), sentiment, derivatives use economic thresholds and are NOT in this test.",
            "Timing multiplier neutralized; cycle context excluded.",
            "Past behavior is not a forecast.",
        ],
    }


def main() -> int:
    cfg = load_config()
    print("Calibrating (deep multi-cycle indicators only)...")
    weekly = _weekly_price()
    print(f"  weekly closes: {len(weekly)} "
          f"({weekly['date'].min().date()} -> {weekly['date'].max().date()})")
    macro = _macro_history(cfg)

    # Breakpoints from each indicator's full native-frequency history (deep as available).
    raw = {"price_to_wma200":
           weekly[["date", "price_to_wma200"]].rename(columns={"price_to_wma200": "v"})}
    for name, df in macro.items():
        raw[name] = df.rename(columns={name: "v"})
    calib = _emit_calibration(raw)
    (APP_DIR / "calibration.json").write_text(json.dumps(calib, indent=2))
    print(f"  wrote app/calibration.json ({len(calib['indicators'])} indicators)")

    # Track record uses ONLY the calibrated indicators (the percentile backbone).
    weekly = weekly.sort_values("date").reset_index(drop=True)
    for name, df in macro.items():
        weekly = pd.merge_asof(weekly, df.sort_values("date"), on="date", direction="backward")
    calibrated = [k for k in calib["indicators"] if k == "price_to_wma200" or k in weekly.columns]
    track = _track_record(weekly, cfg, calibrated)
    (APP_DIR / "track_record.json").write_text(json.dumps(track, indent=2))
    print(f"  wrote app/track_record.json: {track['horizons']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

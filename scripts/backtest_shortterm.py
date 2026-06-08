"""One-off validation of the short-term swing triggers against OKX history.

For each timeframe it walks the closed-candle history, fires the same
``shortterm.detect_triggers`` used live (price-based triggers; funding/OI are
live-only), and reports per-trigger: how often it fired, the direction-adjusted
forward return at a couple of horizons, and a win-rate (share of fires that moved
the trigger's way). This is the gate the plan requires BEFORE trusting alerts.

    python -m scripts.backtest_shortterm            # from the project root

Small-sample caveat applies: this is a sanity check / threshold-tuning aid, not a
promise of edge. Triggers are chosen by standard economic logic, not curve-fit.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import shortterm  # noqa: E402
from app.config import load_config  # noqa: E402
from app.sources import exchange  # noqa: E402

# candles to pull, and forward horizons (in candles) per timeframe
PLAN = {
    "4h": {"total": 1500, "horizons": [6, 12]},   # 24h, 48h
    "1d": {"total": 1000, "horizons": [3, 7]},     # 3d, 1w
}
MIN_LOOKBACK = 35  # need enough bars for EMA/RSI/BB to be defined


def _walk(df, cfg, horizons):
    """Return {trigger_key: {'dir':..., 'n':int, h: [returns...]}}."""
    closes = df["close"].tolist()
    results: dict[str, dict] = {}
    n = len(df)
    maxh = max(horizons)
    for i in range(MIN_LOOKBACK, n - maxh):
        window = df.iloc[: i + 1]
        for trig in shortterm.detect_triggers(window, cfg):
            rec = results.setdefault(trig.key, {"dir": trig.direction, "n": 0,
                                                **{h: [] for h in horizons}})
            rec["n"] += 1
            entry = closes[i]
            for h in horizons:
                fwd = closes[i + h] / entry - 1.0
                # direction-adjusted: profit if price moved the trigger's way
                rec[h].append(fwd if trig.direction == "BUY" else -fwd)
    return results


def _report(tf, results, horizons):
    print(f"\n=== {tf} ===")
    if not results:
        print("  (no triggers fired over the sample)")
        return
    hdr = f"{'trigger':<26}{'dir':<5}{'n':>5}"
    for h in horizons:
        hdr += f"{'win%@'+str(h):>9}{'avg%@'+str(h):>9}"
    print(hdr)
    for key in sorted(results):
        rec = results[key]
        line = f"{key:<26}{rec['dir']:<5}{rec['n']:>5}"
        for h in horizons:
            rets = rec[h]
            if rets:
                win = sum(1 for r in rets if r > 0) / len(rets) * 100
                avg = sum(rets) / len(rets) * 100
                line += f"{win:>9.0f}{avg:>9.2f}"
            else:
                line += f"{'-':>9}{'-':>9}"
        print(line)


def main() -> int:
    cfg = load_config()
    print("Short-term trigger backtest (OKX history). "
          "Win% = share of fires that moved the trigger's way; avg% = mean direction-adjusted return.")
    print("Small sample; sanity check only - favor economic logic over these numbers.")
    for tf, spec in PLAN.items():
        if tf not in cfg.st_timeframes:
            continue
        try:
            df = exchange.klines_history(tf, spec["total"], cfg.symbol)
        except Exception as exc:  # noqa: BLE001
            print(f"\n=== {tf} ===\n  fetch failed: {exc}")
            continue
        df = exchange.closed_only(df)
        print(f"\n(fetched {len(df)} closed {tf} candles "
              f"{df['open_time'].iloc[0].date()} -> {df['open_time'].iloc[-1].date()})")
        _report(tf, _walk(df, cfg, spec["horizons"]), spec["horizons"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

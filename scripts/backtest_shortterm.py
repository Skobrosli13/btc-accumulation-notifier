"""Validation of the short-term swing triggers against OKX history.

This walks the closed-candle history and REPLAYS THE LIVE ALERT PATH
(``app/collect_once.run``): regime suppression + the confluence gate + per-(key,
timeframe) cooldown / same-candle dedup, recomputing indicators on the same
~300-candle window the collector uses. It then reports per-trigger win-rates for
the ALERTED population (what the user actually receives) with a Wilson 95% CI and
the unconditional base rate, and — clearly labeled — the RAW (pre-gate) numbers
the old script reported, so the inflation is visible.

    python -m scripts.backtest_shortterm            # from the project root

Where reachable it pulls multi-year 4h history (OKX ``history-candles`` paginates
back years) and splits win-rates by 200DMA regime (bull / bear). Costs: a 10 bps
round-trip is deducted from every forward return (see st_validation.ROUND_TRIP_COST).

Small-sample caveat applies: a sanity check / threshold-tuning aid, not a promise
of edge. Triggers are chosen by economic logic, not curve-fit.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from app.config import load_config  # noqa: E402
from app.sources import exchange  # noqa: E402
from scripts import st_validation as stv  # noqa: E402
from scripts.st_history import deep_klines, daily_regime_series  # noqa: E402

# candles to pull, and forward horizons (in candles) per timeframe.
# 4h pulls deep history (history-candles paginates years back); 1d caps lower.
PLAN = {
    "4h": {"total": 14000, "horizons": [6, 12]},   # 24h, 48h
    "1d": {"total": 1200, "horizons": [3, 7]},      # 3d, 1w
}


def _adj_return(closes, i: int, h: int, direction: str) -> float | None:
    """Direction-adjusted forward return over ``h`` bars, net of round-trip cost."""
    if i + h >= len(closes):
        return None
    fwd = closes[i + h] / closes[i] - 1.0
    raw = fwd if direction == "BUY" else -fwd
    return raw - stv.ROUND_TRIP_COST


def _score(events, closes, horizons) -> dict:
    """{trigger_key: {'dir', 'n', h: {'win','avg','wilson','base'}}} for a population."""
    by_key: dict[str, dict] = {}
    for e in events:
        by_key.setdefault(e.key, {"dir": e.direction, "events": []})["events"].append(e)
    out: dict[str, dict] = {}
    for key, info in by_key.items():
        direction = info["dir"]
        rec: dict = {"dir": direction, "n": len(info["events"])}
        for h in horizons:
            rets = [r for r in (_adj_return(closes, e.index, h, direction)
                                for e in info["events"]) if r is not None]
            wins = sum(1 for r in rets if r > 0)
            base = stv.base_rate(closes, direction, h)
            lo, hi = stv.wilson_interval(wins, len(rets))
            rec[h] = {
                "n": len(rets),
                "win": (wins / len(rets) if rets else None),
                "avg": (sum(rets) / len(rets) if rets else None),
                "wilson": (lo, hi),
                "base": base,
            }
        out[key] = rec
    return out


def _report(label: str, scored: dict, horizons) -> None:
    print(f"\n  --- {label} ---")
    if not scored:
        print("    (none)")
        return
    hdr = f"  {'trigger':<26}{'dir':<5}{'n':>5}"
    for h in horizons:
        hdr += f"{'win%@'+str(h):>8}{'CI95':>14}{'base%':>7}{'avg%':>8}"
    print(hdr)
    for key in sorted(scored):
        rec = scored[key]
        line = f"  {key:<26}{rec['dir']:<5}{rec['n']:>5}"
        for h in horizons:
            c = rec[h]
            if c["win"] is None:
                line += f"{'-':>8}{'-':>14}{'-':>7}{'-':>8}"
                continue
            ci = f"[{c['wilson'][0]*100:.0f}-{c['wilson'][1]*100:.0f}]"
            base = f"{c['base']*100:.0f}" if c["base"] is not None else "-"
            flag = "*" if c["n"] < stv.MIN_N else " "
            line += f"{c['win']*100:>7.0f}{flag}{ci:>14}{base:>7}{c['avg']*100:>8.2f}"
        print(line)


def _regime_split(events, regime_series, frame) -> dict:
    """Tag each alerted event with its 200DMA regime and count per bucket."""
    buckets = {"bull": [], "bear": [], "unknown": []}
    for e in events:
        when = frame["open_time"].iloc[e.index]
        reg = stv._regime_at(regime_series, when) if regime_series is not None else "unknown"
        buckets.setdefault(reg, []).append(e)
    return buckets


def main() -> int:
    cfg = load_config()
    print("Short-term trigger backtest (OKX history) — replays the LIVE alert path.")
    print("ALERTED = post regime+confluence+cooldown (what users get). RAW = every "
          "detect_triggers fire (the old, inflated number). win% net of "
          f"{stv.ROUND_TRIP_COST*100:.1f}% round-trip cost; base% = unconditional move rate; "
          "CI95 = Wilson; * = n<%d (not meaningful)." % stv.MIN_N)

    # Deep daily series for the 200DMA regime (shared across timeframes).
    regime_series = None
    try:
        regime_series = daily_regime_series(cfg.symbol)
    except Exception as exc:  # noqa: BLE001
        print(f"\n(could not build daily regime series: {exc} — regime splits unavailable)")

    for tf, spec in PLAN.items():
        if tf not in cfg.st_timeframes:
            continue
        try:
            df = exchange.closed_only(deep_klines(tf, spec["total"], cfg.symbol))
        except Exception as exc:  # noqa: BLE001
            print(f"\n=== {tf} ===\n  fetch failed: {exc}")
            continue
        print(f"\n=== {tf} ===")
        print(f"  fetched {len(df)} closed candles "
              f"{df['open_time'].iloc[0].date()} -> {df['open_time'].iloc[-1].date()}")
        closes = df["close"].tolist()
        maxh = max(spec["horizons"])
        replay = stv.replay_alerts(df, cfg, tf, regime_series=regime_series, maxh=maxh)
        print(f"  raw fires: {len(replay.raw)}   alerted (post-gate): {len(replay.alerted)}   "
              f"({(1 - len(replay.alerted)/len(replay.raw))*100:.0f}% filtered)"
              if replay.raw else "  no fires")

        _report("ALERTED population (what users see)",
                _score(replay.alerted, closes, spec["horizons"]), spec["horizons"])
        _report("RAW population (pre-gate, for comparison — OVERSTATES)",
                _score(replay.raw, closes, spec["horizons"]), spec["horizons"])

        if regime_series is not None and replay.alerted:
            buckets = _regime_split(replay.alerted, regime_series, df)
            for reg in ("bull", "bear"):
                if buckets.get(reg):
                    _report(f"ALERTED in {reg} regime",
                            _score(buckets[reg], closes, spec["horizons"]), spec["horizons"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

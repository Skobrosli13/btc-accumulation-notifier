"""Forward-test backtest of the Coinalyze order-flow triggers.

Replays ``flow.detect_flow_triggers`` over Coinalyze history exactly as the live
collector computes it (same window, same closed-bar inputs, no look-ahead) and
reports per-trigger win-rate vs the unconditional base rate with a Wilson CI —
the SAME honest yardstick ``scripts/backtest_shortterm`` uses for the swing
triggers.

HONEST FRAMING — read the CI and the base-rate comparison, not the point win-rate:
  * ONE venue (Binance perp via Coinalyze), ~11 months of 4h bars (the free-tier
    history cap), so per-trigger samples are small.
  * Measures the RAW flow fires (pre-confluence). Live, these are further gated by
    the confluence / cooldown machinery, which can only REDUCE fires.
  * A sanity check / threshold-tuning aid, NOT a promise of edge — consistent with
    the established BTC short-term no-edge finding.

    python -m scripts.backtest_flow
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import flow                                # noqa: E402
from app.config import load_config                  # noqa: E402
from app.sources import coinalyze                   # noqa: E402
from scripts.st_validation import (                 # noqa: E402
    ROUND_TRIP_COST, base_rate, cell_stats)

TF = "4hour"
REQUEST_HOURS = 4000 * 4          # request well past the free-tier cap (~2006 bars)
HORIZONS = [1, 3, 6]              # forward bars on 4h => 4h / 12h / 24h


def _collapse(indices: list[int], gap: int) -> list[int]:
    """Keep the first fire of each run within ``gap`` bars — overlapping forward
    windows are autocorrelated, so counting every fire inflates n and tightens the
    CI dishonestly (same de-correlation as st_validation.collapse_episodes)."""
    out: list[int] = []
    last = None
    for i in sorted(indices):
        if last is None or (i - last) > gap:
            out.append(i)
        last = i
    return out


def _adj_return(closes: list[float], i: int, h: int, direction: str) -> float:
    fwd = closes[i + h] / closes[i] - 1.0
    return (fwd if direction == "BUY" else -fwd) - ROUND_TRIP_COST


def main() -> int:
    cfg = load_config()
    if not cfg.coinalyze_api_key:
        print("COINALYZE_API_KEY not set — nothing to backtest.")
        return 1
    sym, key = cfg.coinalyze_symbol, cfg.coinalyze_api_key
    ohlcv = coinalyze.ohlcv_history(sym, TF, REQUEST_HOURS, key)
    oi = coinalyze.oi_history(sym, TF, REQUEST_HOURS, key)
    liq = coinalyze.liquidations_history(sym, TF, REQUEST_HOURS, key)
    if len(ohlcv) < 100:
        print(f"insufficient history ({len(ohlcv)} bars) — check symbol/plan.")
        return 1

    closes = [r["close"] for r in ohlcv]
    oi_by_ts = {r["ts"]: r["oi"] for r in oi}
    liq_by_ts = {r["ts"]: r for r in liq}
    window_n = cfg.flow_cvd_lookback + 5         # the (lookback+5)-bar window the live collector fetches
    n = len(ohlcv)

    # Replay: at each closed bar t, reconstruct the EXACT inputs the live collector
    # would have had (the trailing window only) and record every flow fire.
    fires: dict[str, dict] = {}
    for t in range(window_n, n):
        ow = ohlcv[t - window_n + 1: t + 1]
        cvd_df = flow.build_cvd(ow)
        oi_rows = [{"ts": r["ts"], "oi": oi_by_ts[r["ts"]]} for r in ow if r["ts"] in oi_by_ts]
        part = flow.participant_aligned(ow, oi_rows, cfg.flow_oi_bar_surge_pct)
        liq_rows = [liq_by_ts[r["ts"]] for r in ow if r["ts"] in liq_by_ts]
        liq_flush = flow.liquidation_flush(liq_rows, cfg.flow_liq_spike_mult, cfg.flow_liq_min_usd)
        for trig in flow.detect_flow_triggers(cvd_df, part, liq_flush, cfg):
            d = fires.setdefault(trig.key, {"direction": trig.direction, "idx": []})
            d["idx"].append(t)

    d0 = datetime.fromtimestamp(ohlcv[0]["ts"] / 1000, timezone.utc).date()
    d1 = datetime.fromtimestamp(ohlcv[-1]["ts"] / 1000, timezone.utc).date()
    print(f"Flow trigger backtest - {sym} {TF}")
    print(f"bars: {n}   span: {d0} .. {d1}   cost: {ROUND_TRIP_COST * 100:.2f}%")
    print("RAW fires (pre-confluence) - one venue - read the win-rate CI vs base, not the point.\n")
    if not fires:
        print("No flow triggers fired over the sample.")
        return 0

    for key in sorted(fires):
        direction = fires[key]["direction"]
        total = len(fires[key]["idx"])
        print(f"{key} [{direction}]  ({total} raw fires)")
        for h in HORIZONS:
            idx = [i for i in _collapse(fires[key]["idx"], h) if i + h < n]
            wins = sum(1 for i in idx if _adj_return(closes, i, h, direction) > 0)
            s = cell_stats(wins, len(idx), base_rate(closes, direction, h))
            flag = "LOW-N" if s["low_n"] else ("~base (no edge)" if s["not_significant"] else "edge?")
            wr = f"{s['win_rate']:.2f}" if s["win_rate"] is not None else "n/a"
            br = f"{s['base_rate']:.2f}" if s["base_rate"] is not None else "n/a"
            print(f"   h={h}bar  n={s['n']:>3}  win={wr}  base={br}  "
                  f"CI[{s['wilson_lo']:.2f},{s['wilson_hi']:.2f}]  {flag}")
        print()
    print("LOW-N = too few events to conclude. '~base' = CI straddles the base rate "
          "(indistinguishable from drift). Past behavior is not a forecast.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

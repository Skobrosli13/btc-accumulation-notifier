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
  * MULTIPLICITY: 6 keys x 3 horizons = ~18 cells at 95% per-cell confidence means
    ~60% odds of at least one spurious "edge?" under the null — and every rerun
    after a flow_* threshold tweak re-evaluates on the SAME free-tier bars the
    tweak was fitted to.

PRE-REGISTERED PROMOTION RULE (a flow trigger may graduate from FORWARD-TEST only
if ALL hold; a lone uncorrected "edge?" cell is expected noise):
  1. the Bonferroni-adjusted Wilson CI low clears the base rate,
  2. at EVERY horizon, not just one,
  3. on data not used to tune flow_* thresholds (a fresh window after the tune
     date, or a second venue).

    python -m scripts.backtest_flow
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import flow                                # noqa: E402
from app.config import load_config                  # noqa: E402
from app.sources import coinalyze                   # noqa: E402
from scripts.st_validation import (                 # noqa: E402
    ROUND_TRIP_COST, base_rate, cell_stats, wilson_interval)

TF = "4hour"
REQUEST_HOURS = 4000 * 4          # request well past the free-tier cap (~2006 bars)
HORIZONS = [1, 3, 6]              # forward bars on 4h => 4h / 12h / 24h


def _drop_forming(rows: list[dict]) -> list[dict]:
    """Mirror app.collect_once._closed: the trailing history bar can still be
    forming — a partial close must not serve as a forward-return endpoint, nor its
    partial volume enter the trailing windows."""
    return rows[:-1] if len(rows) > 1 else rows


def _bonferroni_z(cells: int, alpha: float = 0.05) -> float:
    """Two-sided z for a family-wise ``alpha`` split evenly across ``cells`` tests
    (Bonferroni). cells<=1 -> the plain 1.96."""
    if cells <= 1:
        return 1.96
    return NormalDist().inv_cdf(1 - (alpha / cells) / 2)


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
    ohlcv = _drop_forming(coinalyze.ohlcv_history(sym, TF, REQUEST_HOURS, key))
    oi = _drop_forming(coinalyze.oi_history(sym, TF, REQUEST_HOURS, key))
    liq = _drop_forming(coinalyze.liquidations_history(sym, TF, REQUEST_HOURS, key))
    if len(ohlcv) < 100:
        print(f"insufficient history ({len(ohlcv)} bars) — check symbol/plan.")
        return 1

    closes = [r["close"] for r in ohlcv]
    oi_by_ts = {r["ts"]: r["oi"] for r in oi}
    liq_by_ts = {r["ts"]: r for r in liq}
    # Live, _collect_flow fetches (lookback+5)*interval HOURS and then drops the
    # trailing (forming) bar, so the collector evaluates on lookback+4 CLOSED bars.
    # The replay must use the same count or liquidation_flush's baseline mean runs
    # over one extra bar vs production.
    window_n = cfg.flow_cvd_lookback + 4
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

    cells = len(fires) * len(HORIZONS)
    z_adj = _bonferroni_z(cells)
    print(f"{cells} cells tested ({len(fires)} keys x {len(HORIZONS)} horizons); the "
          f"per-cell 95% CIs are UNCORRECTED. The promotion check uses a "
          f"Bonferroni-adjusted z={z_adj:.2f} — and even that is in-sample (see the "
          "pre-registered rule in the module docstring).\n")

    for key in sorted(fires):
        direction = fires[key]["direction"]
        total = len(fires[key]["idx"])
        print(f"{key} [{direction}]  ({total} raw fires)")
        for h in HORIZONS:
            idx = [i for i in _collapse(fires[key]["idx"], h) if i + h < n]
            wins = sum(1 for i in idx if _adj_return(closes, i, h, direction) > 0)
            s = cell_stats(wins, len(idx), base_rate(closes, direction, h))
            if s["low_n"]:
                flag = "LOW-N"
            elif s["not_significant"]:
                flag = "~base (no edge)"
            else:
                adj_lo, _ = wilson_interval(wins, len(idx), z=z_adj)
                bonf = s["base_rate"] is not None and adj_lo > s["base_rate"]
                flag = ("edge? (in-sample, uncorrected; Bonferroni "
                        f"{'PASS' if bonf else 'FAIL'})")
            wr = f"{s['win_rate']:.2f}" if s["win_rate"] is not None else "n/a"
            br = f"{s['base_rate']:.2f}" if s["base_rate"] is not None else "n/a"
            print(f"   h={h}bar  n={s['n']:>3}  win={wr}  base={br}  "
                  f"CI[{s['wilson_lo']:.2f},{s['wilson_hi']:.2f}]  {flag}")
        print()
    print("LOW-N = too few events to conclude. '~base' = CI straddles the base rate "
          "(indistinguishable from drift). 'edge?' alone promotes NOTHING: the "
          "pre-registered rule requires the Bonferroni-adjusted CI to clear base at "
          "EVERY horizon on data the thresholds were not tuned on. Past behavior is "
          "not a forecast.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

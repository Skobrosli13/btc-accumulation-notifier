"""BTC policy signal functions (pure) — §6: btc_trend_policy, btc_accum_policy.

- :func:`trend_exposure` — the 200-DMA regime filter with a ±2% hysteresis band:
  long (1.0) above MA×(1+band), flat (0.0) below MA×(1−band), HOLD the previous
  state inside the band (that's the hysteresis — no whipsaw at the line). The
  exposure at index t uses only closes[:t+1]; the backtester applies it to the
  NEXT day's return.
- :func:`accum_scales` — the LT accumulation composite as a DCA spend-tilt:
  pre-registered tier multipliers (NEUTRAL 0.75 / WATCH 1.0 / ACCUMULATE 1.5 /
  DEEP_VALUE 2.0). Tilts NEW capital only; the trend policy manages the HELD
  stack — the owner adopts at most one per function (§6).

These two are deliberately independent signals; neither reads the other.
"""
from __future__ import annotations

TREND_PERIOD = 200
TREND_BAND = 0.02

# Pre-registered spend multipliers per LT tier (btc_accum_policy).
TIER_SCALE = {"NEUTRAL": 0.75, "WATCH": 1.0, "ACCUMULATE": 1.5, "DEEP_VALUE": 2.0}


def trend_exposure(closes: list[float], *, period: int = TREND_PERIOD,
                   band: float = TREND_BAND) -> list[float]:
    """Causal 0/1 exposure series from the 200-DMA hysteresis filter.

    Index t is decided from closes[..t] only. Before ``period`` bars of history
    the state is flat (0.0) — no MA, no position; honest cold-start."""
    n = len(closes)
    out = [0.0] * n
    if n == 0:
        return out
    run_sum = 0.0
    state = 0.0
    for t in range(n):
        run_sum += closes[t]
        if t >= period:
            run_sum -= closes[t - period]
        if t + 1 < period:
            out[t] = 0.0
            continue
        ma = run_sum / period
        if closes[t] > ma * (1.0 + band):
            state = 1.0
        elif closes[t] < ma * (1.0 - band):
            state = 0.0
        # inside the band: hold prior state (hysteresis)
        out[t] = state
    return out


def accum_scales(tiers: list[str]) -> list[float]:
    """Spend multipliers for a series of LT tier readings (one per contribution
    period). Unknown/missing tiers spend at 1.0 (plain DCA — the overlay never
    guesses)."""
    return [TIER_SCALE.get((t or "").upper(), 1.0) for t in tiers]

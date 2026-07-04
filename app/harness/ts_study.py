"""Single-asset time-series evaluator (BTC) — stationary block bootstrap (§5.3).

BTC has no cross-section, so the baseline is the asset's OWN return process:
a stationary (Politis–Romano) block bootstrap of daily returns — geometric block
lengths with mean ≈30 days, preserving short-range autocorrelation/vol
clustering — resampled ``n_resamples`` times. Each resample re-places the SAME
event pattern (identical bar indices ⇒ identical count and spacing) and records
the mean forward return; the p-value is the fraction of resampled means at least
as favorable as the observed one (≥ for LONG, ≤ for SHORT).

Honesty rules baked in:
  * events are DE-CORRELATED first (spaced ≥ one horizon apart — the
    episode-collapse rule from perf/calibrate, via ``stats.spaced_subset``);
  * the clustered t uses months as the unit, same as the equity side;
  * regime stratification (above/below the 200-day MA at the event bar);
  * optional structural-break split (pre/post 2024-01 for anything
    on-chain-derived — spot ETFs changed supply dynamics).

Pure and deterministic (seeded); the caller feeds closes/timestamps from the
lake or the candles table.
"""
from __future__ import annotations

import bisect
import random

from . import stats

_DAY_MS = 86_400_000
N_RESAMPLES = 1000
MEAN_BLOCK_DAYS = 30
REGIME_PERIOD = 200
# Structural break for on-chain-derived studies: US spot ETFs (2024-01).
SPLIT_2024_MS = 1_704_067_200_000     # 2024-01-01T00:00:00Z


def _returns(closes: list[float]) -> list[float]:
    return [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]


def _forward_from_returns(rets: list[float], bar_idx: int, h: int) -> float | None:
    """Compound forward return over ``h`` days entering at the close of
    ``bar_idx`` (i.e. over return slots bar_idx .. bar_idx+h-1). None when the
    window runs off the series end."""
    if bar_idx + h > len(rets):
        return None
    acc = 1.0
    for t in range(bar_idx, bar_idx + h):
        acc *= 1.0 + rets[t]
    return acc - 1.0


def stationary_bootstrap(rets: list[float], rnd: random.Random,
                         mean_block: int = MEAN_BLOCK_DAYS) -> list[float]:
    """One stationary-bootstrap resample of a return series (same length).

    Politis–Romano: at each step continue the current block with probability
    1 − 1/mean_block (wrapping at the series end) or jump to a fresh random
    start — block lengths are geometric with the requested mean, preserving
    local dependence structure the i.i.d. bootstrap would destroy."""
    n = len(rets)
    if n == 0:
        return []
    p_restart = 1.0 / float(mean_block)
    out: list[float] = []
    idx = rnd.randrange(n)
    for _ in range(n):
        out.append(rets[idx])
        if rnd.random() < p_restart:
            idx = rnd.randrange(n)
        else:
            idx = (idx + 1) % n
    return out


def _event_indices(ts_ms: list[int], event_ts: list[int]) -> list[int]:
    """Bar index whose close is the ENTRY for each event: the first bar at/after
    the event timestamp (a daily close is tradeable from that bar's close)."""
    out = []
    for ts in event_ts:
        i = bisect.bisect_left(ts_ms, ts)
        if i < len(ts_ms):
            out.append(i)
    return out


def _regime_at(closes: list[float], i: int, period: int = REGIME_PERIOD) -> str:
    """'bull' / 'bear' from the trailing ``period``-bar MA at bar i ('unknown'
    with insufficient history) — mirrors shortterm.current_regime."""
    if i + 1 < period:
        return "unknown"
    ma = sum(closes[i + 1 - period: i + 1]) / period
    return "bull" if closes[i] >= ma else "bear"


def _population_stats(fwd: list[float], ev_ts: list[int], signs: list[float],
                      rets: list[float], indices: list[int], h: int,
                      n_resamples: int, mean_block: int, seed: int) -> dict:
    """Observed stats + bootstrap p for one event population (pure).

    ``signs`` is per-event (+1 LONG / −1 SHORT), applied identically to the
    observed forward returns and to every resample's placements — a mixed-
    direction study is scored per each event's own claimed direction."""
    if not fwd:
        return {"n_events": 0, "observed_mean": None, "p_value": None,
                "t_clustered": None, "n_months": 0, "win_rate": None}
    signed = [s * f for s, f in zip(signs, fwd)]
    obs_mean = sum(signed) / len(signed)
    ct = stats.clustered_t(signed, ev_ts)
    rnd = random.Random(seed)
    at_least = 0
    valid = 0
    for _ in range(n_resamples):
        boot = stationary_bootstrap(rets, rnd, mean_block)
        sample = [s * f for s, f in
                  ((s, _forward_from_returns(boot, i, h))
                   for s, i in zip(signs, indices)) if f is not None]
        if not sample:
            continue
        valid += 1
        if sum(sample) / len(sample) >= obs_mean:
            at_least += 1
    return {"n_events": len(signed),
            "observed_mean": obs_mean,
            "p_value": (at_least / valid) if valid else None,
            "t_clustered": ct["t"], "n_months": ct["n_months"],
            "win_rate": sum(1 for f in signed if f > 0) / len(signed)}


def evaluate(closes: list[float], ts_ms: list[int], event_ts: list[int], *,
             h_days: int, direction: str = "LONG",
             directions: list[str] | None = None,
             n_resamples: int = N_RESAMPLES, mean_block: int = MEAN_BLOCK_DAYS,
             seed: int = 42, split_ts_ms: int | None = None,
             regime_period: int = REGIME_PERIOD) -> dict:
    """Run a BTC time-series study (pure, deterministic).

    ``closes``/``ts_ms``: the daily series ascending; ``event_ts``: raw event
    timestamps (ms). ``directions`` (aligned with ``event_ts``) scores each
    event by its OWN claimed direction — required for mixed contrarian studies
    (e.g. funding extremes fire both ways); ``direction`` is the uniform
    fallback. Returns observed vs bootstrap-baseline stats for the whole
    de-correlated population, per 200-DMA regime, and (when ``split_ts_ms``)
    pre/post the structural break.
    """
    dirs = directions if directions is not None else [direction] * len(event_ts)
    order = sorted(range(len(event_ts)), key=lambda k: event_ts[k])
    ev_sorted = [int(event_ts[k]) for k in order]
    dir_sorted = [dirs[k] for k in order]
    kept = stats.spaced_subset(ev_sorted, h_days * _DAY_MS)     # de-correlate
    ev_kept = [ev_sorted[i] for i in kept]
    sign_kept = [-1.0 if dir_sorted[i] == "SHORT" else 1.0 for i in kept]

    rets = _returns(closes)
    idxs = _event_indices(ts_ms, ev_kept)
    # entry close at bar i => forward over return slots i..i+h-1
    rows = []                                    # (event_ts, bar_idx, fwd, sign)
    for ts, i, s in zip(ev_kept, idxs, sign_kept):
        f = _forward_from_returns(rets, i, h_days)
        if f is not None:
            rows.append((ts, i, f, s))

    def pop(sub_rows: list[tuple], seed_offset: int = 0) -> dict:
        return _population_stats(
            [r[2] for r in sub_rows], [r[0] for r in sub_rows],
            [r[3] for r in sub_rows],
            rets, [r[1] for r in sub_rows], h_days,
            n_resamples, mean_block, seed + seed_offset)

    out = {"n_events_raw": len(event_ts), "n_events_decorrelated": len(ev_kept),
           "h_days": h_days, "direction": direction, "all": pop(rows)}

    regimes: dict[str, list[tuple]] = {}
    for r in rows:
        regimes.setdefault(_regime_at(closes, r[1], regime_period), []).append(r)
    out["regimes"] = {k: pop(v, seed_offset=1) for k, v in regimes.items()}

    if split_ts_ms is not None:
        out["split"] = {
            "pre": pop([r for r in rows if r[0] < split_ts_ms], seed_offset=2),
            "post": pop([r for r in rows if r[0] >= split_ts_ms], seed_offset=3),
        }
    else:
        out["split"] = None
    return out

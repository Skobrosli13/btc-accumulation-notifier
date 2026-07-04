"""ts_study — synthetic fixture with a KNOWN injected effect (M2 acceptance).

A seeded noise series gets +3%/day injected for h days after each event bar; the
evaluator must find it (p ~ 0 vs the block-bootstrap baseline) and must NOT find
an effect when nothing is injected. Deterministic throughout.
"""
from __future__ import annotations

import random

import pytest

from app.harness import ts_study

DAY = 86_400_000
N_DAYS = 800
EVENT_DAYS = [100, 200, 300, 400, 500]
H = 10
RESAMPLES = 300     # plenty of p-resolution for the assertions, keeps tests fast


def _series(inject: float = 0.0, seed: int = 0):
    """Seeded gauss(0, 1%) daily returns; ``inject`` is ADDED to the h return
    slots after each event bar. Returns (closes, ts_ms)."""
    rnd = random.Random(seed)
    rets = [rnd.gauss(0.0, 0.01) for _ in range(N_DAYS - 1)]
    for d in EVENT_DAYS:
        for t in range(d, d + H):
            rets[t] += inject
    closes, ts = [100.0], [0]
    for k, r in enumerate(rets):
        closes.append(closes[-1] * (1.0 + r))
        ts.append((k + 1) * DAY)
    return closes, ts


def _ev_ts():
    return [d * DAY for d in EVENT_DAYS]


def test_injected_long_effect_is_detected():
    closes, ts = _series(inject=0.03)
    out = ts_study.evaluate(closes, ts, _ev_ts(), h_days=H,
                            n_resamples=RESAMPLES, seed=42)
    a = out["all"]
    assert out["n_events_decorrelated"] == 5          # 100-day spacing >> h
    assert a["n_events"] == 5
    assert a["observed_mean"] > 0.25                  # ~ (1.03)^10 - 1 plus noise
    assert a["p_value"] == 0.0                        # no resample comes close
    assert a["win_rate"] == 1.0
    assert a["t_clustered"] is not None and a["t_clustered"] > 3


def test_null_series_shows_no_effect():
    closes, ts = _series(inject=0.0)
    out = ts_study.evaluate(closes, ts, _ev_ts(), h_days=H,
                            n_resamples=RESAMPLES, seed=42)
    p = out["all"]["p_value"]
    assert p is not None and 0.05 < p < 0.95          # comfortably interior


def test_injected_short_effect_via_direction():
    closes, ts = _series(inject=-0.03)
    out = ts_study.evaluate(closes, ts, _ev_ts(), h_days=H, direction="SHORT",
                            n_resamples=RESAMPLES, seed=42)
    a = out["all"]
    assert a["observed_mean"] > 0.2                   # signed: a fall scores positive
    assert a["p_value"] == 0.0
    # and the SAME data evaluated LONG is a disaster, not an edge
    out2 = ts_study.evaluate(closes, ts, _ev_ts(), h_days=H, direction="LONG",
                             n_resamples=RESAMPLES, seed=42)
    assert out2["all"]["observed_mean"] < -0.2
    assert out2["all"]["p_value"] > 0.95


def test_mixed_directions_scored_per_event():
    """A contrarian study fires BOTH ways: +3%/day injected after LONG events,
    -3%/day after SHORT events. Per-event signing must score all of them as
    wins; a uniform LONG read would cancel to ~zero."""
    rnd = random.Random(0)
    rets = [rnd.gauss(0.0, 0.01) for _ in range(N_DAYS - 1)]
    longs, shorts = [100, 300, 500], [200, 400]
    for d in longs:
        for t in range(d, d + H):
            rets[t] += 0.03
    for d in shorts:
        for t in range(d, d + H):
            rets[t] -= 0.03
    closes, ts = [100.0], [0]
    for k, r in enumerate(rets):
        closes.append(closes[-1] * (1.0 + r))
        ts.append((k + 1) * DAY)
    ev = [d * DAY for d in sorted(longs + shorts)]
    dirs = ["LONG" if d // DAY in longs else "SHORT" for d in ev]
    out = ts_study.evaluate(closes, ts, ev, h_days=H, directions=dirs,
                            n_resamples=RESAMPLES, seed=42)
    a = out["all"]
    assert a["n_events"] == 5 and a["win_rate"] == 1.0
    assert a["observed_mean"] > 0.2 and a["p_value"] == 0.0
    # the uniform-LONG misread cancels the two legs to roughly nothing
    flat = ts_study.evaluate(closes, ts, ev, h_days=H, direction="LONG",
                             n_resamples=RESAMPLES, seed=42)["all"]
    assert abs(flat["observed_mean"]) < a["observed_mean"] / 2


def test_overlapping_events_are_decorrelated():
    closes, ts = _series(inject=0.03)
    clustered = [100 * DAY, 101 * DAY, 102 * DAY, 200 * DAY]   # 3 within one horizon
    out = ts_study.evaluate(closes, ts, clustered, h_days=H,
                            n_resamples=10, seed=1)
    assert out["n_events_raw"] == 4
    assert out["n_events_decorrelated"] == 2          # 100 + 200 survive


def test_regime_stratification_tags_bull_and_bear():
    # 400 days rising then 400 falling: an event late in each leg is cleanly
    # above/below its trailing 200-DMA.
    rets = [0.005] * 400 + [-0.005] * 399
    closes, ts = [100.0], [0]
    for k, r in enumerate(rets):
        closes.append(closes[-1] * (1 + r))
        ts.append((k + 1) * DAY)
    out = ts_study.evaluate(closes, ts, [350 * DAY, 700 * DAY], h_days=5,
                            n_resamples=10, seed=1)
    assert set(out["regimes"]) == {"bull", "bear"}
    assert out["regimes"]["bull"]["n_events"] == 1
    assert out["regimes"]["bear"]["n_events"] == 1


def test_structural_break_split_partitions_events():
    closes, ts = _series(inject=0.03)
    out = ts_study.evaluate(closes, ts, _ev_ts(), h_days=H,
                            n_resamples=10, seed=1,
                            split_ts_ms=250 * DAY)
    assert out["split"]["pre"]["n_events"] == 2       # days 100, 200
    assert out["split"]["post"]["n_events"] == 3      # days 300, 400, 500


def test_stationary_bootstrap_preserves_length_and_values():
    rnd = random.Random(0)
    rets = [0.01 * i for i in range(50)]
    boot = ts_study.stationary_bootstrap(rets, rnd, mean_block=5)
    assert len(boot) == 50
    assert set(boot) <= set(rets)                     # only real observations
    assert ts_study.stationary_bootstrap([], rnd) == []

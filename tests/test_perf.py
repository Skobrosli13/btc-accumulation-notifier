"""Live forward-testing math (out-of-sample signal performance)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import perf

_DAY = 86_400_000
_BASE = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def _ms(day):
    return _BASE + day * _DAY


def _iso(day):
    return datetime.fromtimestamp(_ms(day) / 1000, tz=timezone.utc).isoformat()


def _candles(lo, hi):
    return [{"ts": _ms(d), "close": 100.0 + d} for d in range(lo, hi)]


# --- long-term ---------------------------------------------------------------

def test_long_term_forward_returns_and_window():
    candles = _candles(1, 41)  # rising 101 -> 140
    runs = [
        {"run_ts": _iso(2), "price": 102.0, "tier": "ACCUMULATE"},  # +30d -> day32 (132) up
        {"run_ts": _iso(3), "price": 103.0, "tier": "NEUTRAL"},     # base only
        {"run_ts": _iso(38), "price": 138.0, "tier": "ACCUMULATE"}, # +30d beyond data -> excluded
    ]
    out = perf.long_term_performance(runs, candles, horizons_days=(30,))
    d30 = out["horizons"]["30"]
    assert d30["n_signal"] == 1 and d30["n_runs"] == 2   # day38 run excluded (not old enough)
    assert d30["signal_hit_rate"] == 1.0                  # the one signal was positive
    assert d30["base_rate"] == 1.0
    assert out["window"] == {"from": _iso(2), "to": _iso(38)}


def test_long_term_episode_collapse():
    # 5 consecutive ACCUMULATE runs = ONE episode (one market outcome, not five
    # samples); a NEUTRAL break then a new stretch = a second episode.
    candles = _candles(1, 60)
    runs = ([{"run_ts": _iso(2 + i), "price": 100.0, "tier": "ACCUMULATE"} for i in range(5)]
            + [{"run_ts": _iso(8), "price": 100.0, "tier": "NEUTRAL"}]
            + [{"run_ts": _iso(10), "price": 100.0, "tier": "ACCUMULATE"},
               {"run_ts": _iso(11), "price": 100.0, "tier": "ACCUMULATE"}])
    d30 = perf.long_term_performance(runs, candles, horizons_days=(30,))["horizons"]["30"]
    assert d30["n_signal"] == 7           # run-level count still served
    assert d30["episodes"] == 2           # the honest n
    assert d30["episode_hit_rate"] == 1.0
    assert d30["ci"] is None              # <3 episodes -> no CI theater


def test_long_term_tier_change_starts_new_episode():
    candles = _candles(1, 60)
    runs = [{"run_ts": _iso(2), "price": 100.0, "tier": "ACCUMULATE"},
            {"run_ts": _iso(3), "price": 100.0, "tier": "DEEP_VALUE"},
            {"run_ts": _iso(4), "price": 100.0, "tier": "DEEP_VALUE"}]
    d30 = perf.long_term_performance(runs, candles, horizons_days=(30,))["horizons"]["30"]
    assert d30["episodes"] == 2           # same-tier collapse, not any-signal collapse


def test_long_term_ci_present_with_enough_episodes():
    candles = _candles(1, 80)
    runs = []
    for start in (2, 6, 10, 14):          # 4 separated one-run episodes, all winners
        runs.append({"run_ts": _iso(start), "price": 100.0, "tier": "ACCUMULATE"})
        runs.append({"run_ts": _iso(start + 1), "price": 100.0, "tier": "NEUTRAL"})
    d30 = perf.long_term_performance(runs, candles, horizons_days=(30,))["horizons"]["30"]
    assert d30["episodes"] == 4
    assert d30["ci"] == [1.0, 1.0]        # all-win outcomes -> degenerate CI at 1.0


def test_long_term_gap_rejects_stale_pricing():
    # Candles day 1..10 then a gap until day 60: a 30d run at day 2 would otherwise
    # be "priced" by the day-10 close (22 days stale) -> must be skipped instead.
    candles = _candles(1, 11) + _candles(60, 62)
    runs = [{"run_ts": _iso(2), "price": 102.0, "tier": "ACCUMULATE"}]
    d30 = perf.long_term_performance(runs, candles, horizons_days=(30,))["horizons"]["30"]
    assert d30["n_runs"] == 0 and d30["episodes"] == 0


# --- short-term ----------------------------------------------------------------

def test_short_term_dedupes_costs_and_splits():
    candles = _candles(1, 20)  # rising
    alerts = [
        # two rows for the SAME (direction, candle) — one confluence-gated event
        {"ts": _ms(2), "direction": "BUY", "price": 102.0, "trigger_key": "ema_cross_bull"},
        {"ts": _ms(2), "direction": "BUY", "price": 102.0, "trigger_key": "macd_bull_cross"},
        {"ts": _ms(2), "direction": "SELL", "price": 102.0, "trigger_key": "bb_upper_reject"},
        {"ts": _ms(18), "direction": "BUY", "price": 118.0, "trigger_key": "ema_cross_bull"},  # immature
    ]
    out = perf.short_term_performance(alerts, candles, horizon_days=7)
    assert out["n_events"] == 2                        # BUY deduped; SELL separate
    assert out["win_rate"] == 0.5
    assert out["by_direction"]["BUY"] == {"n": 1, "win_rate": 1.0}
    assert out["by_direction"]["SELL"] == {"n": 1, "win_rate": 0.0}
    # per-key scoreboard still counts each key's own alert
    assert out["by_key"]["ema_cross_bull"]["n"] == 1
    assert out["by_key"]["macd_bull_cross"]["n"] == 1
    # unconditional BUY-side base rate over the same candles (all up here)
    assert out["base_rate"] == 1.0
    assert out["horizon_days"] == 7


def test_short_term_cost_flips_marginal_win():
    # +0.05% move over 7d: a gross win, but net of the 0.1% round trip -> loss.
    candles = [{"ts": _ms(d), "close": (100.0 if d < 9 else 100.05)} for d in range(1, 20)]
    alerts = [{"ts": _ms(2), "direction": "BUY", "price": 100.0, "trigger_key": "k"}]
    out = perf.short_term_performance(alerts, candles, horizon_days=7)
    assert out["n_events"] == 1 and out["win_rate"] == 0.0
    assert out["base_rate"] == 0.0        # flat market never beats the cost either


def test_short_term_created_at_anchors_window():
    # Alert stamped on the trigger candle's OPEN (day 2) but actionable a day
    # later: with created_at at day 3 the 7d window ends day 10 (> last candle,
    # immature); anchored at the raw ts it would already "mature" at day 9.
    candles = [{"ts": _ms(d), "close": 100.0} for d in range(1, 10)]
    a = {"ts": _ms(2), "direction": "BUY", "price": 100.0,
         "created_at": datetime.fromtimestamp(_ms(3) / 1000, tz=timezone.utc).isoformat()}
    out = perf.short_term_performance([a], candles, horizon_days=7)
    assert out["n_events"] == 0
    out2 = perf.short_term_performance(
        [{k: v for k, v in a.items() if k != "created_at"}], candles, horizon_days=7)
    assert out2["n_events"] == 1


def test_short_term_gap_rejects_stale_pricing():
    # A candle gap across the target: the nearest close is 8 days old -> skip.
    candles = [{"ts": _ms(d), "close": 100.0} for d in (1, 2, 3, 20)]
    alerts = [{"ts": _ms(2), "direction": "BUY", "price": 100.0, "trigger_key": "k"}]
    out = perf.short_term_performance(alerts, candles, horizon_days=7)
    assert out["n_events"] == 0


def test_short_term_flow_keys_get_scoreboard_rows():
    candles = _candles(1, 20)
    alerts = [
        {"ts": _ms(2), "direction": "BUY", "price": 102.0, "trigger_key": "liq_long_flush"},
        {"ts": _ms(2), "direction": "BUY", "price": 102.0, "trigger_key": "cvd_bull_divergence"},
    ]
    out = perf.short_term_performance(alerts, candles, horizon_days=7)
    assert out["n_events"] == 1                        # one market event
    assert set(out["by_key"]) == {"cvd_bull_divergence", "liq_long_flush"}
    assert out["by_key"]["liq_long_flush"] == {"n": 1, "win_rate": 1.0}


def test_empty_inputs_safe():
    lt = perf.long_term_performance([], [], (30,))
    assert lt["horizons"]["30"]["signal_hit_rate"] is None
    assert lt["horizons"]["30"]["episodes"] == 0 and lt["horizons"]["30"]["ci"] is None
    assert lt["window"] == {"from": None, "to": None}
    st = perf.short_term_performance([], [])
    assert st["win_rate"] is None and st["n_events"] == 0 and st["base_rate"] is None
    assert st["by_direction"]["BUY"] == {"n": 0, "win_rate": None}
    assert st["by_key"] == {}

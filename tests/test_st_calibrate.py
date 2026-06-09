"""Pure-logic tests for the short-term validation layer (scripts/st_validation.py).

All deterministic, over SYNTHETIC frames — no network. Covers:
  * Wilson interval math (known values + clamping).
  * base_rate / cost deduction (the round-trip is actually subtracted).
  * race_R: stop/target ordering, the same-candle stop-before-target pessimism,
    and mark-to-market of trades that resolve neither way within the horizon.
  * cell_stats significance flags.
  * The alerted-population replay applies cooldown + same-candle dedup, and the
    regime/confluence gates, exactly as app/collect_once does.
"""
from __future__ import annotations

import pandas as pd
import pytest

from scripts import st_validation as stv
from tests.factories import make_config


# --- Wilson interval ---------------------------------------------------------

def test_wilson_no_information_when_n_zero():
    lo, hi = stv.wilson_interval(0, 0)
    assert (lo, hi) == (0.0, 1.0)


def test_wilson_known_value():
    # Known textbook value: 1 success in 1 trial, z=1.96 -> Wilson lower ~0.207.
    lo, hi = stv.wilson_interval(1, 1)
    assert lo == pytest.approx(0.2068, abs=1e-3)
    assert hi == pytest.approx(1.0, abs=1e-9)


def test_wilson_centered_and_within_unit_interval():
    lo, hi = stv.wilson_interval(50, 100)
    assert 0.0 <= lo < 0.5 < hi <= 1.0
    # symmetric-ish around 0.5 at p=0.5
    assert (lo + hi) / 2 == pytest.approx(0.5, abs=1e-6)


def test_wilson_clamps_to_unit_interval():
    lo, hi = stv.wilson_interval(0, 5)
    assert lo == 0.0  # never negative
    lo2, hi2 = stv.wilson_interval(5, 5)
    assert hi2 == 1.0  # never above 1


# --- base_rate + cost --------------------------------------------------------

def test_base_rate_subtracts_round_trip_cost():
    # A flat series moves 0 -> minus the cost is negative -> 0% base rate for BOTH
    # directions (the cost turns a no-move into a loss). This is the whole point of
    # deducting the round-trip: a non-mover is not a winner.
    flat = [100.0] * 20
    assert stv.base_rate(flat, "BUY", 1) == pytest.approx(0.0)
    assert stv.base_rate(flat, "SELL", 1) == pytest.approx(0.0)

    # A move smaller than the cost is still a loss; a move larger than the cost wins.
    half_cost = stv.ROUND_TRIP_COST / 2.0
    barely_up = [100.0 * (1 + half_cost) ** i for i in range(20)]
    assert stv.base_rate(barely_up, "BUY", 1) == pytest.approx(0.0)  # < cost -> loss

    # A clearly rising series beats the cost -> 100% for BUY, 0% for SELL.
    rising = [100.0 + 5.0 * i for i in range(20)]
    assert stv.base_rate(rising, "BUY", 1) == pytest.approx(1.0)
    assert stv.base_rate(rising, "SELL", 1) == pytest.approx(0.0)


def test_base_rate_none_when_too_short():
    assert stv.base_rate([100.0, 101.0], "BUY", 5) is None


# --- race_R: ordering, pessimism, mark-to-market -----------------------------

def test_race_target_hit_returns_rr_minus_cost():
    # BUY entry 100, stop 90 (risk 10), target 120 (rr=2). Next bar tags target.
    lv = {"stop": 90.0, "target": 120.0, "rr": 2.0, "atr": 6.67}
    closes = [100.0, 110.0, 130.0]
    highs = [100.0, 121.0, 131.0]   # bar 1 high reaches target
    lows = [100.0, 105.0, 120.0]    # never hits stop
    r = stv.race_R("BUY", lv, highs, lows, closes, 0, fwd=2)
    cost_R = stv.ROUND_TRIP_COST * 100.0 / 10.0
    assert r == pytest.approx(2.0 - cost_R)


def test_race_stop_hit_returns_minus_one_minus_cost():
    lv = {"stop": 90.0, "target": 120.0, "rr": 2.0, "atr": 6.67}
    closes = [100.0, 95.0, 92.0]
    highs = [100.0, 96.0, 93.0]
    lows = [100.0, 89.0, 91.0]      # bar 1 low breaches stop
    r = stv.race_R("BUY", lv, highs, lows, closes, 0, fwd=2)
    cost_R = stv.ROUND_TRIP_COST * 100.0 / 10.0
    assert r == pytest.approx(-1.0 - cost_R)


def test_race_same_candle_resolves_against_us():
    # A single bar whose range spans BOTH stop and target -> pessimism: STOP first.
    lv = {"stop": 90.0, "target": 120.0, "rr": 2.0, "atr": 6.67}
    closes = [100.0, 105.0]
    highs = [100.0, 125.0]   # touches target
    lows = [100.0, 85.0]     # AND touches stop, same bar
    r = stv.race_R("BUY", lv, highs, lows, closes, 0, fwd=1)
    cost_R = stv.ROUND_TRIP_COST * 100.0 / 10.0
    assert r == pytest.approx(-1.0 - cost_R)  # stop, not target


def test_race_unresolved_is_marked_to_market_not_dropped():
    # Neither stop nor target hit within the horizon -> mark to market at horizon
    # close. The OLD code returned None (dropped this trade). It must NOT be None.
    lv = {"stop": 90.0, "target": 120.0, "rr": 2.0, "atr": 6.67}
    closes = [100.0, 102.0, 105.0]   # drifts up to 105 (risk 10) -> +0.5R before cost
    highs = [100.0, 103.0, 106.0]    # never reaches 120
    lows = [100.0, 101.0, 104.0]     # never reaches 90
    r = stv.race_R("BUY", lv, highs, lows, closes, 0, fwd=2)
    cost_R = stv.ROUND_TRIP_COST * 100.0 / 10.0
    assert r is not None
    assert r == pytest.approx(0.5 - cost_R)


def test_race_mark_to_market_sell_direction():
    # SELL entry 100, stop 110 (risk 10), target 80. Price ends at 95 -> +0.5R.
    lv = {"stop": 110.0, "target": 80.0, "rr": 2.0, "atr": 6.67}
    closes = [100.0, 98.0, 95.0]
    highs = [100.0, 99.0, 96.0]      # never hits 110 stop
    lows = [100.0, 97.0, 94.0]       # never hits 80 target
    r = stv.race_R("SELL", lv, highs, lows, closes, 0, fwd=2)
    cost_R = stv.ROUND_TRIP_COST * 100.0 / 10.0
    assert r == pytest.approx(0.5 - cost_R)


# --- cell_stats significance flags -------------------------------------------

def test_cell_stats_low_n_flag():
    cell = stv.cell_stats(wins=5, n=10, base=0.5)
    assert cell["low_n"] is True
    big = stv.cell_stats(wins=30, n=60, base=0.5)
    assert big["low_n"] is False


def test_cell_stats_not_significant_when_ci_straddles_base():
    # 50/100 around base 0.5 -> CI straddles base -> not significant.
    cell = stv.cell_stats(wins=50, n=100, base=0.5)
    assert cell["not_significant"] is True
    # A strong edge over a low base -> CI clears the base -> significant.
    cell2 = stv.cell_stats(wins=95, n=100, base=0.5)
    assert cell2["not_significant"] is False


# --- alerted-population replay ----------------------------------------------

def _candles(closes, tf="4h", volumes=None, start="2026-01-01"):
    n = len(closes)
    volumes = volumes if volumes is not None else [1.0] * n
    freq = {"4h": "4h", "1d": "1D"}[tf]
    times = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({
        "open_time": times,
        "open": [float(c) for c in closes],
        "high": [c * 1.001 for c in closes],
        "low": [c * 0.999 for c in closes],
        "close": [float(c) for c in closes],
        "volume": [float(v) for v in volumes],
        "confirmed": [True] * n,
    })


def test_cooldown_ok_helper_matches_decide_st_alert():
    # First fire always OK.
    assert stv._cooldown_ok(None, candle_ts=1000, now_ms=2000, cooldown_hours=12) is True
    # Same candle -> suppressed.
    cur = stv._Cursor(last_ts=1000, last_created_ms=1000)
    assert stv._cooldown_ok(cur, candle_ts=1000, now_ms=9_999_999, cooldown_hours=12) is False
    # Different candle but within cooldown -> suppressed.
    cur2 = stv._Cursor(last_ts=1000, last_created_ms=0)
    within = int(11 * 3_600_000)   # 11h later < 12h cooldown
    assert stv._cooldown_ok(cur2, candle_ts=2000, now_ms=within, cooldown_hours=12) is False
    # Different candle, past cooldown -> fires.
    past = int(13 * 3_600_000)
    assert stv._cooldown_ok(cur2, candle_ts=2000, now_ms=past, cooldown_hours=12) is True


def test_replay_two_fires_same_candle_count_once():
    # Build a frame where a single closed candle produces a trigger, then craft a
    # second identical detection by repeating the trigger key on the same index.
    # Easier: assert the replay never emits two ALERTED events with the same
    # (key, candle_ts) — same-candle dedup. We force repeated fires across many
    # adjacent candles and check cooldown collapses them.
    cfg = make_config(st_require_confluence=False, st_regime_suppress=False,
                      st_cooldown_hours=48)
    # Sawtooth that crosses EMAs repeatedly -> many raw fires close together.
    closes = [100.0] * 40
    for i in range(40):
        closes.append(100.0 + (3.0 if i % 2 == 0 else -3.0))
    df = _candles(closes, tf="4h")
    res = stv.replay_alerts(df, cfg, "4h", regime_series=None, maxh=0)
    # No two ALERTED events share the same (key, candle_ts) — same-candle dedup.
    seen = set()
    for e in res.alerted:
        assert (e.key, e.candle_ts) not in seen
        seen.add((e.key, e.candle_ts))
    # And there must be MORE raw fires than alerted (cooldown filtered some).
    assert len(res.raw) > len(res.alerted)


def test_replay_cooldown_excludes_within_window():
    cfg = make_config(st_require_confluence=False, st_regime_suppress=False,
                      st_cooldown_hours=1000)  # huge cooldown -> at most 1 alert/key
    closes = [100.0] * 40
    for i in range(40):
        closes.append(100.0 + (3.0 if i % 2 == 0 else -3.0))
    df = _candles(closes, tf="4h")
    res = stv.replay_alerts(df, cfg, "4h", regime_series=None, maxh=0)
    # With a near-infinite cooldown, each trigger key alerts at most once.
    per_key: dict[str, int] = {}
    for e in res.alerted:
        per_key[e.key] = per_key.get(e.key, 0) + 1
    assert per_key, "expected at least one alerted event"
    assert all(v == 1 for v in per_key.values())
    # Raw saw the repeats.
    raw_per_key: dict[str, int] = {}
    for e in res.raw:
        raw_per_key[e.key] = raw_per_key.get(e.key, 0) + 1
    assert any(v > 1 for v in raw_per_key.values())


def test_replay_confluence_gate_filters_lone_unaligned_triggers():
    # With confluence required and unknown regime, a LONE trigger (count==1,
    # regime_aligned None) can never pass confluence_ok -> zero alerted, raw>0.
    # A steady uptrend with a final up-volume spike fires ONLY vol_flush_up (SELL)
    # on that last candle — a single, unaligned trigger.
    cfg = make_config(st_require_confluence=True, st_regime_suppress=False)
    up = [100.0 + i * 0.5 for i in range(41)]
    df = _candles(up, tf="4h", volumes=[1.0] * 40 + [5.0])
    res = stv.replay_alerts(df, cfg, "4h", regime_series=None, maxh=0)
    last = [e for e in res.raw if e.index == 40]
    assert len(last) == 1 and last[0].key == "vol_flush_up"  # exactly one lone fire
    assert all(e.index != 40 for e in res.alerted)  # the lone fire didn't alert


def test_replay_regime_suppression_drops_counter_regime():
    # Bear regime + ST_REGIME_SUPPRESS -> a BUY (counter-regime) is suppressed.
    cfg = make_config(st_require_confluence=False, st_regime_suppress=True)
    # daily regime series: clearly bear (last far below its 200DMA).
    daily = pd.Series([200.0] * 200 + [50.0] * 60,
                      index=pd.date_range("2025-01-01", periods=260, freq="1D", tz="UTC"))
    closes = [100.0] * 40 + [101.0]   # an EMA bull cross (BUY) on the last candle
    df = _candles(closes, tf="4h", start="2026-06-01")
    res = stv.replay_alerts(df, cfg, "4h", regime_series=daily, maxh=0)
    buys_alerted = [e for e in res.alerted if e.direction == "BUY"]
    assert any(e.direction == "BUY" for e in res.raw)  # raw had the BUY
    assert buys_alerted == []  # suppressed against the bear regime


def test_replay_uses_fixed_live_window_not_expanding():
    # Sanity: the replay slices a trailing LIVE_WINDOW, never the whole frame.
    # Construct a frame far longer than LIVE_WINDOW and confirm replay completes
    # and the window length never exceeds LIVE_WINDOW (proxy: result is bounded,
    # and indices are valid). We assert no event index is below MIN_LOOKBACK.
    cfg = make_config(st_require_confluence=False, st_regime_suppress=False)
    n = stv.LIVE_WINDOW + 100
    closes = [100.0 + (i % 5) for i in range(n)]
    df = _candles(closes, tf="4h")
    res = stv.replay_alerts(df, cfg, "4h", regime_series=None, maxh=0)
    assert all(e.index >= stv.MIN_LOOKBACK for e in res.raw)
    assert all(e.index < n for e in res.raw)


def test_collapse_episodes_merges_adjacent_same_key():
    from scripts.st_validation import AlertEvent
    events = [
        AlertEvent("ema_cross_bull", "BUY", "4h", 10, 1000),
        AlertEvent("ema_cross_bull", "BUY", "4h", 12, 2000),   # within gap -> merged
        AlertEvent("ema_cross_bull", "BUY", "4h", 40, 3000),   # far -> separate
        AlertEvent("macd_bull_cross", "BUY", "4h", 11, 1500),  # different key -> kept
    ]
    kept = stv.collapse_episodes(events, gap_bars=6)
    keys_idx = sorted((e.key, e.index) for e in kept)
    assert ("ema_cross_bull", 10) in keys_idx
    assert ("ema_cross_bull", 12) not in keys_idx   # merged into the 10
    assert ("ema_cross_bull", 40) in keys_idx       # separate episode
    assert ("macd_bull_cross", 11) in keys_idx

"""Live forward-testing math (out-of-sample signal performance)."""
from __future__ import annotations

from datetime import datetime, timezone

from app import perf

_DAY = 86_400_000
_BASE = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def _ms(day):
    return _BASE + day * _DAY


def _iso(day):
    return datetime.fromtimestamp(_ms(day) / 1000, tz=timezone.utc).isoformat()


def test_long_term_forward_returns():
    # daily candles day 1..40, price climbing 100 -> 139
    candles = [{"ts": _ms(d), "close": 100.0 + d} for d in range(1, 41)]
    runs = [
        {"run_ts": _iso(2), "price": 102.0, "tier": "ACCUMULATE"},  # +30d -> day32 (132) up
        {"run_ts": _iso(3), "price": 103.0, "tier": "NEUTRAL"},     # base only
        {"run_ts": _iso(38), "price": 138.0, "tier": "ACCUMULATE"}, # +30d beyond data -> excluded
    ]
    out = perf.long_term_performance(runs, candles, horizons_days=(30,))
    d30 = out["30d"]
    assert d30["n_signal"] == 1 and d30["n_total"] == 2   # day38 run excluded (not old enough)
    assert d30["signal_hit_rate"] == 1.0                   # the one signal was positive
    assert d30["base_rate"] == 1.0


def test_short_term_alert_outcomes():
    candles = [{"ts": _ms(d), "close": 100.0 + d} for d in range(1, 20)]  # rising
    alerts = [
        {"ts": _ms(2), "direction": "BUY", "price": 102.0},    # +7d up -> win
        {"ts": _ms(2), "direction": "SELL", "price": 102.0},   # +7d up -> loss
        {"ts": _ms(18), "direction": "BUY", "price": 118.0},   # beyond data -> excluded
    ]
    out = perf.short_term_performance(alerts, candles, horizon_days=7)
    assert out["n"] == 2 and out["win_rate"] == 0.5


def test_empty_inputs_safe():
    assert perf.long_term_performance([], [], (30,))["30d"]["signal_hit_rate"] is None
    assert perf.short_term_performance([], [])["win_rate"] is None

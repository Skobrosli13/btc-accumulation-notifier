"""Short-term alert cooldown / same-candle suppression tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import alerting


NOW = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)


def _alert(ts_ms, created):
    return {"ts": ts_ms, "created_at": created.isoformat()}


def test_first_ever_fires():
    assert alerting.decide_st_alert(candle_ts=1000, last_alert=None,
                                    now=NOW, cooldown_hours=12) is True


def test_same_candle_suppressed():
    last = _alert(1000, NOW - timedelta(hours=48))  # old enough, but SAME candle
    assert alerting.decide_st_alert(candle_ts=1000, last_alert=last,
                                    now=NOW, cooldown_hours=12) is False


def test_within_cooldown_suppressed():
    last = _alert(1000, NOW - timedelta(hours=3))   # different candle, but recent
    assert alerting.decide_st_alert(candle_ts=2000, last_alert=last,
                                    now=NOW, cooldown_hours=12) is False


def test_after_cooldown_and_new_candle_fires():
    last = _alert(1000, NOW - timedelta(hours=13))  # different candle, past cooldown
    assert alerting.decide_st_alert(candle_ts=2000, last_alert=last,
                                    now=NOW, cooldown_hours=12) is True


def test_message_builder_shape():
    from app.shortterm import Trigger
    title, body = alerting.build_st_message(
        trigger=Trigger("ema_cross_bull", "BUY", "EMA 9/21 bullish cross"),
        timeframe="4h", score=37.0, state="BUY", price=63000.0,
        indicators={"rsi": (55.0, 50.0), "atr_pct": 2.5})
    assert "BUY" in title and "4h" in title
    assert "63,000" in body and "+37" in body

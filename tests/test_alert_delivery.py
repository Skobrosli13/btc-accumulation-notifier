"""Alert-delivery robustness + new decisioning logic.

Covers the fixes that stop alerts being silently lost on a failed send, the
zone-exit note, renormalization-aware caveats, the DEEP_VALUE gate-driven exit,
venue-contiguous candle reads, the bounded OI baseline, and batched ST emails.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import alerting, scoring, store
from app.shortterm import Trigger


NOW = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def conn(tmp_path):
    c = store.connect(str(tmp_path / "t.db"))
    store.init_db(c)
    yield c
    c.close()


def _ms(day):
    return int(datetime(2026, 1, day, tzinfo=timezone.utc).timestamp() * 1000)


# --- notified-tier cursor (failed-send retry) --------------------------------

def test_notified_tier_advances_and_is_read():
    # The alert cursor is distinct from the display tier.
    pass  # store-level behavior covered below


def test_failed_tier_send_is_retried_next_run(conn):
    """A run that DECIDES to alert but fails to send must keep notified_tier at the
    previous value so the next run re-fires (vs. advancing the display tier and
    swallowing the alert forever)."""
    # Run 1: WATCH detected from NEUTRAL, but the send "fails" -> record display
    # tier WATCH yet notified_tier stays NEUTRAL.
    store.record_run(conn, run_ts="2026-06-08T00:00:00+00:00", price=1, composite=42.0,
                     tier="WATCH", active_cats=["price"], readings={}, tier_alerted=False,
                     flash_alerted=False, notified_tier="NEUTRAL")
    assert store.last_tier(conn) == "WATCH"            # display advanced
    assert store.last_notified_tier(conn) == "NEUTRAL"  # cursor did NOT

    # Next run re-decides against the cursor -> still a tier alert.
    d = alerting.decide_alerts("WATCH", store.last_notified_tier(conn),
                               False, None, 3, NOW)
    assert d["tier_alert"] is True


def test_successful_send_advances_cursor(conn):
    store.record_run(conn, run_ts="2026-06-08T06:00:00+00:00", price=1, composite=42.0,
                     tier="WATCH", active_cats=["price"], readings={}, tier_alerted=True,
                     flash_alerted=False, notified_tier="WATCH")
    assert store.last_notified_tier(conn) == "WATCH"
    d = alerting.decide_alerts("WATCH", store.last_notified_tier(conn),
                               False, None, 3, NOW)
    assert d["tier_alert"] is False  # no repeat once communicated


def test_genuine_reentry_still_alerts():
    # NEUTRAL -> WATCH (notified WATCH) -> NEUTRAL (notified NEUTRAL) -> WATCH again.
    d = alerting.decide_alerts("WATCH", "NEUTRAL", False, None, 3, NOW)
    assert d["tier_alert"] is True


# --- zone-exit + cats-changed ------------------------------------------------

def test_exit_alert_on_drop_to_neutral():
    d = alerting.decide_alerts("NEUTRAL", "ACCUMULATE", False, None, 3, NOW)
    assert d["exit_alert"] is True and d["tier_alert"] is False


def test_no_exit_alert_neutral_to_neutral():
    d = alerting.decide_alerts("NEUTRAL", "NEUTRAL", False, None, 3, NOW)
    assert d["exit_alert"] is False and d["tier_alert"] is False


def test_cats_changed_flag_set_when_category_set_differs():
    d = alerting.decide_alerts("WATCH", "NEUTRAL", False, None, 3, NOW,
                               prev_active_cats=["onchain", "price"],
                               active_cats=["price"])
    assert d["tier_alert"] is True and d["cats_changed"] is True


def test_cats_changed_flag_clear_when_same():
    d = alerting.decide_alerts("WATCH", "NEUTRAL", False, None, 3, NOW,
                               prev_active_cats=["price", "onchain"],
                               active_cats=["onchain", "price"])  # order-insensitive
    assert d["cats_changed"] is False


def test_exit_message_builds():
    title, body = alerting.build_exit_message(
        composite=35.0, tier="NEUTRAL", subscores={}, price_struct={"price": 50000.0},
        readings={}, active_cats=["price"], onchain_active=False, prev_tier="ACCUMULATE")
    assert "zone exited" in title.lower()
    assert "CLOSED" in body and "ACCUMULATE" in body


def test_tier_message_includes_cats_caveat_when_flagged():
    _, body = alerting.build_tier_message(
        composite=61.0, tier="ACCUMULATE", subscores={}, price_struct={"price": 50000.0},
        readings={}, active_cats=["price"], onchain_active=False, cats_changed=True)
    assert "categories changed" in body.lower()


# --- DEEP_VALUE gate-driven exit (hysteresis) --------------------------------

def test_deep_value_exits_when_price_clears_band_despite_high_composite():
    # prev DEEP_VALUE, composite still >= t_deep, but price is 5% above the 200WMA:
    # the gate-driven exit must fire (the composite dead-band must not trap it).
    out = scoring.tier_hysteresis(score=85, price=210.0, wma200=200.0,
                                  prev_tier="DEEP_VALUE", t_watch=40, t_acc=60,
                                  t_deep=80, margin=2, deep_exit_band=0.02)
    assert out == "ACCUMULATE"


def test_deep_value_holds_on_tiny_price_breach():
    # 0.5% above the 200WMA is within the exit band -> hold DEEP_VALUE (no whipsaw).
    out = scoring.tier_hysteresis(score=85, price=201.0, wma200=200.0,
                                  prev_tier="DEEP_VALUE", t_watch=40, t_acc=60,
                                  t_deep=80, margin=2, deep_exit_band=0.02)
    assert out == "DEEP_VALUE"


def test_deep_value_holds_when_price_below_wma():
    # Price still below the 200WMA + score clears -> stays DEEP_VALUE (raw == prev).
    out = scoring.tier_hysteresis(score=85, price=190.0, wma200=200.0,
                                  prev_tier="DEEP_VALUE", t_watch=40, t_acc=60,
                                  t_deep=80, margin=2)
    assert out == "DEEP_VALUE"


def test_deep_value_score_driven_downgrade_uses_margin():
    # Score genuinely dropped below t_deep (price gate irrelevant) -> normal margin.
    out = scoring.tier_hysteresis(score=79, price=190.0, wma200=200.0,
                                  prev_tier="DEEP_VALUE", t_watch=40, t_acc=60,
                                  t_deep=80, margin=2)
    assert out == "DEEP_VALUE"  # 79 > 80 - 2, inside dead-band
    out = scoring.tier_hysteresis(score=77, price=190.0, wma200=200.0,
                                  prev_tier="DEEP_VALUE", t_watch=40, t_acc=60,
                                  t_deep=80, margin=2)
    assert out == "ACCUMULATE"  # 77 <= 78


# --- store: cooldown counts sent only; contiguous source; OI floor -----------

def test_failed_send_not_recorded_does_not_block_retry(conn):
    # A sent=0 row must NOT count as the cooldown memory.
    store.record_st_alert(conn, ts=_ms(1), created_at="2026-01-01T00:00:00+00:00",
                          trigger_key="ema_cross_bull", timeframe="4h", direction="BUY",
                          price=1.0, message="x", sent=False)
    assert store.last_st_alert(conn, "ema_cross_bull", "4h") is None
    store.record_st_alert(conn, ts=_ms(2), created_at="2026-01-02T00:00:00+00:00",
                          trigger_key="ema_cross_bull", timeframe="4h", direction="BUY",
                          price=1.0, message="x", sent=True)
    assert store.last_st_alert(conn, "ema_cross_bull", "4h")["ts"] == _ms(2)


def test_recent_candles_contiguous_source(conn):
    # Older Kraken batch then newer OKX batch -> contiguous read keeps OKX only.
    store.upsert_candles(conn, "4h", [(_ms(1), 1, 1, 1, 1, 1), (_ms(2), 1, 1, 1, 1, 1)],
                         source="kraken")
    store.upsert_candles(conn, "4h", [(_ms(3), 1, 1, 1, 1, 1), (_ms(4), 1, 1, 1, 1, 1)],
                         source="okx")
    all_rows = store.recent_candles(conn, "4h", 10)
    assert len(all_rows) == 4
    contig = store.recent_candles(conn, "4h", 10, contiguous_source=True)
    assert [r["ts"] for r in contig] == [_ms(3), _ms(4)]
    assert all(r["source"] == "okx" for r in contig)


def test_oi_at_or_before_rejects_stale_baseline(conn):
    # A sample 5 days before the target is older than the floor -> rejected.
    store.record_derivs(conn, ts=_ms(1), funding=None, oi=1000.0, oi_chg_pct=None)
    target = _ms(6)
    floor = _ms(5)  # not_before: only accept >= day 5
    assert store.oi_at_or_before(conn, target) == 1000.0          # unbounded finds it
    assert store.oi_at_or_before(conn, target, not_before_ms=floor) is None  # too old


# --- batched ST message ------------------------------------------------------

def test_batch_message_single_falls_back_to_single():
    items = [{"trigger": Trigger("ema_cross_bull", "BUY", "EMA cross"), "timeframe": "4h",
              "score": 30.0, "state": "BUY", "price": 60000.0, "indicators": {}, "regime": "bull"}]
    title, _ = alerting.build_st_batch_message(items, "BUY")
    assert "4h" in title and "BUY" in title


def test_batch_message_groups_multiple():
    items = [
        {"trigger": Trigger("ema_cross_bull", "BUY", "EMA cross"), "timeframe": "4h",
         "score": 30.0, "state": "BUY", "price": 60000.0, "indicators": {}, "regime": "bull"},
        {"trigger": Trigger("macd_bull_cross", "BUY", "MACD cross"), "timeframe": "4h",
         "score": 30.0, "state": "BUY", "price": 60000.0, "indicators": {}, "regime": "bull"},
        {"trigger": Trigger("rsi_oversold_bounce", "BUY", "RSI bounce"), "timeframe": "1d",
         "score": 30.0, "state": "BUY", "price": 60000.0, "indicators": {}, "regime": "bull"},
    ]
    title, body = alerting.build_st_batch_message(items, "BUY")
    assert "3 triggers" in title
    assert "2 on 4h" in title and "1 on 1d" in title
    # every trigger label appears in the body
    assert "EMA cross" in body and "MACD cross" in body and "RSI bounce" in body

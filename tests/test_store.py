"""SQLite store tests (WAL, time-series tables, cooldown memory, prune)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import store


@pytest.fixture()
def conn(tmp_path):
    c = store.connect(str(tmp_path / "t.db"))
    store.init_db(c)
    yield c
    c.close()


def _ms(day):
    return int(datetime(2026, 1, day, tzinfo=timezone.utc).timestamp() * 1000)


def test_wal_enabled(conn):
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_upsert_candles_idempotent_and_updates(conn):
    store.upsert_candles(conn, "4h", [(_ms(1), 1, 2, 0.5, 1.5, 10)])
    store.upsert_candles(conn, "4h", [(_ms(1), 1, 2, 0.5, 1.9, 12)])  # same ts -> replace
    rows = store.recent_candles(conn, "4h")
    assert len(rows) == 1
    assert rows[0]["close"] == pytest.approx(1.9)
    assert rows[0]["volume"] == pytest.approx(12)


def test_recent_candles_oldest_to_newest_and_limit(conn):
    store.upsert_candles(conn, "1d", [(_ms(d), 1, 1, 1, float(d), 1) for d in (3, 1, 2)])
    rows = store.recent_candles(conn, "1d", limit=2)
    assert [r["ts"] for r in rows] == [_ms(2), _ms(3)]  # newest 2, returned ascending


def test_st_alert_cooldown_memory(conn):
    assert store.last_st_alert(conn, "ema_cross_bull", "4h") is None
    store.record_st_alert(conn, ts=_ms(1), created_at="2026-01-01T00:00:00+00:00",
                          trigger_key="ema_cross_bull", timeframe="4h",
                          direction="BUY", price=100, message="m", sent=True)
    store.record_st_alert(conn, ts=_ms(2), created_at="2026-01-02T00:00:00+00:00",
                          trigger_key="ema_cross_bull", timeframe="4h",
                          direction="BUY", price=110, message="m2", sent=True)
    last = store.last_st_alert(conn, "ema_cross_bull", "4h")
    assert last["ts"] == _ms(2)
    # different timeframe is independent
    assert store.last_st_alert(conn, "ema_cross_bull", "1d") is None


def test_oi_helpers(conn):
    assert store.latest_oi(conn) is None
    assert store.oi_at_or_before(conn, _ms(2)) is None
    store.record_derivs(conn, ts=_ms(1), funding=None, oi=1000.0, oi_chg_pct=None)
    store.record_derivs(conn, ts=_ms(3), funding=None, oi=750.0, oi_chg_pct=None)
    assert store.latest_oi(conn) == pytest.approx(750.0)            # newest sample
    assert store.oi_at_or_before(conn, _ms(2)) == pytest.approx(1000.0)  # newest <= ts
    assert store.oi_at_or_before(conn, _ms(3)) == pytest.approx(750.0)
    assert store.oi_at_or_before(conn, _ms(1) - 1) is None          # nothing that old


def test_derivs_and_signals_roundtrip(conn):
    store.record_derivs(conn, ts=_ms(1), funding=-0.0003, oi=1000.0, oi_chg_pct=5.0)
    assert store.recent_derivs(conn)[-1]["funding"] == pytest.approx(-0.0003)
    store.record_st_signal(conn, ts=_ms(1), timeframe="4h", price=100.0,
                           st_score=42.0, st_state="BUY", indicators={"rsi": 55})
    sig = store.latest_st_signal(conn, "4h")
    assert sig["st_state"] == "BUY"
    assert sig["indicators"]["rsi"] == 55


def test_latest_run_roundtrip(conn):
    store.record_run(conn, run_ts="2026-01-01T00:00:00+00:00", price=100, composite=58.0,
                     tier="WATCH", active_cats=["price", "sentiment"], readings={"x": 1},
                     tier_alerted=True, flash_alerted=False)
    latest = store.latest_run(conn)
    assert latest["tier"] == "WATCH"
    assert latest["readings"] == {"x": 1}


def test_subscriber_lifecycle(conn):
    assert store.list_active_subscribers(conn) == []
    tok, is_new = store.upsert_subscriber(
        conn, email="A@B.com", token="tok1", created_at="2026-01-01T00:00:00+00:00")
    assert is_new is True and tok == "tok1"
    assert store.list_active_subscribers(conn) == [("a@b.com", "tok1")]  # lowercased
    # re-subscribe is idempotent: keeps the original token, reports not-new
    tok2, is_new2 = store.upsert_subscriber(
        conn, email="a@b.com", token="tok2", created_at="2026-01-03T00:00:00+00:00")
    assert is_new2 is False and tok2 == "tok1"
    # unsubscribe by token, then it drops from the active list
    assert store.deactivate_subscriber(conn, "tok1") == "a@b.com"
    assert store.list_active_subscribers(conn) == []
    # idempotent; unknown token -> None
    assert store.deactivate_subscriber(conn, "tok1") == "a@b.com"
    assert store.deactivate_subscriber(conn, "nope") is None


def test_prune_drops_only_old_rows(conn):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    old = now_ms - 500 * 86400 * 1000
    store.upsert_candles(conn, "1d", [(old, 1, 1, 1, 1, 1), (now_ms, 2, 2, 2, 2, 2)])
    store.prune(conn, days=400)
    rows = store.recent_candles(conn, "1d")
    assert len(rows) == 1 and rows[0]["ts"] == now_ms

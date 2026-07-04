"""Watchdog: per-pipeline staleness (a fresh collector must not mask a dead run)
and debounced re-alerting."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import stock_lt_store, stock_store, store, watchdog
from tests.factories import make_config


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "wd.db")
    # Never actually send; capture whether an alert WOULD go out via dry_run=False
    # by stubbing notify.send to a recorder.
    sent = []
    monkeypatch.setattr(watchdog.notify, "send",
                        lambda cfg, title, body, severity=None: sent.append(title) or True)
    return path, sent


def _seed(path, *, collect_ts=None, run_ts=None, stock_ts=None, lt_ts=None):
    conn = store.connect(path)
    store.init_db(conn)
    stock_store.init_stock_db(conn)
    stock_lt_store.init_stock_lt_db(conn)
    if collect_ts is not None:
        ms = int(collect_ts.timestamp() * 1000)
        store.record_derivs(conn, ts=ms, funding=None, oi=1.0, oi_chg_pct=None)
    if run_ts is not None:
        store.record_run(conn, run_ts=run_ts.isoformat(), price=1, composite=1.0,
                         tier="NEUTRAL", active_cats=["price"], readings={},
                         tier_alerted=False, flash_alerted=False, notified_tier="NEUTRAL")
    if stock_ts is not None:
        stock_store.record_stock_run(conn, run_ts=stock_ts.isoformat(),
                                     universe_n=1, scored_n=1, readings={})
    if lt_ts is not None:
        stock_lt_store.record_lt_run(conn, run_ts=lt_ts.isoformat(),
                                     universe_n=1, scored_n=1, survivors_n=1, readings={})
    conn.close()


def test_fresh_collector_does_not_mask_dead_run(db):
    path, sent = db
    now = datetime.now(timezone.utc)
    # Collector fresh (5 min ago) but the long-term run is 2 days stale.
    _seed(path, collect_ts=now - timedelta(minutes=5), run_ts=now - timedelta(days=2))
    cfg = make_config(db_path=path, watchdog_stale_hours=3)
    res = watchdog.check(cfg, dry_run=False)
    assert res["collect_stale"] is False
    assert res["run_stale"] is True
    assert res["stale"] is True
    assert res["alerted"] is True and len(sent) == 1


def test_both_fresh_is_healthy(db):
    path, sent = db
    now = datetime.now(timezone.utc)
    _seed(path, collect_ts=now - timedelta(minutes=5), run_ts=now - timedelta(hours=2))
    cfg = make_config(db_path=path, watchdog_stale_hours=3)
    res = watchdog.check(cfg, dry_run=False)
    assert res["stale"] is False and res["alerted"] is False and sent == []


def test_never_run_stock_pipeline_is_not_watched(db):
    # A box with no stock keys never records a stock run; that must NOT alert
    # (never-run == "not enabled on this box", not "dead").
    path, sent = db
    now = datetime.now(timezone.utc)
    _seed(path, collect_ts=now - timedelta(minutes=5), run_ts=now - timedelta(hours=2))
    cfg = make_config(db_path=path, watchdog_stale_hours=3)
    res = watchdog.check(cfg, dry_run=False)
    assert res["stock_stale"] is False and res["lt_stale"] is False
    assert res["stale"] is False and sent == []


def test_stale_stock_swing_alerts_once_it_has_run(db):
    # BTC pipelines fresh; the daily stock collector went dark 60h ago (> 50h).
    path, sent = db
    now = datetime.now(timezone.utc)
    _seed(path, collect_ts=now - timedelta(minutes=5), run_ts=now - timedelta(hours=2),
          stock_ts=now - timedelta(hours=60))
    cfg = make_config(db_path=path, watchdog_stale_hours=3)
    res = watchdog.check(cfg, dry_run=False)
    assert res["stock_stale"] is True and res["stale"] is True
    assert res["alerted"] is True and len(sent) == 1


def test_stale_stock_longterm_alerts(db):
    # Everything else fresh; the weekly long-term run is ~9 days stale (> 200h).
    path, sent = db
    now = datetime.now(timezone.utc)
    _seed(path, collect_ts=now - timedelta(minutes=5), run_ts=now - timedelta(hours=2),
          stock_ts=now - timedelta(hours=10), lt_ts=now - timedelta(hours=220))
    cfg = make_config(db_path=path, watchdog_stale_hours=3)
    res = watchdog.check(cfg, dry_run=False)
    assert res["lt_stale"] is True and res["stock_stale"] is False
    assert res["stale"] is True and res["alerted"] is True


def test_debounce_suppresses_repeat(db):
    path, sent = db
    now = datetime.now(timezone.utc)
    _seed(path, collect_ts=now - timedelta(hours=10), run_ts=now - timedelta(hours=10))
    cfg = make_config(db_path=path, watchdog_stale_hours=3)
    # First check alerts...
    assert watchdog.check(cfg, dry_run=False)["alerted"] is True
    # ...an immediate re-check is within the debounce window -> no second email.
    res2 = watchdog.check(cfg, dry_run=False)
    assert res2["stale"] is True and res2["alerted"] is False
    assert len(sent) == 1

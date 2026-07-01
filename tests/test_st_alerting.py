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


# --- honesty copy: the no-edge disclaimer applies to ALL swing triggers -------

def test_candle_trigger_carries_no_edge_disclaimer():
    from app.shortterm import Trigger
    _, body = alerting.build_st_message(
        trigger=Trigger("ema_cross_bull", "BUY", "EMA 9/21 bullish cross"),
        timeframe="4h", score=37.0, state="BUY", price=63000.0,
        indicators={"rsi": (55.0, 50.0)})
    assert "coin-flip" in body and "no demonstrated edge" in body


def test_funding_trigger_gets_unvalidated_note():
    from app.shortterm import Trigger
    _, body = alerting.build_st_message(
        trigger=Trigger("funding_spike_bull", "BUY", "Funding deeply negative"),
        timeframe="4h", score=0.0, state="NEUTRAL", price=63000.0, indicators={})
    assert "unvalidated" in body and "no backtest coverage" in body


def test_flow_trigger_gets_forward_test_note():
    from app.shortterm import Trigger
    _, body = alerting.build_st_message(
        trigger=Trigger("liq_long_flush", "BUY", "Long-liquidation flush"),
        timeframe="4h", score=0.0, state="NEUTRAL", price=63000.0, indicators={})
    assert "FORWARD-TEST" in body and "coin-flip" in body


def test_batch_message_does_not_claim_conviction():
    from app.shortterm import Trigger
    items = [
        {"trigger": Trigger("ema_cross_bull", "BUY", "EMA cross"), "timeframe": "4h",
         "score": 30.0, "state": "BUY", "price": 60000.0, "indicators": {}, "regime": "bull"},
        {"trigger": Trigger("funding_spike_bull", "BUY", "Funding spike"), "timeframe": "4h",
         "score": 30.0, "state": "BUY", "price": 60000.0, "indicators": {}, "regime": "bull"},
    ]
    _, body = alerting.build_st_batch_message(items, "BUY")
    assert "higher conviction" not in body
    assert "no demonstrated edge" in body
    assert "unvalidated" in body            # the funding trigger's note is included


# --- collector: per-series Coinalyze staleness + cron-overlap lock ------------

def _flow_bars(tss):
    return [{"ts": t, "close": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i,
             "volume": 10.0, "buyvol": 5.0} for i, t in enumerate(tss)]


def _patch_coinalyze(monkeypatch, ohlcv, oi, liq):
    from app.sources import coinalyze
    monkeypatch.setattr(coinalyze, "ohlcv_history", lambda *a, **k: ohlcv)
    monkeypatch.setattr(coinalyze, "oi_history", lambda *a, **k: oi)
    monkeypatch.setattr(coinalyze, "liquidations_history", lambda *a, **k: liq)


def test_collect_flow_darkens_only_the_stale_series(monkeypatch):
    from app import collect_once
    from tests.factories import make_config
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    h4 = 4 * 3600_000
    fresh_ts = [now_ms - i * h4 for i in range(20, 0, -1)]   # last closed bar fresh
    stale_ts = [t - 12 * h4 for t in fresh_ts]               # whole series ~2 days old

    ohlcv = _flow_bars(fresh_ts)
    oi_stale = [{"ts": t, "oi": 1000.0 + 100 * i} for i, t in enumerate(stale_ts)]
    liq_fresh = [{"ts": t, "long": 1.0, "short": 1.0} for t in fresh_ts]
    _patch_coinalyze(monkeypatch, ohlcv, oi_stale, liq_fresh)

    out = collect_once._collect_flow(make_config(coinalyze_api_key="k", st_timeframes=("4h",)))
    assert out is not None                       # fresh series keep the layer up...
    assert out["participant"] is None            # ...but the stale-OI read is dark
    assert out["readings"]["participant"] is None
    assert out["readings"]["liq_long_usd"] is not None   # fresh liq series still read


def test_collect_flow_fresh_oi_yields_participant(monkeypatch):
    from app import collect_once
    from tests.factories import make_config
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    h4 = 4 * 3600_000
    fresh_ts = [now_ms - i * h4 for i in range(20, 0, -1)]
    _patch_coinalyze(monkeypatch, _flow_bars(fresh_ts),
                     [{"ts": t, "oi": 1000.0 + 100 * i} for i, t in enumerate(fresh_ts)],
                     [{"ts": t, "long": 1.0, "short": 1.0} for t in fresh_ts])
    out = collect_once._collect_flow(make_config(coinalyze_api_key="k", st_timeframes=("4h",)))
    assert out is not None and out["participant"] is not None


def test_collect_flow_all_stale_layer_dark(monkeypatch):
    from app import collect_once
    from tests.factories import make_config
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    h4 = 4 * 3600_000
    stale_ts = [now_ms - (i + 12) * h4 for i in range(20, 0, -1)]
    _patch_coinalyze(monkeypatch, _flow_bars(stale_ts),
                     [{"ts": t, "oi": 1000.0} for t in stale_ts],
                     [{"ts": t, "long": 1.0, "short": 1.0} for t in stale_ts])
    out = collect_once._collect_flow(make_config(coinalyze_api_key="k", st_timeframes=("4h",)))
    assert out is None


def test_collect_once_overlap_lock_skips(tmp_path, monkeypatch):
    from app import collect_once, store
    from tests.factories import make_config
    db = str(tmp_path / "c.db")
    conn = store.connect(db)
    store.init_db(conn)
    now_ts = datetime.now(timezone.utc).timestamp()
    assert store.try_acquire_lock(conn, collect_once._LOCK_KEY, now_ts,
                                  collect_once._LOCK_TTL_S) is True
    conn.close()
    # any fetch would mean the lock failed to short-circuit
    monkeypatch.setattr(collect_once.price, "get_intraday_frames",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetched")))
    out = collect_once.run(make_config(db_path=db), dry_run=False)
    assert out["skipped"] == "overlap-lock" and out["alerts"] == []

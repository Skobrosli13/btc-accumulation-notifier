"""run_once: free oi_flush derived from stored OKX open interest."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import run_once, store
from tests.factories import make_config

_WINDOW_MS = 24 * 3600 * 1000


def _seed(db, *, old_oi, new_oi):
    conn = store.connect(db)
    store.init_db(conn)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if old_oi is not None:
        store.record_derivs(conn, ts=now_ms - _WINDOW_MS - 60_000, funding=None,
                            oi=old_oi, oi_chg_pct=None)
    if new_oi is not None:
        store.record_derivs(conn, ts=now_ms, funding=None, oi=new_oi, oi_chg_pct=None)
    conn.close()


def _stub_readings(monkeypatch, oi_flush):
    monkeypatch.setattr(run_once, "gather_readings", lambda c: (
        {"oi_flush": oi_flush, "fng": None, "drop_24_48h_pct": None},
        {"price": 60000.0, "wma200": None}))


def test_free_oi_flush_computed(monkeypatch, tmp_path):
    db = str(tmp_path / "r.db")
    _seed(db, old_oi=1000.0, new_oi=750.0)        # -25% over the window
    _stub_readings(monkeypatch, oi_flush=None)
    res = run_once.run(make_config(db_path=db, oi_flush_window_hours=24), dry_run=True)
    # value -25 maps to subscore 1.0 (threshold neutral=0, extreme=-25)
    assert res["subscores"]["oi_flush"] == pytest.approx(1.0)


def test_free_oi_flush_insufficient_history(monkeypatch, tmp_path):
    db = str(tmp_path / "r.db")
    _seed(db, old_oi=None, new_oi=750.0)          # no baseline within the window
    _stub_readings(monkeypatch, oi_flush=None)
    res = run_once.run(make_config(db_path=db, oi_flush_window_hours=24), dry_run=True)
    assert res["subscores"]["oi_flush"] is None


def test_free_oi_flush_does_not_clobber_paid(monkeypatch, tmp_path):
    db = str(tmp_path / "r.db")
    _seed(db, old_oi=1000.0, new_oi=750.0)
    _stub_readings(monkeypatch, oi_flush=-5.0)     # a paid Coinglass value is present
    res = run_once.run(make_config(db_path=db, oi_flush_window_hours=24), dry_run=True)
    # -5 (not the -25 the free path would compute) -> subscore 0.2, proving no overwrite
    assert res["subscores"]["oi_flush"] == pytest.approx(0.2)


# --- cycle ATH: stored 1d history beats the venue fetch window ----------------

def _stub_price_struct(monkeypatch, **ps_over):
    ps = {"price": 60000.0, "wma200": None, "source": "exchange"}
    ps.update(ps_over)
    monkeypatch.setattr(run_once, "gather_readings", lambda c: (
        {"fng": None, "drop_24_48h_pct": None}, ps))


def test_cycle_ath_prefers_stored_history_when_higher(monkeypatch, tmp_path):
    db = str(tmp_path / "r.db")
    conn = store.connect(db)
    store.init_db(conn)
    # stored 1d history (never pruned) carries a HIGHER close than the venue
    # window's max — e.g. the true top slid out of the OKX 300-week window
    ath_ms = int(datetime(2025, 10, 6, tzinfo=timezone.utc).timestamp() * 1000)
    store.upsert_candles(conn, "1d", [(ath_ms, 1, 1, 1, 126000.0, 1.0)], source="okx")
    conn.close()
    _stub_price_struct(monkeypatch, ath_date="2026-01-15", ath_price=90000.0)
    res = run_once.run(make_config(db_path=db), dry_run=True)
    assert res["cycle_ath"]["source"] == "stored"
    assert res["cycle_ath"]["date"] == "2025-10-06"
    assert res["cycle_ath"]["price"] == pytest.approx(126000.0)


def test_cycle_ath_keeps_venue_when_higher(monkeypatch, tmp_path):
    db = str(tmp_path / "r.db")
    conn = store.connect(db)
    store.init_db(conn)
    lo_ms = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    store.upsert_candles(conn, "1d", [(lo_ms, 1, 1, 1, 80000.0, 1.0)], source="okx")
    conn.close()
    _stub_price_struct(monkeypatch, ath_date="2025-10-06", ath_price=126000.0)
    res = run_once.run(make_config(db_path=db), dry_run=True)
    assert res["cycle_ath"]["source"] == "venue"
    assert res["cycle_ath"]["date"] == "2025-10-06"


def test_cycle_ath_ignores_stored_without_venue_price(monkeypatch, tmp_path):
    # CoinGecko fallback (365d window) must not let a shallow stored history
    # override the config cycle date either — no venue ATH price to compare.
    db = str(tmp_path / "r.db")
    conn = store.connect(db)
    store.init_db(conn)
    lo_ms = int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp() * 1000)
    store.upsert_candles(conn, "1d", [(lo_ms, 1, 1, 1, 70000.0, 1.0)], source="okx")
    conn.close()
    _stub_price_struct(monkeypatch, source="coingecko", ath_date="2026-02-01",
                       ath_price=70000.0)
    res = run_once.run(make_config(db_path=db), dry_run=True)
    assert res["cycle_ath"]["source"] == "config"
    assert res["cycle_ath"]["date"] == "2025-10-06"   # the config fallback


def test_degraded_flag_set_when_onchain_missing(monkeypatch, tmp_path):
    db = str(tmp_path / "r.db")
    _stub_readings(monkeypatch, oi_flush=None)    # no on-chain readings at all
    res = run_once.run(make_config(db_path=db), dry_run=True)
    assert res["degraded"] is True

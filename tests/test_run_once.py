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

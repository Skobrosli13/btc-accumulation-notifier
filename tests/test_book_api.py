"""/api/book — paper-book detail surface (redesign P4)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import api, store
from app.harness import schema
from tests.factories import make_config


def _client(tmp_path, seed=True):
    db = str(tmp_path / "book.db")
    conn = store.connect(db)
    store.init_db(conn)
    schema.init_harness_db(conn)
    if seed:
        conn.execute(
            "INSERT INTO paper_positions (study, source, ticker, event_ts, qty, "
            "entry_ts, entry_px, exit_ts, exit_px, status, horizon_sessions, "
            "sizing_basis) VALUES "
            "('insider_cluster','lab','AAA',1,0.05,2,100.0,3,105.0,'CLOSED',21,"
            "'kelly_vol_cap')")
        conn.execute(
            "INSERT INTO paper_positions (study, source, ticker, event_ts, status, "
            "skip_reason) VALUES "
            "('insider_cluster','lab','BBB',4,'SKIPPED','limits:max_concurrent')")
        # a SHORT swing pick that made money as price fell
        conn.execute(
            "INSERT INTO paper_positions (study, source, ticker, event_ts, direction, "
            "qty, entry_ts, entry_px, exit_ts, exit_px, status, exit_reason, "
            "horizon_sessions, sizing_basis) VALUES "
            "('swing:pead_drift','swing','CCC',5,'SHORT',0.02,6,100.0,7,90.0,"
            "'CLOSED','target',10,'vol_parity_only')")
        for study, nav, at, bench in (("insider_cluster", 1.01, 1.006, 1.005),
                                      ("@lab", 1.01, 1.006, 1.005),
                                      ("@combined", 1.03, 1.018, 1.005)):
            conn.execute("INSERT INTO paper_nav (study, date, nav, nav_after_tax, "
                         "bench, n_open) VALUES (?,'2026-07-02',?,?,?,1)",
                         (study, nav, at, bench))
        conn.commit()
    conn.close()
    cfg = make_config(db_path=db, api_token="secret")
    api.app.dependency_overrides[api.get_config] = lambda: cfg
    return TestClient(api.app)


def test_book_requires_token_and_serves(tmp_path):
    try:
        client = _client(tmp_path)
        assert client.get("/api/book").status_code == 401
        j = client.get("/api/book", headers={"Authorization": "Bearer secret"}).json()
        assert j["counts"] == {"CLOSED": 2, "SKIPPED": 1}
        closed = next(p for p in j["positions"] if p["ticker"] == "AAA")
        assert closed["ret_pct"] == 5.0
        skipped = next(p for p in j["positions"] if p["status"] == "SKIPPED")
        assert skipped["skip_reason"] == "limits:max_concurrent"  # skips stay visible
        assert j["curves"]["insider_cluster"][0]["nav"] == 1.01
        assert j["curves"]["insider_cluster"][0]["bench"] == 1.005
    finally:
        api.app.dependency_overrides.clear()


def test_short_return_is_signed_by_direction(tmp_path):
    """A short that exits BELOW entry made money. Reading the raw price ratio
    would report the book's winners as losers."""
    try:
        client = _client(tmp_path)
        j = client.get("/api/book", headers={"Authorization": "Bearer secret"}).json()
        short = next(p for p in j["positions"] if p["ticker"] == "CCC")
        assert short["direction"] == "SHORT"
        assert short["ret_pct"] == 10.0            # 100 -> 90 short = +10%
    finally:
        api.app.dependency_overrides.clear()


def test_lab_and_combined_curves_are_served_separately(tmp_path):
    """The meta-gate reads '@lab'; the portfolio view reads '@combined'. Serving
    one number for both would let forward-test picks flatter the edge claim."""
    try:
        client = _client(tmp_path)
        j = client.get("/api/book", headers={"Authorization": "Bearer secret"}).json()
        assert j["summary"]["lab"]["nav"] == 1.01
        assert j["summary"]["combined"]["nav"] == 1.03
        # after-tax excess vs SPY total return is what the meta-gate judges
        assert j["summary"]["lab"]["excess_after_tax"] == pytest.approx(0.001)
    finally:
        api.app.dependency_overrides.clear()


def test_by_source_labels_what_is_validated(tmp_path):
    try:
        client = _client(tmp_path)
        j = client.get("/api/book", headers={"Authorization": "Bearer secret"}).json()
        by_src = {s["source"]: s for s in j["by_source"]}
        assert by_src["lab"]["basis"] == "validated"
        assert by_src["swing"]["basis"] == "forward-test"
        assert by_src["swing"]["namespaces"] == ["swing:pead_drift"]
    finally:
        api.app.dependency_overrides.clear()


def test_book_empty_shape(tmp_path):
    try:
        client = _client(tmp_path, seed=False)
        j = client.get("/api/book", headers={"Authorization": "Bearer secret"}).json()
        assert j["positions"] == [] and j["curves"] == {} and j["counts"] == {}
        assert j["by_source"] == []
        assert j["summary"] == {"combined": None, "lab": None}
    finally:
        api.app.dependency_overrides.clear()

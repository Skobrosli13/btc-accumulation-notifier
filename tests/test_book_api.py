"""/api/book — paper-book detail surface (redesign P4)."""
from __future__ import annotations

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
            "INSERT INTO paper_positions (study, ticker, event_ts, qty, entry_ts, "
            "entry_px, exit_ts, exit_px, status, horizon_sessions) VALUES "
            "('insider_cluster','AAA',1,0.05,2,100.0,3,105.0,'CLOSED',21)")
        conn.execute(
            "INSERT INTO paper_positions (study, ticker, event_ts, status, skip_reason) "
            "VALUES ('insider_cluster','BBB',4,'SKIPPED','limits:max_concurrent')")
        conn.execute("INSERT INTO paper_nav (study, date, nav, bench, n_open) VALUES "
                     "('insider_cluster','2026-07-02',1.01,1.005,1)")
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
        assert j["counts"] == {"CLOSED": 1, "SKIPPED": 1}
        closed = next(p for p in j["positions"] if p["status"] == "CLOSED")
        assert closed["ret_pct"] == 5.0
        skipped = next(p for p in j["positions"] if p["status"] == "SKIPPED")
        assert skipped["skip_reason"] == "limits:max_concurrent"  # skips stay visible
        assert j["nav"][0]["nav"] == 1.01 and j["nav"][0]["bench"] == 1.005
    finally:
        api.app.dependency_overrides.clear()


def test_book_empty_shape(tmp_path):
    try:
        client = _client(tmp_path, seed=False)
        j = client.get("/api/book", headers={"Authorization": "Bearer secret"}).json()
        assert j["positions"] == [] and j["nav"] == [] and j["counts"] == {}
    finally:
        api.app.dependency_overrides.clear()

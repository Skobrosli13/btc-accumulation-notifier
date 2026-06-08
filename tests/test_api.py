"""Read-only API tests (auth, schemas, health)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import api, store
from tests.factories import make_config


@pytest.fixture()
def client(tmp_path):
    db = str(tmp_path / "api.db")
    conn = store.connect(db)
    store.init_db(conn)
    # populate a little of everything
    store.record_run(conn, run_ts="2026-06-08T00:00:00+00:00", price=63000, composite=58.0,
                     tier="WATCH", active_cats=["price", "sentiment"], readings={"x": 1},
                     tier_alerted=True, flash_alerted=False)
    base = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
    step = 4 * 3600_000
    rows = [(base + i * step, 100 + i, 101 + i, 99 + i, 100 + i, 10.0) for i in range(40)]
    store.upsert_candles(conn, "4h", rows)
    store.record_derivs(conn, ts=base, funding=-0.0003, oi=1000.0, oi_chg_pct=2.0)
    store.record_st_signal(conn, ts=base, timeframe="4h", price=140.0, st_score=37.0,
                           st_state="BUY", indicators={"rsi": [55, 50]})
    store.record_st_alert(conn, ts=base, created_at="2026-06-08T00:00:00+00:00",
                          trigger_key="ema_cross_bull", timeframe="4h", direction="BUY",
                          price=140.0, message="m", sent=True)
    conn.close()

    cfg = make_config(db_path=db, api_token="secret", st_timeframes=("4h",))
    api.app.dependency_overrides[api.get_config] = lambda: cfg
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def _auth():
    return {"Authorization": "Bearer secret"}


def test_requires_token(client):
    assert client.get("/api/health").status_code == 401
    assert client.get("/api/longterm/latest").status_code == 401


def test_health_ok(client):
    r = client.get("/api/health", headers=_auth())
    assert r.status_code == 200
    j = r.json()
    assert j["db_ok"] is True
    assert j["exchange"] == "okx"
    assert "last_collect" in j and "layers" in j


def test_longterm_latest(client):
    j = client.get("/api/longterm/latest", headers=_auth()).json()
    assert j["latest"]["tier"] == "WATCH"
    assert j["latest"]["readings"] == {"x": 1}


def test_shortterm_latest(client):
    j = client.get("/api/shortterm/latest", headers=_auth()).json()
    assert j["timeframes"]["4h"]["st_state"] == "BUY"


def test_candles_and_indicators(client):
    c = client.get("/api/candles?timeframe=4h&limit=10", headers=_auth()).json()
    assert c["timeframe"] == "4h" and len(c["candles"]) == 10
    # ascending order
    assert c["candles"][0]["ts"] < c["candles"][-1]["ts"]
    ind = client.get("/api/indicators?timeframe=4h", headers=_auth()).json()
    assert ind["indicators"] is not None
    assert "state" in ind


def test_alerts_feed(client):
    j = client.get("/api/alerts", headers=_auth()).json()
    assert any(a["trigger_key"] == "ema_cross_bull" for a in j["short_term"])
    assert any(a["tier"] == "WATCH" for a in j["long_term"])

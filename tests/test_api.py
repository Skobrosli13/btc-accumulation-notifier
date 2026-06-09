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
    readings = {
        "raw": {"fng": 8, "mvrv_z": 0.1, "realized_ratio": 0.95},
        "price_struct": {"price": 63000, "wma200": 62000, "dma200": 78000,
                         "price_to_wma200": 1.016, "mayer_multiple": 0.81,
                         "drop_24_48h_pct": -4.2, "source": "exchange"},
        "subscores": {"fng": 0.84, "price_to_wma200": 0.3, "mayer": 0.7},
        "category_scores": {"price": 0.5, "sentiment": 0.84, "onchain": None,
                            "macro": None, "derivs": None},
        "cycle_multiplier": 0.975,
    }
    store.record_run(conn, run_ts="2026-06-08T00:00:00+00:00", price=63000, composite=58.0,
                     tier="WATCH", active_cats=["price", "sentiment"], readings=readings,
                     tier_alerted=True, flash_alerted=False, notified_tier="WATCH")
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
    # on-chain is free-by-default now -> active layer + reported source
    assert j["layers"]["onchain"] is True
    assert j["onchain_source"] == "bitcoin-data"


def test_longterm_latest(client):
    j = client.get("/api/longterm/latest", headers=_auth()).json()
    assert j["latest"]["tier"] == "WATCH"
    bd = j["latest"]["breakdown"]
    assert {c["key"] for c in bd["categories"]} == {"onchain", "price", "macro", "sentiment", "derivs"}
    price_cat = next(c for c in bd["categories"] if c["key"] == "price")
    assert price_cat["active"] is True and price_cat["weight"] == 0.20
    # redundancy grouping is surfaced: price/200WMA + Mayer share a group
    p2w = next(i for i in price_cat["indicators"] if i["key"] == "price_to_wma200")
    assert p2w["group"] == "price_to_wma200"
    assert "Fear & Greed" in bd["in_zone"]              # fng subscore 0.84 >= 0.6
    assert bd["levels"]["wma200_rel"] == "above"        # price 63000 > wma200 62000
    assert bd["cycle"]["multiplier"] == 0.975 and "days_since_ath" in bd["cycle"]
    assert bd["tiers"]["accumulate"] == 60


def test_shortterm_latest(client):
    j = client.get("/api/shortterm/latest", headers=_auth()).json()
    sig = j["timeframes"]["4h"]
    assert sig["st_state"] == "BUY"
    assert isinstance(sig["triggers"], list)            # present (may be empty)
    assert any(c["key"] == "funding" for c in sig["components"])  # funding present in derivs
    assert sig["funding"] == -0.0003


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
    lt = next(a for a in j["long_term"] if a["tier"] == "WATCH")
    assert lt["reason"]["tier_label"] == "Watch"
    assert lt["reason"]["type"] == "tier"
    assert "Fear & Greed" in lt["reason"]["in_zone"]
    assert "readings" not in lt          # bulky readings stripped from payload


def test_playbook_endpoint(client):
    assert client.get("/api/playbook").status_code == 401   # token-gated
    j = client.get("/api/playbook", headers=_auth()).json()
    assert "playbook" in j and "what_to_do" in j and "tier" in j


def test_track_record(client):
    assert client.get("/api/track_record").status_code == 401   # token-gated
    j = client.get("/api/track_record", headers=_auth()).json()
    assert "available" in j
    if j["available"]:
        assert "horizons" in j and isinstance(j["horizons"], dict)


def test_subscribe_unsubscribe_flow(tmp_path):
    db = str(tmp_path / "sub.db")
    conn = store.connect(db)
    store.init_db(conn)
    conn.close()
    # resend_api_key stays None -> no welcome email is scheduled (no network in tests)
    cfg = make_config(db_path=db, api_token="secret")
    api.app.dependency_overrides[api.get_config] = lambda: cfg
    try:
        c = TestClient(api.app)
        assert c.post("/api/subscribe", json={"email": "nope"}, headers=_auth()).status_code == 400
        assert c.post("/api/subscribe", json={"email": "a@b.com"}).status_code == 401  # token gate
        r = c.post("/api/subscribe", json={"email": "Friend@Example.com"}, headers=_auth())
        assert r.status_code == 200
        assert r.json()["new"] is True and r.json()["email"] == "friend@example.com"  # lowercased
        # idempotent re-subscribe
        assert c.post("/api/subscribe", json={"email": "friend@example.com"},
                      headers=_auth()).json()["new"] is False

        ro = store.connect_readonly(db)
        subs = store.list_active_subscribers(ro)
        ro.close()
        assert len(subs) == 1 and subs[0][0] == "friend@example.com"
        token = subs[0][1]

        # GET is a non-mutating confirmation page (mail scanners must not be able
        # to unsubscribe by merely fetching the link); the subscriber stays active.
        g = c.get(f"/api/unsubscribe?token={token}")
        assert g.status_code == 200 and "unsubscribe" in g.text.lower()
        ro = store.connect_readonly(db)
        assert len(store.list_active_subscribers(ro)) == 1
        ro.close()

        # POST (button / RFC 8058 one-click) performs the deactivation.
        u = c.post(f"/api/unsubscribe?token={token}")
        assert u.status_code == 200 and "unsubscribed" in u.text.lower()
        ro = store.connect_readonly(db)
        assert store.list_active_subscribers(ro) == []
        ro.close()

        bad = c.post("/api/unsubscribe?token=bogus")
        assert bad.status_code == 200 and "invalid" in bad.text.lower()
    finally:
        api.app.dependency_overrides.clear()


def test_email_recipients_dedupe(tmp_path):
    from app import notify
    db = str(tmp_path / "r.db")
    conn = store.connect(db)
    store.init_db(conn)
    store.upsert_subscriber(conn, email="owner@x.com", token="t-owner",
                            created_at="2026-01-01T00:00:00+00:00")
    store.upsert_subscriber(conn, email="friend@x.com", token="t-friend",
                            created_at="2026-01-02T00:00:00+00:00")
    cfg = make_config(email_to="Owner@X.com")
    rec = notify._email_recipients(cfg, conn)
    conn.close()
    # owner is deduped onto the subscriber entry (so they get an unsubscribe token too)
    assert rec == {"owner@x.com": "t-owner", "friend@x.com": "t-friend"}

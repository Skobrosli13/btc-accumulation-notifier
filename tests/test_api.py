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


def test_health_per_indicator_availability(client):
    # Config-driven layer badges can't see a dead source; the per-indicator block
    # reads what each source actually RETURNED in the persisted runs.
    j = client.get("/api/health", headers=_auth()).json()
    inds = j["indicators"]
    assert inds["fng"]["available"] is True and inds["fng"]["runs_with_data"] == 1
    assert inds["fng"]["runs_checked"] == 1
    assert inds["mayer"]["available"] is False and inds["mayer"]["last_seen"] is None
    assert "mayer" in j["dark_indicators"] and "fng" not in j["dark_indicators"]


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


def test_lt_breakdown_prefers_run_cycle_ath():
    # The cycle panel must derive ath date/price from the SAME ATH the run's
    # multiplier used (readings["cycle_ath"], incl. the stored-history override),
    # not the venue-window price_struct sitting next to it.
    from datetime import date
    cfg = make_config()
    latest = {"readings": {
        "cycle_multiplier": 1.01,
        "cycle_ath": {"date": "2025-12-01", "price": 130000.0, "source": "stored"},
        "price_struct": {"ath_date": "2025-10-06", "ath_price": 126000.0},
    }}
    cyc = api._lt_breakdown(latest, cfg)["cycle"]
    assert cyc["ath_date"] == "2025-12-01"
    assert cyc["ath_price"] == 130000.0
    assert cyc["ath_source"] == "stored"
    assert cyc["days_since_ath"] == (datetime.now(timezone.utc).date()
                                     - date(2025, 12, 1)).days


def test_lt_breakdown_cycle_ath_fallbacks():
    cfg = make_config()
    # pre-migration run (no cycle_ath persisted): the venue price_struct feeds it
    latest = {"readings": {
        "price_struct": {"ath_date": "2025-10-06", "ath_price": 126000.0}}}
    cyc = api._lt_breakdown(latest, cfg)["cycle"]
    assert cyc["ath_date"] == "2025-10-06"
    assert cyc["ath_price"] == 126000.0
    assert cyc["ath_source"] == "venue"
    # nothing at all: config date
    cyc2 = api._lt_breakdown({"readings": {}}, cfg)["cycle"]
    assert cyc2["ath_date"] == cfg.ath_date.isoformat()
    assert cyc2["ath_source"] == "config"


def test_trigger_stats_unmeasured_marker():
    wr = {"ema_cross_bull": {"n": 124, "win_rate": 0.508}}
    assert api._trigger_stats(wr, "ema_cross_bull")["n"] == 124
    # keys with no cell (funding/OI, flow) get an explicit marker, not None
    assert api._trigger_stats(wr, "funding_spike_bull") == {"unmeasured": True}
    assert api._trigger_stats({}, "liq_long_flush") == {"unmeasured": True}


def test_live_performance_contract(tmp_path):
    """Response shape of /api/live_performance (the dashboard builds against this)."""
    db = str(tmp_path / "lp.db")
    conn = store.connect(db)
    store.init_db(conn)
    day = 86_400_000
    base = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    # 60 daily candles; the endpoint drops the newest (possibly forming) one
    store.upsert_candles(conn, "1d",
                         [(base + d * day, 100.0 + d, 101.0 + d, 99.0 + d, 100.0 + d, 1.0)
                          for d in range(60)], source="okx")
    run_iso = datetime.fromtimestamp((base + 2 * day) / 1000, tz=timezone.utc).isoformat()
    store.record_run(conn, run_ts=run_iso, price=102.0, composite=65.0, tier="ACCUMULATE",
                     active_cats=["price"], readings={}, tier_alerted=True,
                     flash_alerted=False, notified_tier="ACCUMULATE")
    # two rows for one confluence-gated event (candle + flow trigger)
    store.record_st_alert(conn, ts=base + 2 * day, created_at=run_iso,
                          trigger_key="ema_cross_bull", timeframe="4h", direction="BUY",
                          price=102.0, message="m", sent=True)
    store.record_st_alert(conn, ts=base + 2 * day, created_at=run_iso,
                          trigger_key="liq_long_flush", timeframe="4h", direction="BUY",
                          price=102.0, message="m", sent=True)
    conn.close()
    cfg = make_config(db_path=db, api_token="secret", st_timeframes=("4h",))
    api.app.dependency_overrides[api.get_config] = lambda: cfg
    try:
        j = TestClient(api.app).get("/api/live_performance", headers=_auth()).json()
    finally:
        api.app.dependency_overrides.clear()

    lt = j["long_term"]
    assert set(lt) == {"horizons", "window"}
    h30 = lt["horizons"]["30"]
    assert {"n_runs", "n_signal", "signal_hit_rate", "base_rate",
            "episodes", "episode_hit_rate",
            "episodes_effective", "episode_hit_rate_effective", "ci"} <= set(h30)
    assert h30["n_signal"] == 1 and h30["episodes"] == 1
    assert h30["episodes_effective"] == 1     # spaced (honest) n passed through
    assert lt["window"]["from"] == run_iso

    st = j["short_term"]
    assert st["n_events"] == 1                       # same-candle rows deduped to one event
    assert st["by_direction"]["BUY"]["n"] == 1
    # flow keys get their own forward-test scoreboard row
    assert set(st["by_key"]) == {"ema_cross_bull", "liq_long_flush"}
    assert st["base_rate"] is not None and st["horizon_days"] == 7
    assert "note" in st and "note" in j


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


def test_schema_init_runs_on_startup_not_import(tmp_path, monkeypatch):
    """§0.5: no import-time side effects — schema init moved into the FastAPI
    lifespan, so it runs when the ASGI server starts, not when app.api imports."""
    calls = {"n": 0}
    monkeypatch.setattr(api, "_ensure_schema", lambda: calls.__setitem__("n", calls["n"] + 1))
    cfg = make_config(db_path=str(tmp_path / "l.db"), api_token="secret")
    api.app.dependency_overrides[api.get_config] = lambda: cfg
    try:
        # Entering the TestClient context manager drives the ASGI lifespan startup.
        with TestClient(api.app):
            pass
    finally:
        api.app.dependency_overrides.clear()
    assert calls["n"] >= 1


def test_unsubscribe_get_is_xss_safe(tmp_path):
    """A hostile ``token`` must never be reflected raw into the confirm page.

    Regression for a reflected XSS: the GET endpoint bypasses the bearer token,
    so any query string is attacker-controlled. A malformed token gets the
    inert 'invalid' page (no form, no reflection); the response also carries a
    locked-down CSP.
    """
    db = str(tmp_path / "xss.db")
    conn = store.connect(db)
    store.init_db(conn)
    conn.close()
    cfg = make_config(db_path=db, api_token="secret")
    api.app.dependency_overrides[api.get_config] = lambda: cfg
    try:
        c = TestClient(api.app)
        payload = '"><script>alert(1)</script>'
        g = c.get("/api/unsubscribe", params={"token": payload})
        assert g.status_code == 200
        # Not reflected verbatim; the injection is either dropped (malformed
        # token -> no form) or HTML-escaped. Either way no live <script> tag.
        assert "<script>alert(1)</script>" not in g.text
        assert "invalid" in g.text.lower()  # malformed token -> invalid page
        # CSP present on the unsubscribe surface.
        assert g.headers.get("content-security-policy") == "default-src 'none'"
    finally:
        api.app.dependency_overrides.clear()


def test_subscribe_rejects_html_metachars_in_email(tmp_path):
    """The tightened _EMAIL_RE keeps HTML metacharacters out of stored emails
    (defence in depth with output escaping on the unsubscribe page)."""
    db = str(tmp_path / "email.db")
    conn = store.connect(db)
    store.init_db(conn)
    conn.close()
    cfg = make_config(db_path=db, api_token="secret")
    api.app.dependency_overrides[api.get_config] = lambda: cfg
    try:
        c = TestClient(api.app)
        for bad in ('a<b@x.com', 'a"b@x.com', "a'b@x.com", "a&b@x.com", "a>b@x.com"):
            r = c.post("/api/subscribe", json={"email": bad}, headers=_auth())
            assert r.status_code == 400, bad
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

"""/api/paper — the consolidated paper-trading surface (dual-track redesign).

The live account (@broker) and the meta-gate evidence (@lab) must be served as
clearly distinct things; forward-test picks must never flatter the edge claim."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app import api, store
from app.harness import schema
from tests.factories import make_config

NOW_MS = int(time.time() * 1000)


def _client(tmp_path, *, seed=True, reconcile_age_min=1.0):
    db = str(tmp_path / "paper.db")
    conn = store.connect(db)
    store.init_db(conn)
    schema.init_harness_db(conn)
    if seed:
        conn.execute(
            "INSERT INTO broker_orders (client_order_id, broker_order_id, intent_id, "
            "namespace, source, ticker, asset, side, target_qty, limit_px, tif, "
            "adv_cap_shares, sizing_fraction, sizing_basis, status, reject_reason, "
            "submitted_ts, updated_ts) VALUES ('coid1','ord1',1,'swing:pead_drift',"
            "'swing','AAA','EQ','buy',20,20.02,'day',20,0.02,'vol_parity_only',"
            "'filled','adv_capped',?,?)", (NOW_MS, NOW_MS))
        conn.execute(
            "INSERT INTO broker_positions (symbol, asset, qty, avg_entry_px, "
            "market_px, unrealized_pnl, updated_ts) VALUES "
            "('AAA','EQ',20,20.02,20.4,7.6,?)", (NOW_MS,))
        conn.execute(
            "INSERT INTO fills (event_id, asset, side, qty, limit_px, fill_px, "
            "fill_ts, venue, slippage_bps, client_order_id, namespace) VALUES "
            "(1,'EQ','buy',20,20.02,20.01,?,'alpaca-paper',-5.0,'coid1','swing:pead_drift')",
            (NOW_MS,))
        for study, nav, at, bench in (("@broker", 1.02, 1.02, 1.005),
                                      ("@lab", 1.01, 1.006, 1.005),
                                      ("@combined", 1.03, 1.018, 1.005)):
            conn.execute("INSERT INTO paper_nav (study, date, nav, nav_after_tax, "
                         "bench, n_open) VALUES (?,'2026-07-20',?,?,?,1)",
                         (study, nav, at, bench))
        rec = NOW_MS - int(reconcile_age_min * 60_000)
        for k, v in (("broker_last_equity", "102000.0"), ("broker_day_pnl", "310.0"),
                     ("broker_last_reconcile", str(rec)),
                     ("broker_go_live_equity", "100000.0")):
            conn.execute("INSERT OR REPLACE INTO lab_meta (key, value) VALUES (?,?)", (k, v))
        conn.commit()
    conn.close()
    cfg = make_config(db_path=db, api_token="secret")
    api.app.dependency_overrides[api.get_config] = lambda: cfg
    return TestClient(api.app)


def test_paper_requires_token_and_serves_account(tmp_path):
    try:
        client = _client(tmp_path)
        assert client.get("/api/paper").status_code == 401
        j = client.get("/api/paper", headers={"Authorization": "Bearer secret"}).json()
        acct = j["account"]
        assert acct["enabled"] and acct["source"] == "alpaca-paper"
        assert acct["equity"] == 102000.0 and acct["nav"] == 1.02
        assert acct["degraded"] is False
        # the live account holding carries its originating namespace/source.
        pos = j["positions"][0]
        assert pos["symbol"] == "AAA" and pos["source"] == "swing"
    finally:
        api.app.dependency_overrides.clear()


def test_paper_separates_live_from_meta_gate(tmp_path):
    try:
        client = _client(tmp_path)
        j = client.get("/api/paper", headers={"Authorization": "Bearer secret"}).json()
        # three distinct curves; @lab is the labeled meta-gate evidence.
        assert set(j["curves"]) == {"@broker", "@lab", "@combined"}
        assert j["research"]["lab"]["nav"] == 1.01
        assert j["research"]["combined"]["nav"] == 1.03
        assert "meta-gate" in j["note"] and "@lab" in j["note"]
        # execution delta from the live fills.
        assert j["execution_delta"]["n_fills"] == 1
        assert j["execution_delta"]["mean_slippage_bps"] == -5.0
        assert j["execution_delta"]["adv_capped"] == 1     # target_qty == adv_cap_shares
    finally:
        api.app.dependency_overrides.clear()


def test_paper_degraded_when_reconcile_stale(tmp_path):
    try:
        client = _client(tmp_path, reconcile_age_min=300.0)   # > 180 min threshold
        j = client.get("/api/paper", headers={"Authorization": "Bearer secret"}).json()
        assert j["account"]["degraded"] is True
    finally:
        api.app.dependency_overrides.clear()


def test_paper_empty_shape(tmp_path):
    try:
        client = _client(tmp_path, seed=False)
        j = client.get("/api/paper", headers={"Authorization": "Bearer secret"}).json()
        assert j["account"] is None
        assert j["positions"] == [] and j["orders"] == [] and j["fills"] == []
        assert j["curves"] == {}
        assert j["research"] == {"lab": None, "combined": None}
    finally:
        api.app.dependency_overrides.clear()

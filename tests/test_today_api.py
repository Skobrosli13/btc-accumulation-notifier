"""/api/today aggregation + act-window + digest render (redesign P2, Gap D).

The invariant under test: the Today page and the daily digest consume ONE
aggregation (aggregate_today) over ONE window definition (act_window_start_ms),
so they can never disagree about what "since the previous business day" means.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import api, schedule as sched, store
from app.api.today import aggregate_today
from app.harness import schema
from scripts.send_digest import render
from tests.factories import make_config


# ---------------------------------------------------------------- act window

def test_act_window_previous_business_day_midweek():
    # Wed 2026-07-01 18:00 UTC -> window opens Tue 2026-06-30 00:00 ET
    now = datetime(2026, 7, 1, 18, 0, tzinfo=timezone.utc)
    ms = sched.act_window_start_ms(now)
    et_hours = 4  # EDT
    assert ms == int(datetime(2026, 6, 30, et_hours, tzinfo=timezone.utc).timestamp() * 1000)


def test_act_window_monday_reaches_back_to_friday():
    # Mon 2026-07-06 13:00 UTC -> previous business day is Fri 2026-07-03
    now = datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)
    ms = sched.act_window_start_ms(now)
    assert ms == int(datetime(2026, 7, 3, 4, tzinfo=timezone.utc).timestamp() * 1000)


def test_act_window_sunday_also_friday():
    now = datetime(2026, 7, 5, 13, 0, tzinfo=timezone.utc)  # Sunday
    assert sched.act_window_start_ms(now) == sched.act_window_start_ms(
        datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc))


# ------------------------------------------------------------- aggregation

def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


@pytest.fixture()
def conn(tmp_path):
    c = store.connect(str(tmp_path / "today.db"))
    store.init_db(c)
    schema.init_harness_db(c)
    yield c
    c.close()


def _register(conn, name, tier, status):
    schema.register_study(conn, name=name, asset="EQ", evaluator="car", tier=tier,
                          spec_path=f"studies/{name}.md", registered_at=1,
                          primary_horizon=10)
    schema.set_study_status(conn, name, status,
                            verdict_at=1 if status != "REGISTERED" else None)


def test_aggregate_promoted_events_only_inside_window(conn):
    _register(conn, "insider_cluster", "alpha", "PROMOTED")
    _register(conn, "sue_pead", "alpha", "EXTEND")
    fresh, stale = _now_ms(), sched.act_window_start_ms() - 1
    schema.insert_events(conn, [
        {"study": "insider_cluster", "asset": "EQ", "ticker": "ABC",
         "event_ts": fresh, "direction": "LONG",
         "meta": {"n_managers": 3, "agg_usd": 250_000}},
        {"study": "insider_cluster", "asset": "EQ", "ticker": "OLD",
         "event_ts": stale, "direction": "LONG"},
        # EXTEND study: its events must NOT surface as act rows.
        {"study": "sue_pead", "asset": "EQ", "ticker": "XYZ",
         "event_ts": fresh, "direction": "LONG"},
    ])
    out = aggregate_today(conn)
    events = [a for a in out["act"] if a["kind"] == "event"]
    assert [e["ticker"] for e in events] == ["ABC"]
    assert events[0]["label"] == "PROMOTED"
    assert events[0]["stale"] is True   # never-synced lab -> demoted framing
    assert "3 insider(s)" in events[0]["detail"]
    # testing strip carries every study verbatim + a concrete next decision
    assert {t["name"]: t["status"] for t in out["testing"]} == {
        "insider_cluster": "PROMOTED", "sue_pead": "EXTEND"}
    extend = next(t for t in out["testing"] if t["name"] == "sue_pead")
    assert "review" in extend["next_decision"] and "1" in extend["next_decision"]
    # §4: the digest's one-line health summary rides the same payload
    assert set(out["health"]) == {"collect_age_hours", "run_age_hours",
                                  "collect_stale", "run_stale"}


def test_aggregate_policy_studies_never_emit_event_rows(conn):
    # tier='policy' PROMOTED studies act through tier-change/trend-flip rows,
    # not per-event picks — the query filters on tier='alpha'.
    _register(conn, "btc_trend_policy", "policy", "PROMOTED")
    schema.insert_events(conn, [{"study": "btc_trend_policy", "asset": "BTC",
                                 "ticker": "BTC", "event_ts": _now_ms(),
                                 "direction": "LONG"}])
    out = aggregate_today(conn)
    assert [a for a in out["act"] if a["kind"] == "event"] == []


def _record_run(conn, ts, tier, froth=None):
    store.record_run(conn, run_ts=ts, price=60000, composite=50.0, tier=tier,
                     active_cats=["price"], readings={}, tier_alerted=False,
                     flash_alerted=False, notified_tier=tier, froth=froth)


def _iso_at(offset_ms: int) -> str:
    return datetime.fromtimestamp((_now_ms() + offset_ms) / 1000,
                                  tz=timezone.utc).isoformat()


DAY = 86_400_000


def test_aggregate_btc_tier_change_across_window(conn):
    # Gap D: the comparison is latest-vs-pre-window state, so a change ~12h old
    # (well past the previous 6h run) is still reported.
    _record_run(conn, _iso_at(-4 * DAY), "WATCH")           # pre-window state
    _record_run(conn, _iso_at(-12 * 3_600_000), "ACCUMULATE")
    _record_run(conn, _iso_at(0), "ACCUMULATE")             # latest, no flap
    out = aggregate_today(conn)
    tiers = [a for a in out["act"] if a["kind"] == "btc_tier"]
    assert len(tiers) == 1 and tiers[0]["detail"] == "WATCH → ACCUMULATE"


def test_aggregate_no_tier_change_when_stable(conn):
    _record_run(conn, _iso_at(-4 * DAY), "WATCH")
    _record_run(conn, _iso_at(0), "WATCH")
    assert [a for a in aggregate_today(conn)["act"] if a["kind"] == "btc_tier"] == []


def test_aggregate_stale_pipeline_reports_no_change(conn):
    # Latest run itself predates the window -> the state at window start IS the
    # current state; nothing happened "today".
    _record_run(conn, _iso_at(-6 * DAY), "WATCH")
    _record_run(conn, _iso_at(-5 * DAY), "ACCUMULATE")
    assert [a for a in aggregate_today(conn)["act"] if a["kind"] == "btc_tier"] == []


def test_aggregate_froth_escalation(conn):
    # WARMING (30) -> FROTHY (60) inside the window = an act row; de-escalation
    # is deliberately NOT actionable.
    _record_run(conn, _iso_at(-4 * DAY), "WATCH", froth=30.0)
    _record_run(conn, _iso_at(0), "WATCH", froth=60.0)
    out = aggregate_today(conn)
    froths = [a for a in out["act"] if a["kind"] == "froth"]
    assert len(froths) == 1 and froths[0]["detail"] == "WARMING → FROTHY"


def test_aggregate_froth_deescalation_silent(conn):
    _record_run(conn, _iso_at(-4 * DAY), "WATCH", froth=60.0)
    _record_run(conn, _iso_at(0), "WATCH", froth=30.0)
    assert [a for a in aggregate_today(conn)["act"] if a["kind"] == "froth"] == []


def test_aggregate_trend_flip_inside_window(conn):
    # 220 closed dailies: flat below MA, then a decisive breakout on the most
    # recent CLOSED candle (which lands inside the act window).
    day = 86_400_000
    now = _now_ms()
    closes = [100.0] * 219 + [150.0, 150.0]  # last (still-forming) is dropped
    # Last candle stamped `now`, so the flip candle sits exactly 24h back —
    # always inside the act window (whose start is always >24h in the past).
    rows = [(now - (len(closes) - 1 - i) * day, c, c, c, c, 1.0)
            for i, c in enumerate(closes)]
    store.upsert_candles(conn, "1d", rows)
    out = aggregate_today(conn)
    flips = [a for a in out["act"] if a["kind"] == "trend_flip"]
    assert len(flips) == 1 and flips[0]["detail"] == "FLAT → LONG"


def test_aggregate_paper_and_sync_blocks(conn):
    conn.execute("INSERT INTO paper_nav (study, date, nav, bench, n_open) "
                 "VALUES ('insider_cluster', '2026-07-02', 1.0132, 1.0050, 2)")
    conn.execute("INSERT INTO paper_positions (study, ticker, event_ts, status) "
                 "VALUES ('insider_cluster','A',1,'OPEN'), "
                 "('insider_cluster','B',2,'PENDING'), "
                 "('insider_cluster','C',3,'CLOSED')")
    conn.execute("INSERT INTO lab_meta (key, value) VALUES ('last_sync', ?)",
                 (datetime.now(timezone.utc).isoformat(),))
    conn.commit()
    out = aggregate_today(conn)
    assert out["paper"] == {"nav": 1.0132, "bench": 1.0050, "nav_after_tax": None,
                            "date": "2026-07-02",
                            "open": 1, "pending": 1, "closed": 1}
    assert out["lab_sync"]["overdue"] is False


def test_aggregate_empty_db_shape(conn):
    out = aggregate_today(conn)
    assert out["act"] == [] and out["testing"] == []
    assert out["paper"]["nav"] is None and out["paper"]["open"] == 0
    assert out["lab_sync"]["overdue"] is True  # never synced -> honest flag


# ------------------------------------------------------------------ endpoint

def test_endpoint_requires_token_and_serves(tmp_path):
    db = str(tmp_path / "t.db")
    c = store.connect(db)
    store.init_db(c)
    schema.init_harness_db(c)
    c.close()
    cfg = make_config(db_path=db, api_token="secret")
    api.app.dependency_overrides[api.get_config] = lambda: cfg
    try:
        client = TestClient(api.app)
        assert client.get("/api/today").status_code == 401
        r = client.get("/api/today", headers={"Authorization": "Bearer secret"})
        assert r.status_code == 200
        j = r.json()
        assert set(j) >= {"window_start_ms", "act", "testing", "paper", "lab_sync"}
    finally:
        api.app.dependency_overrides.clear()


# ------------------------------------------------------------------- digest

def test_digest_render_with_act_rows(conn):
    _register(conn, "insider_cluster", "alpha", "PROMOTED")
    schema.insert_events(conn, [{"study": "insider_cluster", "asset": "EQ",
                                 "ticker": "ABC", "event_ts": _now_ms(),
                                 "direction": "LONG",
                                 "meta": {"n_managers": 2, "agg_usd": 90_000}}])
    conn.execute("INSERT INTO lab_meta (key, value) VALUES ('last_sync', ?)",
                 (datetime.now(timezone.utc).isoformat(),))
    conn.commit()
    title, body = render(aggregate_today(conn))
    assert title.startswith("Daily digest — 1 item")
    assert "[PROMOTED] ABC LONG" in body      # fresh sync -> no STALE FEED tag
    assert "insider_cluster = PROMOTED" in body
    assert "Health: collector" in body        # §4 health summary rides along


def test_digest_render_stale_feed_demotes_events(conn):
    # Gap C: never-synced lab -> the digest tags the event row STALE FEED.
    _register(conn, "insider_cluster", "alpha", "PROMOTED")
    schema.insert_events(conn, [{"study": "insider_cluster", "asset": "EQ",
                                 "ticker": "ABC", "event_ts": _now_ms(),
                                 "direction": "LONG"}])
    _, body = render(aggregate_today(conn))
    assert "[PROMOTED — STALE FEED] ABC" in body


def test_digest_render_quiet_day(conn):
    title, body = render(aggregate_today(conn))
    assert title == "Daily digest — nothing to do"
    assert "Nothing needs you today." in body
    # never-synced lab is called out rather than hidden
    assert "Lab sync OVERDUE" in body

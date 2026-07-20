"""Bridge: surfaced stock picks -> the shared paper book."""
from __future__ import annotations

import sqlite3

import pytest

from app.harness import schema
from app.portfolio import bridge

DAY = 86_400_000


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    schema.init_harness_db(c)
    from app import stock_lt_store, stock_store
    stock_store.init_stock_db(c)
    stock_lt_store.init_stock_lt_db(c)
    return c


def _add_swing(c, ticker, *, direction="BUY", entry=100.0, stop=95.0, t2=110.0,
               archetype="pead_drift", day=10, time_stop_days=8):
    c.execute(
        "INSERT INTO stock_positions (ticker, opened_ts, direction, archetype, "
        "entry, stop, t1, t2, time_stop_days, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')",
        (ticker, day * DAY, direction, archetype, entry, stop, None, t2,
         time_stop_days))
    c.commit()


def _add_lt(c, ticker, *, day=20, entry=50.0):
    c.execute("INSERT INTO stock_lt_holdings (ticker, opened_ts, entry, status) "
              "VALUES (?, ?, ?, 'OPEN')", (ticker, day * DAY, entry))
    c.commit()


def test_levels_convert_to_basis_free_fractions():
    """The pick is struck on an as-traded basis, the book prices adjusted bars.
    Only the RATIO survives that translation, so only the ratio is carried."""
    c = _conn()
    _add_swing(c, "AAA", entry=100.0, stop=95.0, t2=110.0)
    picks = bridge.swing_picks(c)["swing:pead_drift"]
    assert len(picks) == 1
    p = picks[0]
    assert p["stop_frac"] == pytest.approx(-0.05)
    assert p["target_frac"] == pytest.approx(0.10)
    assert p["direction"] == "LONG"
    assert p["horizon"] == 8
    c.close()


def test_sell_becomes_short_with_the_stop_above_entry():
    c = _conn()
    _add_swing(c, "BBB", direction="SELL", entry=100.0, stop=104.0, t2=88.0)
    p = bridge.swing_picks(c)["swing:pead_drift"][0]
    assert p["direction"] == "SHORT"
    assert p["stop_frac"] == pytest.approx(0.04)      # above entry for a short
    assert p["target_frac"] == pytest.approx(-0.12)
    c.close()


def test_unpriceable_levels_are_dropped_not_filed_with_a_broken_stop():
    c = _conn()
    _add_swing(c, "NOENTRY", entry=None, stop=95.0)
    _add_swing(c, "ZEROWIDTH", entry=100.0, stop=100.0)   # risk of 0
    assert bridge.swing_picks(c) == {}
    c.close()


def test_archetypes_get_their_own_namespace():
    c = _conn()
    _add_swing(c, "AAA", archetype="pead_drift")
    _add_swing(c, "BBB", archetype="expl_gap")
    ns = bridge.swing_picks(c)
    assert set(ns) == {"swing:pead_drift", "swing:expl_gap"}
    c.close()


def test_file_picks_is_idempotent_and_tags_the_source():
    c = _conn()
    _add_swing(c, "AAA")
    _add_lt(c, "CCC")
    first = bridge.file_picks(c)
    assert first["swing:pead_drift"] == 1 and first["longterm:qvm"] == 1
    again = bridge.file_picks(c)
    assert sum(again.values()) == 0                  # UNIQUE key converges

    rows = {r["ticker"]: dict(r) for r in
            c.execute("SELECT * FROM paper_positions")}
    assert rows["AAA"]["source"] == "swing"
    assert rows["AAA"]["horizon_sessions"] == 8
    assert rows["CCC"]["source"] == "longterm"
    assert rows["CCC"]["horizon_sessions"] == bridge.LT_HORIZON_SESSIONS
    assert rows["CCC"]["stop_frac"] is None          # long-buys carry no stop
    assert all(r["status"] == "PENDING" for r in rows.values())
    c.close()


def test_mixed_horizons_in_one_archetype_are_filed_separately():
    """horizon_sessions is per-position; two picks of the same archetype with
    different time-stops must not collapse onto one horizon."""
    c = _conn()
    _add_swing(c, "AAA", time_stop_days=5)
    _add_swing(c, "BBB", time_stop_days=12)
    bridge.file_picks(c)
    rows = {r["ticker"]: r["horizon_sessions"] for r in
            c.execute("SELECT ticker, horizon_sessions FROM paper_positions")}
    assert rows == {"AAA": 5, "BBB": 12}
    c.close()


def test_since_ts_bounds_the_backfill():
    """Filing history wholesale would backfill a record the book never lived
    through; the nightly can bound what it picks up."""
    c = _conn()
    _add_swing(c, "OLD", day=1)
    _add_swing(c, "NEW", day=30)
    picks = bridge.swing_picks(c, since_ts=10 * DAY)["swing:pead_drift"]
    assert [p["ticker"] for p in picks] == ["NEW"]
    c.close()


def test_go_live_floor_refuses_to_backfill_collector_history():
    """The collectors have months of history. Filing it would manufacture an
    equity curve the book never lived through — the exact fake track record the
    program exists to avoid, and the opposite of what /api/book promises. The
    first run must file NOTHING and start the clock."""
    c = _conn()
    _add_swing(c, "OLD", day=1)
    _add_lt(c, "ALSOOLD", day=2)
    now = 100 * DAY

    since = bridge.go_live_ts(c, now)
    assert since == now
    assert sum(bridge.file_picks(c, since_ts=since).values()) == 0
    assert c.execute("SELECT count(*) FROM paper_positions").fetchone()[0] == 0

    # the floor is stamped once and survives later runs — a second night must
    # not silently re-open the window and sweep the history in
    assert bridge.go_live_ts(c, now + 30 * DAY) == now

    # a pick that surfaces AFTER go-live is filed normally
    _add_swing(c, "NEW", day=110)
    assert bridge.file_picks(c, since_ts=since)["swing:pead_drift"] == 1
    rows = [r["ticker"] for r in c.execute("SELECT ticker FROM paper_positions")]
    assert rows == ["NEW"]
    c.close()


def test_sector_is_attached_so_the_sector_limit_can_bind():
    c = _conn()
    _add_swing(c, "AAA")
    p = bridge.swing_picks(c, sectors={"AAA": "Technology"})["swing:pead_drift"][0]
    assert p["sector"] == "Technology"
    c.close()

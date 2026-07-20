"""Stage-0 paper book — fill/skip/close lifecycle + NAV math, hand-checked."""
from __future__ import annotations

import sqlite3

import pytest

from app.harness import schema
from app.portfolio import book

DAY = 86_400_000


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    schema.init_harness_db(c)
    return c


def _bars(n_days: int, *, alt_until: int = 60, level: float = 100.0,
          post: float = 110.0, start_day: int = 0) -> list[dict]:
    """Alternating ±1% closes (real vol) until ``alt_until``, then flat ``post``."""
    out = []
    for k in range(n_days):
        day = start_day + k
        if k < alt_until:
            close = level * (1.01 if k % 2 else 0.99)
        else:
            close = post
        out.append({"ts": day * DAY, "open": level, "high": max(level, close),
                    "low": min(level, close), "close": close, "volume": 1.0})
    return out


def _event(ticker, day, tier="small", sector="Tech"):
    return {"ticker": ticker, "event_ts": day * DAY, "tier": tier, "sector": sector}


def test_fill_close_and_nav_happy_path():
    c = _conn()
    bars = {"AAA": _bars(100)}                      # alternating 60, then flat 110
    spy = _bars(100, alt_until=0, post=100.0)       # flat benchmark
    assert book.record_pending(c, "insider_cluster", [_event("AAA", 65)],
                               horizon=3) == 1
    assert book.record_pending(c, "insider_cluster", [_event("AAA", 65)],
                               horizon=3) == 0      # idempotent

    stats = book.process(c, "insider_cluster", bars,
                         expectancy=0.009, car_std=0.09,
                         now_ms=99 * DAY)
    assert stats["filled"] == 1
    p = dict(c.execute("SELECT * FROM paper_positions").fetchone())
    # entry: next session after day 65 = day 66's open (100) +20bps (small tier 40bps RT)
    assert p["entry_ts"] == 66 * DAY
    assert p["entry_px"] == pytest.approx(100.0 * 1.002)
    # sizing: kelly quarter = .25*(.009/.0081)=0.278; vol-parity ~0.94; cap 0.07 binds
    assert p["qty"] == pytest.approx(0.07)
    # closed at entry_idx+3 close (flat 110) −20bps
    assert p["status"] == "CLOSED"
    assert p["exit_ts"] == 69 * DAY
    assert p["exit_px"] == pytest.approx(110.0 * 0.998)

    n = book.mark_nav(c, "insider_cluster", bars, spy)
    assert n > 0
    last = dict(c.execute(
        "SELECT * FROM paper_nav WHERE study='insider_cluster' ORDER BY date DESC LIMIT 1"
    ).fetchone())
    want_nav = 1.0 + 0.07 * (110.0 * 0.998 / (100.0 * 1.002) - 1.0)
    assert last["nav"] == pytest.approx(want_nav)
    assert last["bench"] == pytest.approx(1.0)      # flat SPY
    c.close()


def test_perfectly_correlated_candidate_is_skipped():
    c = _conn()
    shared = _bars(100)
    bars = {"AAA": shared, "BBB": [dict(b) for b in shared]}   # identical series
    book.record_pending(c, "s", [_event("AAA", 65), _event("BBB", 66)], horizon=10)
    stats = book.process(c, "s", bars, expectancy=0.009, car_std=0.09,
                         now_ms=80 * DAY)
    assert stats["filled"] == 1 and stats["skipped"] == 1
    skip = dict(c.execute(
        "SELECT * FROM paper_positions WHERE status='SKIPPED'").fetchone())
    assert "mean_corr" in skip["skip_reason"]       # 60d corr 1.0 > 0.6 rejected
    c.close()


def test_pending_expires_without_bars_and_gap_skips():
    c = _conn()
    # NOFILL: no bars at all after the event; GAP: bars start 20 days later
    bars = {"GAP": _bars(40, start_day=85)}
    book.record_pending(c, "s", [_event("NOFILL", 60), _event("GAP", 62)])
    book.process(c, "s", bars, now_ms=75 * DAY)     # 13 days later
    rows = {r["ticker"]: dict(r) for r in c.execute("SELECT * FROM paper_positions")}
    assert rows["NOFILL"]["status"] == "SKIPPED"
    assert rows["NOFILL"]["skip_reason"] == "no_fill"
    assert rows["GAP"]["status"] == "SKIPPED"
    assert rows["GAP"]["skip_reason"] == "entry_gap"
    c.close()


def test_open_position_marks_in_nav_before_close():
    c = _conn()
    bars = {"AAA": _bars(70)}                       # only 10 post-entry sessions
    spy = _bars(70, alt_until=0, post=100.0)
    book.record_pending(c, "s", [_event("AAA", 59)], horizon=21)  # can't close yet
    book.process(c, "s", bars, expectancy=0.009, car_std=0.09, now_ms=69 * DAY)
    p = dict(c.execute("SELECT * FROM paper_positions").fetchone())
    assert p["status"] == "OPEN"
    book.mark_nav(c, "s", bars, spy)
    last = dict(c.execute(
        "SELECT * FROM paper_nav ORDER BY date DESC LIMIT 1").fetchone())
    assert last["n_open"] == 1
    want = 1.0 + p["qty"] * (110.0 / p["entry_px"] - 1.0)     # marked at last close
    assert last["nav"] == pytest.approx(want)
    c.close()


# --- stop / target exits (swing source) ----------------------------------------
#
# Entry is always bar 60: 60 alternating ±1% sessions give _vol60 real risk to
# price, the event lands on day 59, and the fill is the next session's open.

def _vol_bars(n: int = 60, level: float = 100.0) -> list[dict]:
    out = []
    for k in range(n):
        close = level * (1.01 if k % 2 else 0.99)
        out.append({"ts": k * DAY, "open": level, "high": max(level, close),
                    "low": min(level, close), "close": close, "volume": 1.0})
    return out


def _bar(day, o, h, l, c_):
    return {"ts": day * DAY, "open": o, "high": h, "low": l, "close": c_,
            "volume": 1.0}


def _swing(ticker="AAA", *, direction="LONG", stop_frac=-0.05, target_frac=0.05):
    return {"ticker": ticker, "event_ts": 59 * DAY, "tier": "small",
            "sector": "Tech", "direction": direction,
            "stop_frac": stop_frac, "target_frac": target_frac}


def _fill(c, bars, **kw):
    """Record one swing pick and advance the book; returns the position row."""
    book.record_pending(c, "swing:t", [_swing(**kw)], 10, source="swing")
    book.process(c, "swing:t", {"AAA": bars}, source="swing", now_ms=80 * DAY)
    return dict(c.execute("SELECT * FROM paper_positions").fetchone())


def test_stop_and_target_rebase_onto_the_actual_fill():
    """Levels arrive as fractions and are struck against the book's own entry —
    the pick's absolute prices are on a different (unadjusted) basis entirely."""
    c = _conn()
    bars = _vol_bars() + [_bar(60, 100.0, 101.0, 99.0, 100.0)]
    p = _fill(c, bars)
    entry = 100.0 * 1.002                       # small tier 40bps RT, half crossed
    assert p["entry_px"] == pytest.approx(entry)
    assert p["stop_px"] == pytest.approx(entry * 0.95)
    assert p["target_px"] == pytest.approx(entry * 1.05)
    c.close()


def test_stop_wins_a_same_bar_stop_and_target_tie():
    """A bar that trades through BOTH levels is booked as the stop — the
    pessimistic assumption, matching stock_positions.reprice."""
    c = _conn()
    bars = _vol_bars() + [_bar(60, 100.0, 101.0, 99.0, 100.0),
                          _bar(61, 100.0, 130.0, 80.0, 100.0)]   # hits both
    p = _fill(c, bars)
    assert p["status"] == "CLOSED" and p["exit_reason"] == "stop"
    c.close()


def test_gap_through_the_stop_fills_at_the_open_not_the_stop():
    """A price the tape never offered is not a fill. Opening below the stop
    books the open — the loss a subscriber could actually take."""
    c = _conn()
    entry = 100.0 * 1.002
    gap_open = entry * 0.90                     # opens well below the 5% stop
    bars = _vol_bars() + [_bar(60, 100.0, 101.0, 99.0, 100.0),
                          _bar(61, gap_open, gap_open, gap_open * 0.98, gap_open)]
    p = _fill(c, bars)
    assert p["exit_reason"] == "stop"
    assert p["exit_px"] == pytest.approx(gap_open * 0.998)       # NOT the stop price
    assert p["exit_px"] < entry * 0.95                           # worse than the stop
    c.close()


def test_target_closes_when_the_stop_is_untouched():
    c = _conn()
    entry = 100.0 * 1.002
    bars = _vol_bars() + [_bar(60, 100.0, 101.0, 99.0, 100.0),
                          _bar(61, 100.0, entry * 1.06, 99.5, 105.0)]
    p = _fill(c, bars)
    assert p["exit_reason"] == "target"
    assert p["exit_px"] == pytest.approx(entry * 1.05 * 0.998)
    c.close()


def test_horizon_still_exits_when_no_level_trades():
    c = _conn()
    flat = [_bar(60 + k, 100.0, 100.5, 99.5, 100.0) for k in range(12)]
    p = _fill(c, _vol_bars() + flat)
    assert p["status"] == "CLOSED" and p["exit_reason"] == "horizon"
    assert p["exit_ts"] == 70 * DAY             # entry bar 60 + 10-session horizon
    c.close()


def test_short_crosses_the_spread_the_other_way_and_profits_when_price_falls():
    c = _conn()
    bars = _vol_bars() + [_bar(60, 100.0, 101.0, 99.0, 100.0)] + \
        [_bar(60 + k, 90.0, 90.5, 89.5, 90.0) for k in range(1, 12)]
    p = _fill(c, bars, direction="SHORT", stop_frac=0.05, target_frac=-0.20)
    # a short SELLS to open (hits the bid) and BUYS to close (pays the offer)
    assert p["entry_px"] == pytest.approx(100.0 * 0.998)
    assert p["status"] == "CLOSED"
    assert p["exit_px"] > 90.0                  # paid up to cover

    spy = [{"ts": b["ts"], "close": 100.0} for b in bars]
    book.mark_nav(c, "swing:t", {"AAA": bars}, spy)
    last = dict(c.execute("SELECT * FROM paper_nav ORDER BY date DESC LIMIT 1")
                .fetchone())
    assert last["nav"] > 1.0                    # price fell, the short made money
    c.close()


# --- roll-ups: the meta-gate wall ----------------------------------------------

def test_rollups_separate_lab_from_the_rest():
    """'@lab' must stay lab-only: the meta-gate judges the promoted study's
    curve, and a forward-test pick must never be able to flatter or spoil it."""
    c = _conn()
    lab_bars, swing_bars = _bars(100), _bars(100, level=50.0, post=40.0)
    spy = _bars(100, alt_until=0, post=100.0)
    book.record_pending(c, "insider_cluster", [_event("AAA", 65)], 3, source="lab")
    book.process(c, "insider_cluster", {"AAA": lab_bars},
                 expectancy=0.009, car_std=0.09, source="lab", now_ms=99 * DAY)
    book.record_pending(c, "swing:t", [_event("ZZZ", 65)], 3, source="swing")
    book.process(c, "swing:t", {"ZZZ": swing_bars}, source="swing", now_ms=99 * DAY)

    all_bars = {"AAA": lab_bars, "ZZZ": swing_bars}
    rolled = book.mark_rollups(c, all_bars, spy)
    assert rolled[book.NAV_LAB] > 0 and rolled[book.NAV_COMBINED] > 0

    lab_only = book.mark_nav(c, "insider_cluster", all_bars, spy) and dict(c.execute(
        "SELECT * FROM paper_nav WHERE study='insider_cluster' "
        "ORDER BY date DESC LIMIT 1").fetchone())
    at_lab = dict(c.execute(f"SELECT * FROM paper_nav WHERE study='{book.NAV_LAB}' "
                            "ORDER BY date DESC LIMIT 1").fetchone())
    at_all = dict(c.execute(f"SELECT * FROM paper_nav WHERE study='{book.NAV_COMBINED}'"
                            " ORDER BY date DESC LIMIT 1").fetchone())
    # the lab roll-up equals the lone lab study's own curve...
    assert at_lab["nav"] == pytest.approx(lab_only["nav"])
    # ...and the losing swing position drags @combined below it
    assert at_all["nav"] < at_lab["nav"]
    c.close()


def test_swing_positions_are_sized_on_parity_not_on_a_borrowed_edge():
    """A bridged pick carries no OOS stats, so it must land on the unvalidated
    path — recorded on the row, so the book can never be re-read as if the pick
    had been sized on a measured edge."""
    c = _conn()
    bars = _vol_bars() + [_bar(60 + k, 100.0, 100.5, 99.5, 100.0) for k in range(12)]
    p = _fill(c, bars)
    assert p["sizing_basis"] == "vol_parity_only"
    assert p["qty"] == pytest.approx(0.02)      # the unvalidated cap, not 7%
    c.close()

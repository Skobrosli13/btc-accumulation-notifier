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

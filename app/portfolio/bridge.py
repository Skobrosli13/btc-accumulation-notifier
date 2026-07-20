"""Bridge: surfaced stock picks -> the shared paper book (pure transforms).

The swing screener and the long-buy engine each keep their own operational
ledger (``stock_positions``, ``stock_lt_holdings``). Those tables stay the
source of truth for the SIGNAL — what fired, when, at what levels. This module
files the same picks into ``paper_positions`` so they are accounted, sized and
benchmarked in ONE portfolio alongside the lab's promoted studies, instead of
three disconnected forward-tests the dashboard has to reconcile.

The bridge is deliberately one-directional and idempotent: it only ever writes
PENDING rows, and ``paper_positions``' UNIQUE(study, ticker, event_ts) makes a
re-run converge. It never writes back to the stock tables, so nothing here can
corrupt the collectors' own record.

Two conversions matter:

* **Levels travel as fractions of entry, not prices.** A pick is struck on the
  collector's as-traded basis (Alpaca/Massive); the paper book prices off the
  lake's dividend+split-ADJUSTED bars. The same nominal $ stop means different
  things on the two scales, and the gap grows with every dividend. Converting
  to a signed fraction of the pick's own entry makes the level basis-free; the
  book rebases it onto whatever fill it actually gets.
* **Direction vocabulary.** The collectors say BUY/SELL; the book says
  LONG/SHORT.

Neither source has a pre-registered OOS expectancy, so both are filed for
vol-parity-only sizing under the unvalidated NAV cap (see ``sizing``). They are
forward-test positions in a shared book — never an edge claim.
"""
from __future__ import annotations

import sqlite3

from . import book

# The paper book's LT leg exits on a fixed quarter rather than tracking the
# collector's conviction-decay exit: the long-buy engine is a QUARTERLY
# rebalance by design, so a 63-session hold is the same policy expressed as a
# horizon. A holding the collector drops early keeps running here to its
# quarter — the divergence is bounded, stated, and visible in exit_reason.
LT_HORIZON_SESSIONS = 63
DEFAULT_SWING_HORIZON = 10

# Both universes are S&P-500 / large-cap screens, so 'large' is a description of
# the universe, not an optimistic cost assumption. (round_trip_bps defaults an
# unknown tier to MICRO — 80bps — which would be nonsense for these names.)
DEFAULT_TIER = "large"


def _frac(level: float | None, entry: float | None) -> float | None:
    """Signed distance of ``level`` from ``entry`` as a fraction (basis-free)."""
    if not level or not entry:
        return None
    return (level / entry) - 1.0


def swing_picks(conn: sqlite3.Connection, *, since_ts: int | None = None,
                sectors: dict[str, str] | None = None,
                tier: str = DEFAULT_TIER) -> dict[str, list[dict]]:
    """Surfaced swing picks grouped by book namespace ('swing:<archetype>').

    Reads ``stock_positions`` — which is already the SURFACED set (the collector
    only inserts rows for picks inside its top-N budget), so no extra filtering
    is needed to honour "promoted picks only". Rows whose levels are unusable
    (no entry, or a zero-width stop) are dropped rather than filed with a stop
    the book cannot price."""
    sectors = sectors or {}
    sql = ("SELECT ticker, opened_ts, direction, archetype, entry, stop, t2, "
           "time_stop_days FROM stock_positions WHERE opened_ts IS NOT NULL")
    params: list = []
    if since_ts is not None:
        sql += " AND opened_ts >= ?"
        params.append(int(since_ts))
    out: dict[str, list[dict]] = {}
    for r in conn.execute(sql, params).fetchall():
        d = dict(r)
        entry, stop = d.get("entry"), d.get("stop")
        if not entry or not stop or stop == entry:
            continue
        arch = d.get("archetype") or "unknown"
        tk = (d.get("ticker") or "").upper()
        if not tk:
            continue
        out.setdefault(f"swing:{arch}", []).append({
            "ticker": tk,
            "event_ts": int(d["opened_ts"]),
            "direction": "SHORT" if (d.get("direction") or "BUY").upper() == "SELL"
                         else "LONG",
            "stop_frac": _frac(stop, entry),
            "target_frac": _frac(d.get("t2"), entry),
            "horizon": int(d.get("time_stop_days") or DEFAULT_SWING_HORIZON),
            "tier": tier,
            "sector": sectors.get(tk),
        })
    return out


def lt_picks(conn: sqlite3.Connection, *, since_ts: int | None = None,
             sectors: dict[str, str] | None = None,
             tier: str = DEFAULT_TIER) -> list[dict]:
    """Surfaced long-buy holdings as book events (namespace 'longterm:qvm').

    Long-only by construction (the QVM engine screens for accumulation, never
    shorts) and carries no stop — conviction decay is its exit, approximated
    here by the quarterly horizon."""
    sectors = sectors or {}
    sql = ("SELECT ticker, opened_ts FROM stock_lt_holdings "
           "WHERE opened_ts IS NOT NULL")
    params: list = []
    if since_ts is not None:
        sql += " AND opened_ts >= ?"
        params.append(int(since_ts))
    out = []
    for r in conn.execute(sql, params).fetchall():
        tk = (dict(r).get("ticker") or "").upper()
        if not tk:
            continue
        out.append({
            "ticker": tk,
            "event_ts": int(dict(r)["opened_ts"]),
            "direction": "LONG",
            "tier": tier,
            "sector": sectors.get(tk),
        })
    return out


GO_LIVE_KEY = "bridge_go_live"


def go_live_ts(conn: sqlite3.Connection, now_ms: int) -> int:
    """Read (or stamp, on first call) the moment the bridge went live.

    This is the anti-backfill floor and it matters more than it looks. The
    collectors have been running for months, so ``stock_positions`` already
    holds a long history; filing all of it on the first nightly would
    manufacture a paper record the book never actually lived through — a
    backfilled equity curve is exactly the fake track record this whole system
    exists to avoid, and both /api/book's note and the page promise the
    opposite. Stamping the floor at first run means the book starts empty and
    earns every position forward, which is the honest (and less flattering)
    outcome."""
    row = conn.execute("SELECT value FROM lab_meta WHERE key=?",
                       (GO_LIVE_KEY,)).fetchone()
    if row and row[0]:
        return int(row[0])
    conn.execute("INSERT OR REPLACE INTO lab_meta (key, value) VALUES (?, ?)",
                 (GO_LIVE_KEY, str(int(now_ms))))
    conn.commit()
    return int(now_ms)


def file_picks(conn: sqlite3.Connection, *, since_ts: int | None = None,
               sectors: dict[str, str] | None = None) -> dict[str, int]:
    """File every surfaced swing + long-term pick as PENDING. Returns per-namespace
    counts of NEWLY filed rows (idempotent: a re-run files 0).

    ``since_ts`` bounds what is picked up — callers should pass
    :func:`go_live_ts` rather than None, or the collectors' entire history is
    filed at once."""
    filed: dict[str, int] = {}
    for ns, picks in swing_picks(conn, since_ts=since_ts, sectors=sectors).items():
        # One record_pending per horizon: the column is per-position, and swing
        # archetypes carry different time-stops.
        by_h: dict[int, list[dict]] = {}
        for p in picks:
            by_h.setdefault(p["horizon"], []).append(p)
        n = 0
        for horizon, group in by_h.items():
            n += book.record_pending(conn, ns, group, horizon, source="swing")
        filed[ns] = n
    lt = lt_picks(conn, since_ts=since_ts, sectors=sectors)
    if lt:
        filed["longterm:qvm"] = book.record_pending(
            conn, "longterm:qvm", lt, LT_HORIZON_SESSIONS, source="longterm")
    return filed

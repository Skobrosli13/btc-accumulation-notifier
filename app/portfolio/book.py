"""Stage-0 paper book (§7 + meta-gate §9) — trades a PROMOTED study on paper.

The meta-gate judges the program on an ALPHA-PROMOTED strategy's LIVE paper
equity curve vs SPY after tax over its forward window — this module is that
curve. Lifecycle, all computed from the lake's adjusted bars (never fabricated):

  event (PROMOTED study) ──▶ PENDING          (recorded the night it appears)
  PENDING ──▶ OPEN   at the NEXT session's open after event_ts, entry price
                     open × (1 + tier_RT_bps/2)   (taker crosses the spread)
          └─▶ SKIPPED when §7 limits reject it (max concurrent / sector /
                     correlation) or no fill appears within EXPIRY_SESSIONS
  OPEN    ──▶ CLOSED at the close of the study's primary-horizon session,
                     exit price close × (1 − tier_RT_bps/2)

Sizing (§7): min(vol-parity @15% target on 60d realized vol, 0.25×Kelly from
the study's OOS mean/dispersion, 7% NAV). NAV marks daily: 1 + Σ realized pnl
+ Σ open qty×(mark/entry − 1); benchmark = SPY total-return (SFP closeadj)
normalized to the book's first session. Pure functions here; the nightly feeds
bars and persists.
"""
from __future__ import annotations

import sqlite3
from bisect import bisect_right
from datetime import datetime, timezone

from ..harness import costs as hcosts
from . import limits, sizing

EXPIRY_SESSIONS = 5          # a pending with no fill within ~a week expires
DEFAULT_HORIZON = 21
_DAY_MS = 86_400_000
MAX_ENTRY_LAG_MS = 7 * _DAY_MS   # same adjacency honesty as the CAR evaluator


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()


def record_pending(conn: sqlite3.Connection, study: str, events: list[dict],
                   horizon: int = DEFAULT_HORIZON) -> int:
    """Insert PENDING rows for events (idempotent via the UNIQUE key)."""
    before = conn.execute("SELECT count(*) FROM paper_positions").fetchone()[0]
    conn.executemany(
        """INSERT OR IGNORE INTO paper_positions
           (study, ticker, event_ts, status, horizon_sessions, tier, sector)
           VALUES (?, ?, ?, 'PENDING', ?, ?, ?)""",
        [(study, e["ticker"], int(e["event_ts"]), horizon,
          e.get("tier"), e.get("sector")) for e in events if e.get("ticker")])
    conn.commit()
    return conn.execute("SELECT count(*) FROM paper_positions").fetchone()[0] - before


def _vol60(bars: list[dict], upto_idx: int) -> float | None:
    """Annualized 60-session realized vol ending at ``upto_idx`` (entry-time info)."""
    lo = max(1, upto_idx - 59)
    rets = [bars[i]["close"] / bars[i - 1]["close"] - 1.0
            for i in range(lo, upto_idx + 1) if bars[i - 1]["close"]]
    if len(rets) < 20:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return (var ** 0.5) * (252 ** 0.5)


def _mean_corr(bars_a: list[dict], others: list[list[dict]], end_ts: int) -> float | None:
    """Mean 60d return correlation of candidate vs open book (None if no basis)."""
    def rets(bars):
        ts = [b["ts"] for b in bars]
        j = bisect_right(ts, end_ts)
        window = bars[max(1, j - 60):j]
        return {b["ts"]: b["close"] / prev["close"] - 1.0
                for prev, b in zip(bars[max(0, j - 61):j - 1], window) if prev["close"]}
    ra = rets(bars_a)
    if len(ra) < 20 or not others:
        return None
    cors = []
    for ob in others:
        rb = rets(ob)
        common = sorted(set(ra) & set(rb))
        if len(common) < 20:
            continue
        xa = [ra[t] for t in common]
        xb = [rb[t] for t in common]
        ma, mb = sum(xa) / len(xa), sum(xb) / len(xb)
        num = sum((x - ma) * (y - mb) for x, y in zip(xa, xb))
        da = sum((x - ma) ** 2 for x in xa) ** 0.5
        db = sum((y - mb) ** 2 for y in xb) ** 0.5
        if da > 0 and db > 0:
            cors.append(num / (da * db))
    return (sum(cors) / len(cors)) if cors else None


def process(conn: sqlite3.Connection, study: str,
            bars_by_ticker: dict[str, list[dict]], *,
            expectancy: float | None = None, car_std: float | None = None,
            now_ms: int | None = None) -> dict:
    """Advance the book: fill PENDING, expire stale, close matured (pure w.r.t.
    market data — everything prices off the supplied adjusted bars)."""
    now_ms = now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
    stats = {"filled": 0, "skipped": 0, "closed": 0, "expired": 0}

    # --- pass 1: fill (or skip/expire) PENDING rows, oldest first -------------
    pendings = [dict(r) for r in conn.execute(
        "SELECT * FROM paper_positions WHERE study=? AND status='PENDING' "
        "ORDER BY event_ts", (study,)).fetchall()]
    open_rows = [dict(r) for r in conn.execute(
        "SELECT * FROM paper_positions WHERE study=? AND status='OPEN'",
        (study,)).fetchall()]

    for r in pendings:
        bars = bars_by_ticker.get(r["ticker"], [])
        ts_list = [b["ts"] for b in bars]
        i = bisect_right(ts_list, r["event_ts"])
        if i >= len(bars):
            # no post-event session yet; expire if the event is stale
            if now_ms - r["event_ts"] > (EXPIRY_SESSIONS + 3) * _DAY_MS:
                conn.execute("UPDATE paper_positions SET status='SKIPPED', "
                             "skip_reason='no_fill' WHERE id=?", (r["id"],))
                stats["expired"] += 1
            continue
        if bars[i]["ts"] - r["event_ts"] > MAX_ENTRY_LAG_MS:
            conn.execute("UPDATE paper_positions SET status='SKIPPED', "
                         "skip_reason='entry_gap' WHERE id=?", (r["id"],))
            stats["skipped"] += 1
            continue
        # §7 limits at the moment of fill
        book = [{"ticker": o["ticker"], "sector": o.get("sector")} for o in open_rows]
        corr = _mean_corr(bars, [bars_by_ticker.get(o["ticker"], [])
                                 for o in open_rows], bars[i]["ts"])
        viol = limits.check_candidate(book, {"ticker": r["ticker"],
                                             "sector": r.get("sector")},
                                      mean_corr_60d=corr)
        if viol:
            conn.execute("UPDATE paper_positions SET status='SKIPPED', "
                         "skip_reason=? WHERE id=?", ("; ".join(viol), r["id"]))
            stats["skipped"] += 1
            continue
        vol = _vol60(bars, i)
        qty = sizing.position_size(
            asset_vol_annual=vol or 0.0, n_concurrent=len(open_rows) + 1,
            expectancy=expectancy or 0.0,
            variance=(car_std ** 2) if car_std else 0.0)
        if qty <= 0:
            conn.execute("UPDATE paper_positions SET status='SKIPPED', "
                         "skip_reason='unsizeable' WHERE id=?", (r["id"],))
            stats["skipped"] += 1
            continue
        bps = hcosts.round_trip_bps(r.get("tier"))
        entry_px = bars[i]["open"] * (1 + bps / 2 / 10_000.0)
        conn.execute(
            "UPDATE paper_positions SET status='OPEN', qty=?, entry_ts=?, "
            "entry_px=? WHERE id=?", (qty, bars[i]["ts"], entry_px, r["id"]))
        r.update(status="OPEN", qty=qty, entry_ts=bars[i]["ts"], entry_px=entry_px)
        open_rows.append(r)
        stats["filled"] += 1

    # --- pass 2: close matured OPEN rows (incl. same-night backfills) ---------
    for r in [dict(x) for x in conn.execute(
            "SELECT * FROM paper_positions WHERE study=? AND status='OPEN'",
            (study,)).fetchall()]:
        bars = bars_by_ticker.get(r["ticker"], [])
        ts_list = [b["ts"] for b in bars]
        i = bisect_right(ts_list, r["entry_ts"]) - 1
        exit_i = i + (r["horizon_sessions"] or DEFAULT_HORIZON)
        if 0 <= exit_i < len(bars):
            bps = hcosts.round_trip_bps(r.get("tier"))
            exit_px = bars[exit_i]["close"] * (1 - bps / 2 / 10_000.0)
            conn.execute(
                "UPDATE paper_positions SET status='CLOSED', exit_ts=?, exit_px=? "
                "WHERE id=?", (bars[exit_i]["ts"], exit_px, r["id"]))
            stats["closed"] += 1
    conn.commit()
    return stats


def mark_nav(conn: sqlite3.Connection, study: str,
             bars_by_ticker: dict[str, list[dict]],
             bench_bars: list[dict]) -> int:
    """(Re)compute the daily NAV series from inception: 1 + realized + open marks.

    Recomputed wholesale each night (positions are few; determinism beats
    incrementalism). Benchmark = bench_bars closeadj normalized to the book's
    first session. Returns number of NAV rows written."""
    pos = [dict(r) for r in conn.execute(
        "SELECT * FROM paper_positions WHERE study=? AND status IN ('OPEN','CLOSED')",
        (study,)).fetchall()]
    if not pos:
        return 0
    start_ts = min(p["entry_ts"] for p in pos)
    dates = [b for b in bench_bars if b["ts"] >= start_ts]
    if not dates:
        return 0
    bench0 = dates[0]["close"]

    def px_at(ticker: str, ts: int) -> float | None:
        bars = bars_by_ticker.get(ticker, [])
        ts_list = [b["ts"] for b in bars]
        j = bisect_right(ts_list, ts) - 1
        return bars[j]["close"] if j >= 0 else None

    conn.execute("DELETE FROM paper_nav WHERE study=?", (study,))
    n = 0
    for b in dates:
        ts = b["ts"]
        nav = 1.0
        n_open = 0
        for p in pos:
            if p["entry_ts"] > ts or not p["entry_px"]:
                continue
            if p["status"] == "CLOSED" and p["exit_ts"] <= ts:
                nav += p["qty"] * (p["exit_px"] / p["entry_px"] - 1.0)
            else:
                mark = px_at(p["ticker"], ts)
                if mark:
                    nav += p["qty"] * (mark / p["entry_px"] - 1.0)
                    n_open += 1
        conn.execute(
            "INSERT OR REPLACE INTO paper_nav (study, date, nav, bench, n_open) "
            "VALUES (?, ?, ?, ?, ?)",
            (study, _iso(ts), nav, b["close"] / bench0, n_open))
        n += 1
    conn.commit()
    return n

"""Stage-0 paper book (§7 + meta-gate §9) — one book, three sources.

Every paper position the program holds lives in ``paper_positions``, namespaced
by ``source``. All three feeds share this accounting, this NAV engine and this
benchmark, so the dashboard can show ONE portfolio instead of three disjoint
forward-tests:

  'lab'      PROMOTED car-study events. Sized min(vol-parity, ¼Kelly, 7% NAV)
             from the study's OOS stats, limited under the original §7 constants
             against LAB POSITIONS ONLY. Exits on horizon.
  'swing'    Surfaced stock_collect picks. Carry a stop and a target.
  'longterm' Surfaced stock_lt_collect long-buys. Exits on horizon.

**Why lab is walled off.** The meta-gate judges the program on the promoted
study's LIVE paper curve vs SPY after tax. That verdict is only meaningful if
the curve is a function of the constants the study registered under — so adding
these new feeds must not change which lab fills happen. Lab keeps its own limit
budget, its own sizing rule, and its own '@lab' NAV series. '@combined' is the
whole-portfolio view and is explicitly NOT meta-gate evidence.

Lifecycle, all computed from the lake's adjusted bars (never fabricated):

  pick / event ──▶ PENDING          (recorded the night it appears)
  PENDING ──▶ OPEN   at the NEXT session's open after event_ts, entry price
                     open × (1 ± tier_RT_bps/2)   (taker crosses the spread)
          └─▶ SKIPPED when limits reject it (max concurrent / sector /
                     correlation) or no fill appears within EXPIRY_SESSIONS
  OPEN    ──▶ CLOSED on the first of: stop, target, horizon session close,
                     exit price × (1 ∓ tier_RT_bps/2)

Sizing honesty: a source with no validated OOS expectancy passes
``expectancy=None``, which DROPS the Kelly leg rather than zeroing it, and caps
at 2% NAV (``sizing`` module docstring). The basis is persisted per position.

Stop/target honesty: stops are carried as FRACTIONS of the pick's own entry and
rebased onto the book's fill price, because the pick is struck on an as-traded
basis while the book prices off dividend-adjusted bars. Intrabar conventions
match ``stock_positions.reprice``: a stop wins a same-bar stop+target tie, and a
bar that gaps through the stop fills at the open, not at the untouchable stop.

NAV marks daily: 1 + Σ realized pnl + Σ open qty×sign×(mark/entry − 1);
benchmark = SPY total-return (SFP closeadj) normalized to the book's first
session. Pure functions here; the nightly feeds bars and persists.
"""
from __future__ import annotations

import sqlite3
from bisect import bisect_right
from datetime import datetime, timezone

from ..harness import costs as hcosts
from ..harness import tax as htax
from . import limits, sizing

EXPIRY_SESSIONS = 5          # a pending with no fill within ~a week expires
DEFAULT_HORIZON = 21
_DAY_MS = 86_400_000
MAX_ENTRY_LAG_MS = 7 * _DAY_MS   # same adjacency honesty as the CAR evaluator

# Synthetic NAV keys. The '@' prefix cannot collide with a registered study name
# (study names are kebab-case identifiers), so these can share paper_nav.
NAV_LAB = "@lab"             # lab source only — THIS is the meta-gate curve
NAV_COMBINED = "@combined"   # the whole book — the portfolio view


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()


def _sign(direction: str | None) -> float:
    return -1.0 if (direction or "LONG").upper() == "SHORT" else 1.0


def record_pending(conn: sqlite3.Connection, study: str, events: list[dict],
                   horizon: int = DEFAULT_HORIZON, *,
                   source: str = "lab") -> int:
    """Insert PENDING rows for events/picks (idempotent via the UNIQUE key).

    Each event may carry ``direction`` ('LONG'/'SHORT'), and — for sources that
    manage their own risk — ``stop_frac``/``target_frac`` as SIGNED fractions of
    entry (a long's stop is negative). Absolute prices are deliberately not
    accepted: see the module docstring on basis drift."""
    before = conn.execute("SELECT count(*) FROM paper_positions").fetchone()[0]
    conn.executemany(
        """INSERT OR IGNORE INTO paper_positions
           (study, source, ticker, event_ts, direction, status,
            horizon_sessions, tier, sector, stop_frac, target_frac)
           VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?)""",
        [(study, source, e["ticker"], int(e["event_ts"]),
          (e.get("direction") or "LONG").upper(), horizon,
          e.get("tier"), e.get("sector"),
          e.get("stop_frac"), e.get("target_frac"))
         for e in events if e.get("ticker")])
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


def _resolve_exit(r: dict, bars: list[dict], start_i: int,
                  horizon: int) -> tuple[int, float, str] | None:
    """First exit among stop / target / horizon, scanning bars after entry.

    Returns ``(bar_index, raw_exit_price, reason)`` or None if the position is
    still live. Conventions match ``stock_positions.reprice`` exactly: the stop
    wins a same-bar stop+target tie (the pessimistic assumption), and a bar that
    OPENS through the stop books the achievable open rather than the stop price
    the tape never offered. Horizon is only reached if neither level trades."""
    long_ = _sign(r.get("direction")) > 0
    stop_px, target_px = r.get("stop_px"), r.get("target_px")
    last_i = start_i + horizon
    for i in range(start_i + 1, min(last_i, len(bars) - 1) + 1):
        b = bars[i]
        if stop_px is not None:
            hit = b["low"] <= stop_px if long_ else b["high"] >= stop_px
            if hit:
                opn = b.get("open")
                fill = (min(opn, stop_px) if long_ else max(opn, stop_px)) \
                    if opn is not None else stop_px
                return i, fill, "stop"
        if target_px is not None:
            hit = b["high"] >= target_px if long_ else b["low"] <= target_px
            if hit:
                return i, target_px, "target"
        if i >= last_i:
            return i, b["close"], "horizon"
    return None


def process(conn: sqlite3.Connection, study: str,
            bars_by_ticker: dict[str, list[dict]], *,
            expectancy: float | None = None, car_std: float | None = None,
            source: str = "lab", now_ms: int | None = None) -> dict:
    """Advance the book: fill PENDING, expire stale, close resolved (pure w.r.t.
    market data — everything prices off the supplied adjusted bars).

    ``expectancy``/``car_std`` None means "no validated OOS study" — the Kelly
    leg is dropped and the position sizes on vol-parity under the unvalidated
    cap. Limits are evaluated against this SOURCE's open positions under this
    source's budget, so feeds never crowd each other out (and lab's fills stay
    reproducible from the constants it registered under)."""
    now_ms = now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
    stats = {"filled": 0, "skipped": 0, "closed": 0, "expired": 0}
    budget = limits.limits_for(source)

    # --- pass 1: fill (or skip/expire) PENDING rows, oldest first -------------
    pendings = [dict(r) for r in conn.execute(
        "SELECT * FROM paper_positions WHERE study=? AND status='PENDING' "
        "ORDER BY event_ts", (study,)).fetchall()]
    # Limits see the whole SOURCE's open book, not just this study's — two
    # studies in one feed genuinely compete for the same concurrency budget.
    open_rows = [dict(r) for r in conn.execute(
        "SELECT * FROM paper_positions WHERE source=? AND status='OPEN'",
        (source,)).fetchall()]

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
        # limits at the moment of fill, under this source's budget
        book = [{"ticker": o["ticker"], "sector": o.get("sector")} for o in open_rows]
        corr = _mean_corr(bars, [bars_by_ticker.get(o["ticker"], [])
                                 for o in open_rows], bars[i]["ts"])
        viol = limits.check_candidate(book, {"ticker": r["ticker"],
                                             "sector": r.get("sector")},
                                      mean_corr_60d=corr,
                                      max_concurrent=budget["max_concurrent"],
                                      max_per_sector=budget["max_per_sector"])
        if viol:
            conn.execute("UPDATE paper_positions SET status='SKIPPED', "
                         "skip_reason=? WHERE id=?", ("; ".join(viol), r["id"]))
            stats["skipped"] += 1
            continue
        vol = _vol60(bars, i)
        qty, basis = sizing.position_size(
            asset_vol_annual=vol or 0.0, n_concurrent=len(open_rows) + 1,
            expectancy=expectancy,
            variance=(car_std ** 2) if car_std else None)
        if qty <= 0:
            conn.execute("UPDATE paper_positions SET status='SKIPPED', "
                         "skip_reason='unsizeable' WHERE id=?", (r["id"],))
            stats["skipped"] += 1
            continue
        sign = _sign(r.get("direction"))
        bps = hcosts.round_trip_bps(r.get("tier"))
        half = bps / 2 / 10_000.0
        # Taker crosses the spread in the direction of the trade: a long lifts
        # the offer, a short hits the bid.
        entry_px = bars[i]["open"] * (1 + sign * half)
        # Rebase the pick's stop/target fractions onto the fill actually got.
        stop_px = (entry_px * (1 + r["stop_frac"])
                   if r.get("stop_frac") is not None else None)
        target_px = (entry_px * (1 + r["target_frac"])
                     if r.get("target_frac") is not None else None)
        conn.execute(
            "UPDATE paper_positions SET status='OPEN', qty=?, sizing_basis=?, "
            "entry_ts=?, entry_px=?, stop_px=?, target_px=? WHERE id=?",
            (qty, basis, bars[i]["ts"], entry_px, stop_px, target_px, r["id"]))
        r.update(status="OPEN", qty=qty, entry_ts=bars[i]["ts"],
                 entry_px=entry_px, stop_px=stop_px, target_px=target_px)
        open_rows.append(r)
        stats["filled"] += 1

    # --- pass 2: close resolved OPEN rows (incl. same-night backfills) --------
    for r in [dict(x) for x in conn.execute(
            "SELECT * FROM paper_positions WHERE study=? AND status='OPEN'",
            (study,)).fetchall()]:
        bars = bars_by_ticker.get(r["ticker"], [])
        ts_list = [b["ts"] for b in bars]
        i = bisect_right(ts_list, r["entry_ts"]) - 1
        if i < 0:
            continue
        hit = _resolve_exit(r, bars, i, r["horizon_sessions"] or DEFAULT_HORIZON)
        if hit:
            exit_i, raw_px, reason = hit
            sign = _sign(r.get("direction"))
            bps = hcosts.round_trip_bps(r.get("tier"))
            exit_px = raw_px * (1 - sign * bps / 2 / 10_000.0)
            conn.execute(
                "UPDATE paper_positions SET status='CLOSED', exit_ts=?, "
                "exit_px=?, exit_reason=? WHERE id=?",
                (bars[exit_i]["ts"], exit_px, reason, r["id"]))
            stats["closed"] += 1
    conn.commit()
    return stats


def _mark(conn: sqlite3.Connection, key: str, pos: list[dict],
          bars_by_ticker: dict[str, list[dict]],
          bench_bars: list[dict]) -> int:
    """Write one daily NAV series for an arbitrary set of positions under ``key``."""
    pos = [p for p in pos if p.get("entry_ts") and p.get("entry_px")]
    if not pos:
        conn.execute("DELETE FROM paper_nav WHERE study=?", (key,))
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

    conn.execute("DELETE FROM paper_nav WHERE study=?", (key,))
    n = 0
    for b in dates:
        ts = b["ts"]
        nav = 1.0
        nav_at = 1.0    # after-tax: realized legs taxed (meta-gate judges after tax)
        n_open = 0
        for p in pos:
            if p["entry_ts"] > ts:
                continue
            sign = _sign(p.get("direction"))
            if p["status"] == "CLOSED" and p["exit_ts"] <= ts:
                pnl = p["qty"] * sign * (p["exit_px"] / p["entry_px"] - 1.0)
                nav += pnl
                # Every Stage-0 hold is <= its horizon (~21 sessions) — short-term
                # rate; unrealized marks stay untaxed until they close.
                nav_at += htax.after_tax(pnl)
            else:
                mark = px_at(p["ticker"], ts)
                if mark:
                    unreal = p["qty"] * sign * (mark / p["entry_px"] - 1.0)
                    nav += unreal
                    nav_at += unreal
                    n_open += 1
        conn.execute(
            "INSERT OR REPLACE INTO paper_nav "
            "(study, date, nav, nav_after_tax, bench, n_open) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, _iso(ts), nav, nav_at, b["close"] / bench0, n_open))
        n += 1
    conn.commit()
    return n


def mark_nav(conn: sqlite3.Connection, study: str,
             bars_by_ticker: dict[str, list[dict]],
             bench_bars: list[dict]) -> int:
    """(Re)compute one study's daily NAV series from inception.

    Recomputed wholesale each night (positions are few; determinism beats
    incrementalism). Benchmark = bench_bars closeadj normalized to the book's
    first session. Returns number of NAV rows written."""
    pos = [dict(r) for r in conn.execute(
        "SELECT * FROM paper_positions WHERE study=? AND status IN ('OPEN','CLOSED')",
        (study,)).fetchall()]
    return _mark(conn, study, pos, bars_by_ticker, bench_bars)


def mark_rollups(conn: sqlite3.Connection,
                 bars_by_ticker: dict[str, list[dict]],
                 bench_bars: list[dict]) -> dict[str, int]:
    """Recompute the two synthetic curves.

    '@lab' is lab-source positions only and IS the meta-gate evidence — it is
    computed from the same rows, under the same constants, as before this table
    gained other feeds. '@combined' is every source and is the portfolio view
    only; it must never be quoted as the meta-gate, because most of its
    positions were never sized on a validated edge."""
    all_pos = [dict(r) for r in conn.execute(
        "SELECT * FROM paper_positions WHERE status IN ('OPEN','CLOSED')").fetchall()]
    lab_pos = [p for p in all_pos if (p.get("source") or "lab") == "lab"]
    return {
        NAV_LAB: _mark(conn, NAV_LAB, lab_pos, bars_by_ticker, bench_bars),
        NAV_COMBINED: _mark(conn, NAV_COMBINED, all_pos, bars_by_ticker, bench_bars),
    }

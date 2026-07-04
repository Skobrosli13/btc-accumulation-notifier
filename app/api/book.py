"""/api/book — the Stage-0 paper book's detail surface (redesign P4).

Serves the meta-gate evidence verbatim: every position the book opened,
skipped (with the recorded reason — the honest record), or closed, plus the
daily NAV-vs-SPY series. No derived judgement here; the page renders rows.
Owner gating happens at the PAGE (X-Auth-User) — positions are PROMOTED-study
output (directive 6); this API stays token-internal.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from ..api_deps import conn_ro as _conn
from ..api_deps import get_config, require_token
from ..config import Config

router = APIRouter()


def _rows(conn, sql: str, params=()) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []


@router.get("/api/book")
def book(cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    conn = _conn(cfg)
    try:
        positions = _rows(conn,
                          "SELECT study, ticker, event_ts, qty, entry_ts, entry_px, "
                          "exit_ts, exit_px, status, skip_reason, horizon_sessions, "
                          "tier, sector FROM paper_positions ORDER BY event_ts DESC "
                          "LIMIT 200")
        nav = _rows(conn, "SELECT study, date, nav, bench, n_open FROM paper_nav "
                          "ORDER BY date ASC")
    finally:
        conn.close()
    for p in positions:
        if p["status"] == "CLOSED" and p.get("entry_px") and p.get("exit_px"):
            sign = 1.0  # book is long-only at Stage 0
            p["ret_pct"] = round(sign * (p["exit_px"] / p["entry_px"] - 1.0) * 100, 2)
    counts: dict[str, int] = {}
    for p in positions:
        counts[p["status"]] = counts.get(p["status"], 0) + 1
    return {"positions": positions, "nav": nav, "counts": counts,
            "note": ("Stage-0 paper book — PROMOTED-study events filed after "
                     "registration only, next-open fills with costs, "
                     "pre-registered sizing/limits. Nothing is backfilled; "
                     "skips are recorded, not hidden. This curve IS the "
                     "meta-gate evidence (vs SPY total return).")}

"""/api/today — the decision-first home's aggregation (redesign §3, P2).

Answers "do I need to act today?" in one payload. The digest email renders the
SAME aggregation (Gap D: one source, page and digest can never disagree):

  * act — rows since the previous business day 00:00 ET, verdict-labeled:
    new PROMOTED-study events, BTC tier changes, trend-policy flips.
  * testing — one line per active study (status + what decides next).
  * paper — the Stage-0 book's latest NAV vs SPY + open/pending counts.

The aggregation is a plain function over a read connection so the digest
script imports it directly; the router is a thin wrapper. Owner gating of the
act rows happens at the PAGE (X-Auth-User) — this API stays token-internal.
"""
from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends

from .. import schedule as sched
from .. import store
from ..api_deps import conn_ro as _conn
from ..api_deps import get_config, require_token
from ..config import Config
from ..policies import btc as pol

router = APIRouter()


def _rows(conn, sql: str, params=()) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []


def aggregate_today(conn) -> dict:
    """The Today payload from one read connection (pure aside from reads)."""
    window_ms = sched.act_window_start_ms()
    act: list[dict] = []

    # --- new events from PROMOTED studies (the live picks) -------------------
    promoted = [r["name"] for r in _rows(
        conn, "SELECT name FROM studies WHERE status='PROMOTED' AND tier='alpha'")]
    for study in promoted:
        for e in _rows(conn,
                       "SELECT ticker, event_ts, direction, strength, tier, sector, meta "
                       "FROM events WHERE study=? AND event_ts >= ? "
                       "ORDER BY event_ts DESC LIMIT 20", (study, window_ms)):
            try:
                meta = json.loads(e.get("meta") or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            act.append({"kind": "event", "study": study, "ticker": e["ticker"],
                        "direction": e["direction"], "ts": e["event_ts"],
                        "tier": e.get("tier"), "sector": e.get("sector"),
                        "detail": f"{meta.get('n_managers') or len(meta.get('owners', []))} "
                                  f"insider(s), ${round((meta.get('agg_usd') or 0) / 1000)}k"
                                  if meta else "",
                        "label": "PROMOTED"})

    # --- BTC tier change since the window ------------------------------------
    runs = _rows(conn, "SELECT run_ts, tier FROM runs ORDER BY run_ts DESC LIMIT 12")
    if len(runs) >= 2 and runs[0]["tier"] != runs[1]["tier"]:
        act.append({"kind": "btc_tier", "ticker": "BTC",
                    "detail": f"{runs[1]['tier']} → {runs[0]['tier']}",
                    "ts": None, "label": "TIER CHANGE"})

    # --- trend-policy flip within the window ----------------------------------
    candles = store.candles_since(conn, "1d")
    candles = candles[:-1] if len(candles) > 1 else candles
    closes = [c["close"] for c in candles]
    exposure = pol.trend_exposure(closes)
    flip = None
    for i in range(len(exposure) - 1, 0, -1):
        if exposure[i] != exposure[i - 1]:
            if candles[i]["ts"] >= window_ms:
                flip = {"kind": "trend_flip", "ticker": "BTC",
                        "detail": ("FLAT → LONG" if exposure[i] >= 1.0 else "LONG → FLAT"),
                        "ts": candles[i]["ts"], "label": "POLICY"}
            break
    if flip:
        act.append(flip)

    # --- testing strip ---------------------------------------------------------
    testing = [{"name": r["name"], "status": r["status"], "tier": r["tier"]}
               for r in _rows(conn, "SELECT name, status, tier FROM studies "
                                    "ORDER BY registered_at")]

    # --- paper book ------------------------------------------------------------
    nav = _rows(conn, "SELECT * FROM paper_nav ORDER BY date DESC LIMIT 1")
    counts = {r["status"]: r["n"] for r in _rows(
        conn, "SELECT status, count(*) n FROM paper_positions GROUP BY status")}
    paper = {"nav": nav[0]["nav"] if nav else None,
             "bench": nav[0]["bench"] if nav else None,
             "date": nav[0]["date"] if nav else None,
             "open": counts.get("OPEN", 0), "pending": counts.get("PENDING", 0),
             "closed": counts.get("CLOSED", 0)}

    lab_sync_row = _rows(conn, "SELECT value FROM lab_meta WHERE key='last_sync'")
    return {"window_start_ms": window_ms,
            "act": act,
            "testing": testing,
            "paper": paper,
            "lab_sync": sched.lab_sync_state(
                lab_sync_row[0]["value"] if lab_sync_row else None),
            "note": ("Act rows are verdict-labeled and share the digest's "
                     "window (previous business day 00:00 ET). Nothing here is "
                     "advice; the owner decides.")}


@router.get("/api/today")
def today(cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    conn = _conn(cfg)
    try:
        return aggregate_today(conn)
    finally:
        conn.close()

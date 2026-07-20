"""/api/book — the Stage-0 paper book: one portfolio, three sources.

Serves the book's evidence verbatim: every position it opened, skipped (with
the recorded reason — the honest record), or closed, plus the daily NAV-vs-SPY
series. No derived judgement here; the page renders rows.

Two curves ship, and they are NOT interchangeable:
  '@lab'      lab-source positions only — sized on validated OOS expectancy
              under the constants their studies registered with. THIS is the
              meta-gate evidence (§9).
  '@combined' the whole book including swing/long-term picks, which have no
              validated edge and are sized on vol-parity alone. A portfolio
              view, never an edge claim.

Owner gating happens at the PAGE (X-Auth-User) — positions are strategy output
(directive 6); this API stays token-internal.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from ..api_deps import conn_ro as _conn
from ..api_deps import get_config, require_token
from ..config import Config
from ..portfolio import book as pbook

router = APIRouter()

# Source -> how the dashboard must frame it. Kept here (not in the page) so the
# API and the UI can never disagree about what is edge and what is forward-test.
SOURCE_META = {
    "lab": {"label": "Lab studies",
            "basis": "validated",
            "blurb": "Events from a PROMOTED pre-registered study, sized on its "
                     "out-of-sample expectancy. This is the only source whose "
                     "curve is meta-gate evidence."},
    "swing": {"label": "Swing picks",
              "basis": "forward-test",
              "blurb": "Surfaced short-term setups. No validated edge — sized on "
                       "volatility parity under a 2% cap, exits on stop/target."},
    "longterm": {"label": "Long buys",
                 "basis": "forward-test",
                 "blurb": "Surfaced QVM accumulation names. No validated edge — "
                          "sized on volatility parity, quarterly horizon exit."},
}


def _rows(conn, sql: str, params=()) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []


def _curve_summary(curve: list[dict]) -> dict | None:
    """Total return, after-tax return and benchmark over the curve's window."""
    if not curve:
        return None
    last = curve[-1]
    return {
        "days": len(curve),
        "start": curve[0]["date"],
        "end": last["date"],
        "nav": last.get("nav"),
        "nav_after_tax": last.get("nav_after_tax"),
        "bench": last.get("bench"),
        # Excess is measured after tax vs SPY total return — the meta-gate's
        # own definition. Positive does NOT mean the edge is established; the
        # study verdict does that, not the curve.
        "excess_after_tax": (
            round(last["nav_after_tax"] - last["bench"], 4)
            if last.get("nav_after_tax") is not None and last.get("bench") is not None
            else None),
        "n_open": last.get("n_open"),
    }


@router.get("/api/book")
def book(cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    conn = _conn(cfg)
    try:
        positions = _rows(conn,
                          "SELECT study, source, ticker, event_ts, direction, qty, "
                          "sizing_basis, entry_ts, entry_px, exit_ts, exit_px, "
                          "exit_reason, stop_px, target_px, status, skip_reason, "
                          "horizon_sessions, tier, sector FROM paper_positions "
                          "ORDER BY event_ts DESC LIMIT 400")
        nav = _rows(conn, "SELECT study, date, nav, nav_after_tax, bench, n_open "
                          "FROM paper_nav ORDER BY date ASC")
    finally:
        conn.close()

    for p in positions:
        p.setdefault("source", "lab")
        p.setdefault("direction", "LONG")
        if p["status"] == "CLOSED" and p.get("entry_px") and p.get("exit_px"):
            sign = -1.0 if (p.get("direction") or "LONG") == "SHORT" else 1.0
            p["ret_pct"] = round(sign * (p["exit_px"] / p["entry_px"] - 1.0) * 100, 2)

    curves: dict[str, list[dict]] = {}
    for r in nav:
        curves.setdefault(r["study"], []).append(
            {k: r[k] for k in ("date", "nav", "nav_after_tax", "bench", "n_open")})

    # Per-source rollup of what the book is actually doing.
    by_source: dict[str, dict] = {}
    for p in positions:
        s = by_source.setdefault(p["source"], {
            "source": p["source"], **SOURCE_META.get(p["source"], {}),
            "counts": {}, "namespaces": set(), "closed_rets": []})
        s["counts"][p["status"]] = s["counts"].get(p["status"], 0) + 1
        s["namespaces"].add(p["study"])
        if p.get("ret_pct") is not None:
            s["closed_rets"].append(p["ret_pct"])
    for s in by_source.values():
        rets = s.pop("closed_rets")
        s["namespaces"] = sorted(s["namespaces"])
        s["n_closed"] = len(rets)
        # Mean closed return is descriptive only — n is small, the episodes
        # overlap, and no CI is computed here. The dashboard must render it as
        # a record, never as an expectancy.
        s["mean_closed_ret_pct"] = round(sum(rets) / len(rets), 2) if rets else None

    counts: dict[str, int] = {}
    for p in positions:
        counts[p["status"]] = counts.get(p["status"], 0) + 1

    return {
        "positions": positions,
        "curves": curves,
        "summary": {
            "combined": _curve_summary(curves.get(pbook.NAV_COMBINED, [])),
            "lab": _curve_summary(curves.get(pbook.NAV_LAB, [])),
        },
        "by_source": sorted(by_source.values(), key=lambda s: s["source"]),
        "counts": counts,
        "note": ("Stage-0 paper book — picks filed after they surfaced only, "
                 "next-open fills with costs, pre-registered sizing/limits. "
                 "Nothing is backfilled; skips are recorded, not hidden. The "
                 "'lab' curve alone is meta-gate evidence (vs SPY total return "
                 "after tax); swing and long-term picks are forward-test "
                 "positions with no validated edge."),
    }

"""/api/studies — the research lab's verdict surface (M5 §8).

Read-only view over the harness tables: every registered study with its status
(the ONLY source of UI labels — nothing hardcoded downstream), tier, per-segment
results incl. uncertainty fields (n_events / n_months / t), and the placebo
row. Absent tables (fresh box) serve an empty list, never a 500.
"""
from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends

from ..api_deps import conn_ro as _conn
from ..api_deps import get_config, require_token
from ..config import Config

router = APIRouter()


def _rows(conn, sql: str, params=()) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        return []          # harness tables not created yet on this box


@router.get("/api/studies/events")
def recent_events(study: str = "insider_cluster", days: int = 14,
                  cfg: Config = Depends(get_config),
                  _=Depends(require_token)) -> dict:
    """Recent live events for one study — the dashboard's 'Lab signals' feed.

    Serves events with the study's CURRENT verdict + gate stats attached so the
    UI can label them honestly (a KILLED study's events render as recording,
    never as picks). Events arrive via the nightly emit+sync."""
    import time
    days = max(1, min(int(days), 90))
    since = int((time.time() - days * 86_400) * 1000)
    from .. import schedule as sched
    conn = _conn(cfg)
    try:
        study_row = _rows(conn, "SELECT * FROM studies WHERE name = ?", (study,))
        evs = _rows(conn,
                    "SELECT ticker, event_ts, direction, strength, tier, sector, "
                    "days_since_earnings, meta FROM events "
                    "WHERE study = ? AND event_ts >= ? ORDER BY event_ts DESC "
                    "LIMIT 100", (study, since))
        # The study's primary-horizon OOS row = the honest stats badge.
        stats = _rows(conn,
                      "SELECT * FROM study_results WHERE study = ? AND "
                      "segment = 'OOS' AND horizon = "
                      "(SELECT primary_horizon FROM studies WHERE name = ?)",
                      (study, study))
        # §10 staleness: the events arrive via the laptop nightly — the card
        # must know how fresh the feed is (Gap C guard keys off `overdue`).
        sync_row = _rows(conn, "SELECT value FROM lab_meta WHERE key='last_sync'")
        lab_sync = sched.lab_sync_state(sync_row[0]["value"] if sync_row else None)
    finally:
        conn.close()
    for e in evs:
        try:
            e["meta"] = json.loads(e.get("meta") or "{}")
        except (json.JSONDecodeError, TypeError):
            e["meta"] = {}
    s = study_row[0] if study_row else None
    st = stats[0] if stats else None
    return {"study": study,
            "status": s["status"] if s else None,
            "primary_horizon": s["primary_horizon"] if s else None,
            "gate_stats": ({"t_clustered": st["t_clustered"],
                            "n_events": st["n_events"],
                            "n_months": st["n_months"],
                            "exp_after_tax": st["exp_after_tax"],
                            "win_rate": st["win_rate"]} if st else None),
            "days": days, "events": evs,
            "lab_sync": lab_sync,
            "note": ("Events from a pre-registered study; the status/gate stats "
                     "are the label. LIVE forward evidence accrues from these — "
                     "not investment advice.")}


@router.get("/api/studies")
def studies(cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    conn = _conn(cfg)
    try:
        studs = _rows(conn, "SELECT * FROM studies ORDER BY registered_at")
        results = _rows(conn, "SELECT * FROM study_results ORDER BY study, segment, horizon")
        counts = {r["study"]: r["n"] for r in _rows(
            conn, "SELECT study, count(*) AS n FROM events GROUP BY study")}
    finally:
        conn.close()
    by_study: dict[str, list[dict]] = {}
    for r in results:
        try:
            r["extra"] = json.loads(r.pop("extra_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            r["extra"] = {}
        by_study.setdefault(r["study"], []).append(r)
    out = []
    for s in studs:
        rows = by_study.get(s["name"], [])
        out.append({**s,
                    "n_events_emitted": counts.get(s["name"], 0),
                    "results": [r for r in rows if r["segment"] != "PLACEBO"],
                    "placebo": next((r for r in rows if r["segment"] == "PLACEBO"),
                                    None)})
    return {"studies": out,
            "note": ("Statuses are machine verdicts from pre-registered gates "
                     "(§5.5); IS results are exploration, OOS is the scored "
                     "window, LIVE is the only uncontaminated evidence. "
                     "Personal research tool — not investment advice.")}

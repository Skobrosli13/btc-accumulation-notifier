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

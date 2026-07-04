"""/api/health — DB freshness (per-pipeline cadence) + per-indicator availability.

The config ``layers`` only say what is CONFIGURED; a scored source that fails
soft to None forever (a dead upstream) is otherwise invisible because the
composite renormalizes around it with no alert — so this also reports each
scored indicator's recent availability from the persisted run readings.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from .. import schedule as sched
from .. import scoring, store
from ..api_deps import get_config, require_token
from ..config import Config

router = APIRouter()


def _lab_sync_state(conn) -> dict:
    """Freshness of the laptop-run lab sync from the lab_meta marker."""
    try:
        row = conn.execute(
            "SELECT value FROM lab_meta WHERE key='last_sync'").fetchone()
        return sched.lab_sync_state(row[0] if row else None)
    except sqlite3.Error:
        return sched.lab_sync_state(None)

# Long-term runs are a 6h cron; allow ~2 cadences + slack before "stale" so a
# single missed run isn't flagged, but a dead run_once (which the 10-min collector
# would otherwise mask) is caught.
_RUN_STALE_HOURS = 13.0

# How many recent runs (~2 days at the 6h cadence) the per-indicator availability
# check inspects: an indicator None across all of them is reported dark.
_INDICATOR_HEALTH_RUNS = 8


@router.get("/api/health")
def health(cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    now = datetime.now(timezone.utc)
    out = {
        "ok": True,
        "now": now.isoformat(),
        "exchange": cfg.exchange,
        "symbol": cfg.symbol,
        "timeframes": list(cfg.st_timeframes),
        "layers": {
            "onchain": cfg.onchain_active,
            "macro": cfg.macro_active,
            "derivs_paid": cfg.derivs_paid_active,
            "flow": cfg.coinalyze_active,   # free Coinalyze order-flow layer (CVD/OI/liq)
            "email": cfg.email_active,
        },
        "onchain_source": cfg.onchain_source,
        "db_ok": False,
        "last_collect": None,
        "last_run": None,
        "collect_age_hours": None,
        "run_age_hours": None,
        # Default to stale=True so a DB error never renders as "healthy".
        "collect_stale": True,
        "run_stale": True,
        "stale": True,
    }
    # §10 freshness: NEXT attempts from the cron grid (never from the data),
    # lab sync self-calibrating from its marker.
    out["schedule"] = {**sched.btc_schedule(), "lab": sched.lab_sync_state(None)}
    try:
        conn = store.connect_readonly(cfg.db_path)
        lc = store.last_collect_ts(conn)
        lr = store.last_run_ts(conn)
        recent_reads = store.recent_run_readings(conn, _INDICATOR_HEALTH_RUNS)
        out["schedule"]["lab"] = _lab_sync_state(conn)
        conn.close()
        inds = {}
        for name in scoring.THRESHOLDS:
            seen = [r["run_ts"] for r in recent_reads
                    if scoring._finite(r["raw"].get(name)) is not None]
            inds[name] = {
                # data present in the LATEST run
                "available": bool(recent_reads and seen
                                  and seen[0] == recent_reads[0]["run_ts"]),
                "runs_with_data": len(seen),
                "runs_checked": len(recent_reads),
                "last_seen": seen[0] if seen else None,
            }
        out["indicators"] = inds
        # Scored indicators dark across every checked run — a likely dead source,
        # not a one-run blip (empty until at least one run exists).
        out["dark_indicators"] = (sorted(n for n, v in inds.items()
                                         if v["runs_with_data"] == 0)
                                  if recent_reads else [])
        out["db_ok"] = True
        out["last_collect"] = lc.isoformat() if lc else None
        out["last_run"] = lr.isoformat() if lr else None
        collect_age = (now - lc).total_seconds() / 3600.0 if lc else None
        run_age = (now - lr).total_seconds() / 3600.0 if lr else None
        out["collect_age_hours"] = round(collect_age, 2) if collect_age is not None else None
        out["run_age_hours"] = round(run_age, 2) if run_age is not None else None
        # Each pipeline is judged against its OWN cadence: the fresh 10-min collector
        # must not hide a dead 6h long-term run (and vice versa).
        out["collect_stale"] = collect_age is None or collect_age > cfg.watchdog_stale_hours
        out["run_stale"] = run_age is None or run_age > _RUN_STALE_HOURS
        out["stale"] = out["collect_stale"] or out["run_stale"]
    except sqlite3.Error as exc:
        out["ok"] = False
        out["error"] = str(exc)
    return out

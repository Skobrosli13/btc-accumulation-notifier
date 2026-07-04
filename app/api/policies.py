"""/api/policies/state — the live state of the PROMOTED BTC policies (P1 §3).

The lab certified two BTC discipline overlays; the BTC page must show their
CURRENT prescriptions first-class, labels from the studies table verbatim:

  * btc_trend_policy — LONG or FLAT from the 200-DMA ±2% hysteresis over the
    stored daily closes (the exact `policies.btc.trend_exposure` the study
    gated). Cold-start honesty: with no band-cross inside the stored window
    the hysteresis state is inherited from the cold-start default — flagged
    ``warming_up`` so the UI can say so instead of overclaiming.
  * btc_accum_policy — this period's DCA spend multiplier from the latest LT
    tier (NEUTRAL 0.75 / WATCH 1.0 / ACCUMULATE 1.5 / DEEP_VALUE 2.0).
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from .. import store
from ..api_deps import conn_ro as _conn
from ..api_deps import get_config, require_token
from ..config import Config
from ..policies import btc as pol

router = APIRouter()


def _study_status(conn, name: str) -> str | None:
    try:
        row = conn.execute("SELECT status FROM studies WHERE name=?", (name,)).fetchone()
        return row["status"] if row else None
    except sqlite3.Error:
        return None


def _gate_stats(conn, name: str, segment: str) -> dict | None:
    """One-line gate numbers for a policy chip (§3: 'PROMOTED chip and one-line
    gate stats') — the study_results row's overlay-vs-baseline evidence, served
    verbatim so the chip is never a bare claim."""
    import json as _json
    try:
        row = conn.execute(
            "SELECT n_events, extra_json FROM study_results "
            "WHERE study=? AND segment=? ORDER BY computed_at DESC LIMIT 1",
            (name, segment)).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        extra = _json.loads(row["extra_json"] or "{}")
    except (ValueError, TypeError):
        extra = {}
    if extra.get("overlay_return") is None:
        return None
    return {"segment": segment, "n_days": row["n_events"],
            "overlay_return": extra.get("overlay_return"),
            "baseline_return": extra.get("baseline_return"),
            "overlay_maxdd": extra.get("overlay_maxdd"),
            "baseline_maxdd": extra.get("baseline_maxdd")}


@router.get("/api/policies/state")
def policies_state(cfg: Config = Depends(get_config),
                   _=Depends(require_token)) -> dict:
    conn = _conn(cfg)
    try:
        candles = store.candles_since(conn, "1d")
        candles = candles[:-1] if len(candles) > 1 else candles   # closed only
        latest = store.latest_run(conn)
        trend_status = _study_status(conn, "btc_trend_policy")
        accum_status = _study_status(conn, "btc_accum_policy")
        # OOS for trend (event-split study), BACKTEST for accum (a DCA curve
        # can't be split at a boundary — see harness.schema segment notes).
        trend_gate = _gate_stats(conn, "btc_trend_policy", "OOS")
        accum_gate = _gate_stats(conn, "btc_accum_policy", "BACKTEST")
    finally:
        conn.close()

    closes = [c["close"] for c in candles]
    exposure = pol.trend_exposure(closes)
    state = exposure[-1] if exposure else 0.0
    # warming_up: no band cross observed in-window means the hysteresis state
    # is the cold-start default, not an observed regime decision.
    crossed = any(exposure[i] != exposure[i - 1] for i in range(1, len(exposure)))
    warming = (len(closes) < pol.TREND_PERIOD + 5) or (not crossed and state == 0.0)

    tier = (latest or {}).get("tier")
    scale = pol.accum_scales([tier])[0] if tier else 1.0

    return {
        "trend": {"status": trend_status, "state": "LONG" if state >= 1.0 else "FLAT",
                  "warming_up": warming, "period": pol.TREND_PERIOD,
                  "band_pct": pol.TREND_BAND * 100, "n_closes": len(closes),
                  "gate_stats": trend_gate},
        "accum": {"status": accum_status, "tier": tier, "dca_multiplier": scale,
                  "gate_stats": accum_gate},
        "note": ("Discipline overlays — not alpha (POLICY tier). Trend manages "
                 "the HELD stack; accumulation tilts NEW capital. Statuses are "
                 "machine verdicts; see /lab."),
    }

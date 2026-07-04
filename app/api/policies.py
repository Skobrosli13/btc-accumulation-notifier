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
                  "band_pct": pol.TREND_BAND * 100, "n_closes": len(closes)},
        "accum": {"status": accum_status, "tier": tier, "dca_multiplier": scale},
        "note": ("Discipline overlays — not alpha (POLICY tier). Trend manages "
                 "the HELD stack; accumulation tilts NEW capital. Statuses are "
                 "machine verdicts; see /lab."),
    }

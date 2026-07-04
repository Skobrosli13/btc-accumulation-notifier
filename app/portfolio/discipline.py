"""Discipline ledger (§7) — the system-vs-executed record.

The app PROPOSES; the owner executes at the brokerage and confirms one-tap:
Executed (fill px optional) or Skipped (reason REQUIRED). Rows land in the
harness `decisions` table; the R-gap report quantifies what deviation cost.
A deviation without a reason code is refused — that is the whole point.
"""
from __future__ import annotations

import sqlite3
import time

REASON_CODES = ("followed", "liquidity", "conviction", "risk_off", "error",
                "unavailable", "other")


def record_decision(conn: sqlite3.Connection, *, event_id: int,
                    system_action: str, user_action: str,
                    reason_code: str | None = None,
                    ts: int | None = None) -> None:
    """Append one decision. ``user_action`` != ``system_action`` REQUIRES a
    reason code from REASON_CODES (raises ValueError otherwise)."""
    deviated = user_action != system_action
    if deviated and reason_code not in REASON_CODES:
        raise ValueError(
            f"deviation ({system_action} -> {user_action}) requires a reason "
            f"code from {REASON_CODES}")
    conn.execute(
        "INSERT INTO decisions (event_id, system_action, user_action, "
        "reason_code, ts) VALUES (?, ?, ?, ?, ?)",
        (event_id, system_action, user_action,
         reason_code if deviated else (reason_code or "followed"),
         ts if ts is not None else int(time.time() * 1000)))
    conn.commit()


def r_gap(pairs: list[dict]) -> dict:
    """System-vs-executed R gap over decision-outcome pairs (pure).

    ``pairs``: [{system_r, executed_r}] — system_r is what the proposed trade
    realized; executed_r what the owner's actual action realized (0.0 for a
    skip). Returns {n, followed, deviated, mean_system_r, mean_executed_r,
    gap_r} where gap_r = executed − system (negative = deviation cost money)."""
    if not pairs:
        return {"n": 0, "followed": 0, "deviated": 0, "mean_system_r": None,
                "mean_executed_r": None, "gap_r": None}
    sys_r = [float(p.get("system_r") or 0.0) for p in pairs]
    exe_r = [float(p.get("executed_r") or 0.0) for p in pairs]
    deviated = sum(1 for p in pairs
                   if p.get("system_r") is not None
                   and p.get("executed_r") != p.get("system_r"))
    ms, me = sum(sys_r) / len(sys_r), sum(exe_r) / len(exe_r)
    return {"n": len(pairs), "followed": len(pairs) - deviated,
            "deviated": deviated, "mean_system_r": ms,
            "mean_executed_r": me, "gap_r": me - ms}

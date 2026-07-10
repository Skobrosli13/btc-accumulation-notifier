"""/api/today — the decision-first home's aggregation (redesign §3, P2).

Answers "do I need to act today?" in one payload. The digest email renders the
SAME aggregation (Gap D: one source, page and digest can never disagree):

  * act — rows since the previous business day 00:00 ET, verdict-labeled:
    new PROMOTED-study events, BTC tier changes, trend-policy flips, and
    froth band escalations (§3's four actionable kinds). Every comparison is
    against the last state BEFORE the window opened, not merely the previous
    run — a change 12h old is still news if it happened inside the window.
    Study events window on ARRIVAL (ingested_at), not event_ts — see the
    query comment in aggregate_today for why.
  * testing — one line per study: status verbatim + the concrete next
    decision date (monthly review = 1st of next month).
  * paper — the Stage-0 book's latest NAV (pre- and after-tax) vs SPY.
  * health — the digest's one-line health summary (§4).

Gap C: when the lab sync is overdue, event rows carry ``stale: true`` so the
page and the digest DEMOTE them from actionable picks to recording.

The aggregation is a plain function over a read connection so the digest
script imports it directly; the router is a thin wrapper. Owner gating of the
act rows happens at the PAGE (X-Auth-User) — this API stays token-internal.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from .. import schedule as sched
from .. import scoring, store
from ..api_deps import conn_ro as _conn
from ..api_deps import get_config, require_token
from ..config import Config
from ..policies import btc as pol
from .health import _RUN_STALE_HOURS

router = APIRouter()

_BAND_ORDER = [name for name, _ in scoring.FROTH_BANDS]
_DAY_MS = 86_400_000
# Monthly review (Task Scheduler, 1st 04:00): the concrete "what decides next"
# per status — §3 Today item 3 wants a date, not a generic phrase.
_NEXT_DECISION = {
    "PROMOTED": "live — re-checked at the {review} review",
    "EXTEND": "verdict at the {review} review (next miss kills)",
    "RUNNING": "results computing — verdict by the {review} review",
    "REGISTERED": "awaiting first run — verdict by the {review} review",
    "KILLED": "final — kept for the record",
    "WATCHLIST": "unscored context — re-tested only on new data",
}


def _next_review(now: datetime) -> str:
    """The next monthly-review date (1st of next month) as e.g. 'Aug 1'."""
    y, m = (now.year, now.month + 1) if now.month < 12 else (now.year + 1, 1)
    return f"{datetime(y, m, 1).strftime('%b')} 1"


def _rows(conn, sql: str, params=()) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []


def _iso_ms(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def _band(froth) -> str | None:
    return scoring.froth_band(froth) if froth is not None else None


def aggregate_today(conn, cfg: Config | None = None) -> dict:
    """The Today payload from one read connection (pure aside from reads)."""
    now = datetime.now(timezone.utc)
    window_ms = sched.act_window_start_ms(now)
    act: list[dict] = []

    lab_sync_row = _rows(conn, "SELECT value FROM lab_meta WHERE key='last_sync'")
    lab_sync = sched.lab_sync_state(
        lab_sync_row[0]["value"] if lab_sync_row else None, now)

    # --- new events from PROMOTED studies (the live picks) -------------------
    # "New" means newly ARRIVED (ingested_at), not newly dated: event_ts is
    # stamped midnight UTC of the trade date and the nightly ingests it ≥1 day
    # later, so `event_ts >= window` (midnight ET = 04:00 UTC) could never
    # match — the owner can only act once the row lands anyway. COALESCE keeps
    # the old semantics for rows without an ingest stamp. The event_ts bound
    # keeps a mass re-crawl (late-filed Form 4s, monthly SUE refresh) from
    # surfacing near-expired events as fresh picks: only the first half of a
    # study's holding window is worth acting on (sessions → calendar ≈ ×7/5).
    promoted = _rows(conn, "SELECT name, primary_horizon FROM studies "
                           "WHERE status='PROMOTED' AND tier='alpha'")
    now_ms = int(now.timestamp() * 1000)
    for s in promoted:
        horizon_days = round((s.get("primary_horizon") or 21) * 7 / 5)
        min_event_ts = now_ms - horizon_days * _DAY_MS // 2
        for e in _rows(conn,
                       "SELECT ticker, event_ts, direction, strength, tier, sector, meta "
                       "FROM events WHERE study=? AND COALESCE(ingested_at, event_ts) >= ? "
                       "AND event_ts >= ? "
                       "ORDER BY event_ts DESC LIMIT 20", (s["name"], window_ms, min_event_ts)):
            try:
                meta = json.loads(e.get("meta") or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            act.append({"kind": "event", "study": s["name"], "ticker": e["ticker"],
                        "direction": e["direction"], "ts": e["event_ts"],
                        "tier": e.get("tier"), "sector": e.get("sector"),
                        "detail": f"{meta.get('n_managers') or len(meta.get('owners', []))} "
                                  f"insider(s), ${round((meta.get('agg_usd') or 0) / 1000)}k"
                                  if meta else "",
                        "label": "PROMOTED",
                        # Gap C: an overdue nightly means these may be old news
                        # — the page/digest demote them from picks to recording.
                        "stale": bool(lab_sync.get("overdue"))})

    # --- BTC tier change / froth escalation across the WINDOW ----------------
    # Compare the latest run against the last run BEFORE the window opened
    # (Gap D): a change anywhere inside the window is news; comparing only the
    # last two 6-hourly runs would drop anything older than ~6h.
    runs = _rows(conn, "SELECT run_ts, tier, froth FROM runs ORDER BY run_ts DESC LIMIT 300")
    latest = runs[0] if runs else None
    # A change is news only if the LATEST run is itself inside the window —
    # otherwise the pre-window state and the current state are the same run.
    if latest and (_iso_ms(latest["run_ts"]) or 0) < window_ms:
        latest = None
    prev = None
    for r in runs[1:]:
        ms = _iso_ms(r["run_ts"])
        if ms is not None and ms < window_ms:
            prev = r
            break
    if latest and prev:
        if latest["tier"] != prev["tier"]:
            act.append({"kind": "btc_tier", "ticker": "BTC",
                        "detail": f"{prev['tier']} → {latest['tier']}",
                        "ts": _iso_ms(latest["run_ts"]), "label": "TIER CHANGE"})
        b_now, b_prev = _band(latest.get("froth")), _band(prev.get("froth"))
        if (b_now and b_prev and b_now != b_prev
                and _BAND_ORDER.index(b_now) > _BAND_ORDER.index(b_prev)):
            act.append({"kind": "froth", "ticker": "BTC",
                        "detail": f"{b_prev} → {b_now}",
                        "ts": _iso_ms(latest["run_ts"]), "label": "FROTH"})

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

    # --- testing strip (status verbatim + concrete next decision) -------------
    review = _next_review(now)
    testing = [{"name": r["name"], "status": r["status"], "tier": r["tier"],
                "next_decision": _NEXT_DECISION.get(
                    r["status"], "—").format(review=review)}
               for r in _rows(conn, "SELECT name, status, tier FROM studies "
                                    "ORDER BY registered_at")]

    # --- paper book ------------------------------------------------------------
    nav = _rows(conn, "SELECT * FROM paper_nav ORDER BY date DESC LIMIT 1")
    counts = {r["status"]: r["n"] for r in _rows(
        conn, "SELECT status, count(*) n FROM paper_positions GROUP BY status")}
    n0 = nav[0] if nav else {}
    paper = {"nav": n0.get("nav"), "bench": n0.get("bench"),
             "nav_after_tax": n0.get("nav_after_tax"), "date": n0.get("date"),
             "open": counts.get("OPEN", 0), "pending": counts.get("PENDING", 0),
             "closed": counts.get("CLOSED", 0)}

    # --- one-line health summary (§4: the digest carries it too) --------------
    last_collect = store.last_collect_ts(conn)
    last_run = store.last_run_ts(conn)
    collect_h = ((now - last_collect).total_seconds() / 3600.0
                 if last_collect else None)
    run_h = (now - last_run).total_seconds() / 3600.0 if last_run else None
    stale_h = cfg.watchdog_stale_hours if cfg else 3.0
    health = {"collect_age_hours": round(collect_h, 2) if collect_h is not None else None,
              "run_age_hours": round(run_h, 2) if run_h is not None else None,
              "collect_stale": collect_h is None or collect_h > stale_h,
              "run_stale": run_h is None or run_h > _RUN_STALE_HOURS}

    return {"window_start_ms": window_ms,
            "act": act,
            "testing": testing,
            "paper": paper,
            "health": health,
            "lab_sync": lab_sync,
            "note": ("Act rows are verdict-labeled and share the digest's "
                     "window (previous business day 00:00 ET). Nothing here is "
                     "advice; the owner decides.")}


@router.get("/api/today")
def today(cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    conn = _conn(cfg)
    try:
        return aggregate_today(conn, cfg)
    finally:
        conn.close()

"""Read-only JSON API for the stock swing tracker (FastAPI router).

Mounted into the same app as the BTC API (``app.include_router`` in api.py) and
shares the same DB (opened read-only per request) + bearer-token gate. Namespaced
under ``/api/stock/*`` so the two asset dashboards never collide.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from . import stock_positions, stock_store, store
from .api_deps import conn_ro as _conn, get_config as _cfg, require_token as _require_token
from .config import Config

router = APIRouter(prefix="/api/stock", tags=["stock"])

# Daily cron; allow ~1 missed run + slack before "stale".
_STOCK_STALE_HOURS = 30.0
# A configured layer that returned ZERO rows for this many consecutive runs is
# flagged degraded — key/flag presence says nothing about data actually flowing.
_DEGRADED_RUNS = 3
# Open positions not repriced for longer than this (~5 trading days) are surfaced.
_POSITION_STALE_MS = 7 * 86_400_000


# P3 retirement: the stock_track_record.json / stock_st_winrates.json loaders
# are gone (artifacts archived to archive/v1 — the seed was look-ahead-tainted
# and the honest recalibration measured coin-flip). Setup trust now reads from
# the lab's sue_pead verdict; nothing here re-derives a maturity rung.

# Layer -> the run-readings counts key that proves data actually flowed.
_LAYER_COUNT_KEYS = {
    "prices": "prices_fetched",
    "earnings_pead": "earnings_rows",
    "insider": "insider_ok",
}


def _layer_status(runs: list[dict], layer: str, active: bool) -> str:
    """'off' | 'ok' | 'degraded' from the last runs' per-layer outcome counts.
    Degraded = configured-active but zero rows for the last N runs that recorded
    counts (a revoked key / blocked scrape looks exactly like this). Runs from
    before the counts readings existed are skipped (tolerate old rows)."""
    if not active:
        return "off"
    key = _LAYER_COUNT_KEYS[layer]
    vals = []
    for r in runs:
        c = (r.get("readings") or {}).get("counts") or {}
        if c.get(key) is not None:
            vals.append(c[key])
    if len(vals) >= _DEGRADED_RUNS and all(v == 0 for v in vals[:_DEGRADED_RUNS]):
        return "degraded"
    return "ok"


@router.get("/health")
def health(cfg: Config = Depends(_cfg), _=Depends(_require_token)) -> dict:
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    out = {
        "ok": True, "now": now.isoformat(),
        "price_source": cfg.stock_price_source,
        "universe_path": cfg.stock_universe_path,
        "layers": {
            "prices": False,  # proven by data flow below, not asserted
            "earnings_pead": cfg.finnhub_active,
            "insider": cfg.stock_insider_active,
        },
        "layer_status": {}, "degraded_layers": [],
        "db_ok": False, "last_run": None, "run_age_hours": None, "stale": True,
        "universe_n": None, "regime": None, "coverage": None, "degraded_run": None,
        "stale_positions": [],
    }
    # §10 freshness: next cron ATTEMPTS from the grid (never from the data).
    from . import schedule as _sched
    out["schedule"] = _sched.stock_schedule(now)
    try:
        conn = store.connect_readonly(cfg.db_path)
        lr = stock_store.last_stock_run_ts(conn)
        runs = stock_store.recent_stock_runs(conn, _DEGRADED_RUNS)
        uni = stock_store.get_universe(conn)
        openp = stock_store.open_positions(conn)
        conn.close()
        latest = runs[0] if runs else None
        readings = (latest or {}).get("readings", {})
        counts = readings.get("counts") or {}
        out["db_ok"] = True
        out["last_run"] = lr.isoformat() if lr else None
        out["universe_n"] = len(uni)
        out["regime"] = readings.get("regime")
        out["coverage"] = readings.get("coverage")
        out["degraded_run"] = readings.get("degraded")
        out["layers"]["prices"] = bool(counts.get("prices_fetched") or
                                       (readings.get("layers") or {}).get("prices"))
        active = {"prices": True, "earnings_pead": cfg.finnhub_active,
                  "insider": cfg.stock_insider_active}
        out["layer_status"] = {k: _layer_status(runs, k, v) for k, v in active.items()}
        out["degraded_layers"] = [k for k, v in out["layer_status"].items() if v == "degraded"]
        # Open positions whose ticker hasn't repriced in ~5 trading days: a silent
        # per-name fetch failure freezes the forward-test without any other alarm.
        out["stale_positions"] = [
            {"ticker": p["ticker"], "archetype": p["archetype"],
             "days_since_reprice": round((now_ms - (p.get("last_reprice_ts")
                                                    or p.get("opened_ts") or now_ms))
                                         / 86_400_000, 1)}
            for p in openp
            if now_ms - (p.get("last_reprice_ts") or p.get("opened_ts") or now_ms)
            > _POSITION_STALE_MS]
        age = (now - lr).total_seconds() / 3600.0 if lr else None
        out["run_age_hours"] = round(age, 2) if age is not None else None
        out["stale"] = age is None or age > _STOCK_STALE_HOURS
    except sqlite3.Error as exc:
        out["ok"] = False
        out["error"] = str(exc)
    return out


def _setup_from_signal(s: dict) -> dict:
    """Shape a stored stock_signals row (+ parsed detail) into a screener setup card."""
    d = s.get("detail") or {}
    conf = d.get("confidence") or {}
    lv = d.get("levels") or {}
    feat = d.get("feat") or {}
    return {
        "ticker": s["ticker"], "rank": s["rank"], "direction": s["direction"],
        "archetype": s["archetype"], "archetype_label": d.get("archetype_label", s["archetype"]),
        "composite": s["composite"], "surfaced": d.get("surfaced", True),
        "edge_class": d.get("edge_class", "unproven"), "priority": d.get("priority"),
        "catalyst": d.get("catalyst"),
        "name": feat.get("name"), "sector": feat.get("sector"),
        "confidence": {
            "prob": conf.get("prob", s.get("confidence")),
            "label": conf.get("label"), "base_rate": conf.get("base_rate"),
            "expectancy_r": conf.get("expectancy_r"), "n": conf.get("n"),
            "live_confirmed": conf.get("live_confirmed"),
        },
        "levels": {
            "price": s["price"], "entry": s["entry"], "stop": s["stop"],
            "t1": s["t1"], "t2": s["t2"], "atr": s["atr"], "rr": s["rr"],
            "risk_pct": lv.get("risk_pct"), "time_stop_days": lv.get("time_stop_days"),
        },
        "components": {"pead": s.get("pead"), "technical": s.get("technical"),
                       "insider": s.get("insider"), "revision": s.get("revision")},
        "context": d.get("context") or {},
        "rel": d.get("rel"), "regime": d.get("regime_state"),
        "pead_detail": {k: d.get(k) for k in
                        ("surprise_pct", "reaction_pct", "reaction_sigma", "rev_surprise_pct",
                         "vol_ratio", "drift_since_pct", "bars_since")
                        if d.get(k) is not None} or None,
    }


@router.get("/screener")
def screener(cfg: Config = Depends(_cfg), _=Depends(_require_token)) -> dict:
    """Latest run's ranked setups (surfaced top-N first) with levels + confidence."""
    conn = _conn(cfg)
    try:
        latest = stock_store.latest_stock_run(conn)
        signals = stock_store.latest_stock_signals(conn)
    finally:
        conn.close()
    setups = [_setup_from_signal(s) for s in signals]
    readings = (latest or {}).get("readings", {})
    return {
        "run_ts": (latest or {}).get("run_ts"),
        "regime": readings.get("regime"),
        "universe_n": (latest or {}).get("universe_n"),
        "scored_n": (latest or {}).get("scored_n"),
        "coverage": readings.get("coverage"),
        "degraded": readings.get("degraded"),
        "layers": readings.get("layers", {}),
        "price_source": readings.get("price_source", cfg.stock_price_source),
        "surfaced": [s for s in setups if s["surfaced"]],
        "watchlist": [s for s in setups if not s["surfaced"]][:20],
        "note": ("Cross-sectional swing screener — RECORDING ONLY. The alerted "
                 "population measured statistically indistinguishable from a coin "
                 "flip; setups accrue forward evidence for the lab (sue_pead), "
                 "they are not picks. Alert-only, not advice."),
    }


@router.get("/positions")
def positions(cfg: Config = Depends(_cfg), _=Depends(_require_token)) -> dict:
    """Open forward-test positions + recently closed + the aggregate track record.
    The position tracker IS the out-of-sample record (grows as trades close)."""
    conn = _conn(cfg)
    try:
        openp = stock_store.open_positions(conn)
        pending = stock_store.pending_positions(conn)
        closed = stock_store.closed_positions(conn)
    finally:
        conn.close()
    summary = stock_positions.summarize(closed)  # excludes voided ('rebased') rows
    recent_closed = sorted(closed, key=lambda r: r.get("closed_ts") or 0, reverse=True)[:40]
    return {"open": openp, "pending": pending, "recent_closed": recent_closed,
            "summary": summary, "n_open": len(openp), "n_pending": len(pending),
            "n_closed": len(closed),
            "note": ("Forward-tested on the tracker's own signals — out-of-sample, "
                     "grows over time. Fills are at the NEXT session's open (pending "
                     "until then); voided/rebased rows never count.")}


@router.get("/alerts")
def alerts(limit: int = Query(50, ge=1, le=200),
           cfg: Config = Depends(_cfg), _=Depends(_require_token)) -> dict:
    conn = _conn(cfg)
    try:
        rows = stock_store.recent_stock_alerts(conn, limit)
    finally:
        conn.close()
    return {"alerts": rows}


@router.get("/ticker/{sym}")
def ticker(sym: str, cfg: Config = Depends(_cfg), _=Depends(_require_token)) -> dict:
    """Per-name detail: latest setup (if any) + recent price series for a mini chart."""
    sym = sym.upper()[:12]
    conn = _conn(cfg)
    try:
        signals = stock_store.latest_stock_signals(conn)
        sig = next((s for s in signals if s["ticker"] == sym), None)
        prices = stock_store.recent_prices(conn, sym, 180)
    finally:
        conn.close()
    return {"ticker": sym,
            "setup": _setup_from_signal(sig) if sig else None,
            "candles": prices}

"""Read-only JSON API for the stock swing tracker (FastAPI router).

Mounted into the same app as the BTC API (``app.include_router`` in api.py) and
shares the same DB (opened read-only per request) + bearer-token gate. Namespaced
under ``/api/stock/*`` so the two asset dashboards never collide.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from . import stock_confidence, stock_positions, stock_store, store
from .config import Config, load_config

router = APIRouter(prefix="/api/stock", tags=["stock"])

# Daily cron; allow ~1 missed run + slack before "stale".
_STOCK_STALE_HOURS = 30.0


@lru_cache(maxsize=1)
def _cfg() -> Config:
    return load_config()


def _require_token(authorization: str | None = Header(None)) -> None:
    import secrets
    cfg = _cfg()
    if not cfg.api_token:
        return
    expected = f"Bearer {cfg.api_token}"
    ok = bool(authorization) and secrets.compare_digest(
        (authorization or "").encode("utf-8", "ignore"), expected.encode("utf-8"))
    if not ok:
        raise HTTPException(status_code=401, detail="unauthorized")


def _conn(cfg: Config) -> sqlite3.Connection:
    try:
        return store.connect_readonly(cfg.db_path)
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}")


@lru_cache(maxsize=1)
def _track_record() -> dict:
    try:
        return json.loads(Path(__file__).with_name("stock_track_record.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


@lru_cache(maxsize=1)
def _winrates() -> dict:
    try:
        return json.loads(Path(__file__).with_name("stock_st_winrates.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


@router.get("/health")
def health(cfg: Config = Depends(_cfg), _=Depends(_require_token)) -> dict:
    now = datetime.now(timezone.utc)
    out = {
        "ok": True, "now": now.isoformat(),
        "price_source": cfg.stock_price_source,
        "universe_path": cfg.stock_universe_path,
        "layers": {
            "prices": True,  # keyless (Yahoo) or keyed (Alpaca/Tiingo)
            "earnings_pead": cfg.finnhub_active,
            "insider": cfg.stock_insider_active,
            "shortvol": cfg.stock_shortvol_active,
            "congress": cfg.stock_congress_active,
        },
        "db_ok": False, "last_run": None, "run_age_hours": None, "stale": True,
        "universe_n": None, "regime": None,
    }
    try:
        conn = store.connect_readonly(cfg.db_path)
        lr = stock_store.last_stock_run_ts(conn)
        latest = stock_store.latest_stock_run(conn)
        uni = stock_store.get_universe(conn)
        conn.close()
        out["db_ok"] = True
        out["last_run"] = lr.isoformat() if lr else None
        out["universe_n"] = len(uni)
        out["regime"] = (latest or {}).get("readings", {}).get("regime")
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
                       "insider": s.get("insider"), "shortvol": s.get("shortvol"),
                       "revision": s.get("revision")},
        "context": d.get("context") or {},
        "rel": d.get("rel"), "regime": d.get("regime_state"),
        "pead_detail": {k: d.get(k) for k in
                        ("surprise_pct", "reaction_pct", "drift_since_pct", "bars_since")
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
        "layers": readings.get("layers", {}),
        "price_source": readings.get("price_source", cfg.stock_price_source),
        "surfaced": [s for s in setups if s["surfaced"]],
        "watchlist": [s for s in setups if not s["surfaced"]][:20],
        "note": ("Cross-sectional swing screener. Confidence is a backtested prior "
                 "until the live position tracker confirms it. Alert-only, not advice."),
    }


@router.get("/positions")
def positions(cfg: Config = Depends(_cfg), _=Depends(_require_token)) -> dict:
    """Open forward-test positions + recently closed + the aggregate track record.
    The position tracker IS the out-of-sample record (grows as trades close)."""
    conn = _conn(cfg)
    try:
        openp = stock_store.open_positions(conn)
        closed = stock_store.closed_positions(conn)
    finally:
        conn.close()
    summary = stock_positions.summarize(closed)
    recent_closed = sorted(closed, key=lambda r: r.get("closed_ts") or 0, reverse=True)[:40]
    return {"open": openp, "recent_closed": recent_closed, "summary": summary,
            "n_open": len(openp), "n_closed": len(closed),
            "note": "Forward-tested on the tracker's own signals — out-of-sample, grows over time."}


@router.get("/track_record")
def track_record(_=Depends(_require_token)) -> dict:
    tr = _track_record()
    return {"available": True, **tr} if tr.get("available") else {"available": False}


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
        sv = stock_store.recent_shortvol(conn, sym, 20)
    finally:
        conn.close()
    return {"ticker": sym,
            "setup": _setup_from_signal(sig) if sig else None,
            "candles": prices, "shortvol": sv}

"""Read-only JSON API for the dashboard (FastAPI + uvicorn).

Bound to localhost in production; the co-hosted Next.js server reads it over
127.0.0.1 and the human-facing gate is Cloudflare Access in front of the
dashboard. A bearer token (cfg.api_token) is an internal dashboard<->API secret:
enforced when set, open when unset (local dev / localhost-only).

The DB is opened READ-ONLY per request so the API can never corrupt collector
writes (WAL allows concurrent reads).

Run:  uvicorn app.api:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from functools import lru_cache

import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from . import shortterm, store
from .config import Config, load_config

app = FastAPI(title="BTC Signal API", version="1.0.0")


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()


# CORS only matters if the dashboard is served cross-origin (it usually isn't).
_cfg = get_config()
if _cfg.api_cors_origin:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[_cfg.api_cors_origin],
        allow_methods=["GET"],
        allow_headers=["*"],
    )


def require_token(authorization: str | None = Header(None),
                  cfg: Config = Depends(get_config)) -> None:
    """Enforce the internal bearer token when one is configured."""
    if not cfg.api_token:
        return  # dev / localhost-only: open
    if authorization != f"Bearer {cfg.api_token}":
        raise HTTPException(status_code=401, detail="unauthorized")


def _conn(cfg: Config) -> sqlite3.Connection:
    try:
        return store.connect_readonly(cfg.db_path)
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}")


@app.get("/api/health")
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
            "email": cfg.email_active,
        },
        "db_ok": False,
        "last_collect": None,
        "last_run": None,
        "collect_age_hours": None,
    }
    try:
        conn = store.connect_readonly(cfg.db_path)
        lc = store.last_collect_ts(conn)
        lr = store.last_run_ts(conn)
        conn.close()
        out["db_ok"] = True
        out["last_collect"] = lc.isoformat() if lc else None
        out["last_run"] = lr.isoformat() if lr else None
        if lc:
            out["collect_age_hours"] = round((now - lc).total_seconds() / 3600.0, 2)
        out["stale"] = (lc is None) or ((now - lc).total_seconds() / 3600.0 > cfg.watchdog_stale_hours)
    except sqlite3.OperationalError as exc:
        out["ok"] = False
        out["error"] = str(exc)
    return out


@app.get("/api/longterm/latest")
def longterm_latest(cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    conn = _conn(cfg)
    try:
        latest = store.latest_run(conn)
    finally:
        conn.close()
    return {"latest": latest}


@app.get("/api/shortterm/latest")
def shortterm_latest(cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    conn = _conn(cfg)
    try:
        out = {tf: store.latest_st_signal(conn, tf) for tf in cfg.st_timeframes}
    finally:
        conn.close()
    return {"timeframes": out}


@app.get("/api/candles")
def candles(timeframe: str = Query("4h"), limit: int = Query(300, le=1000),
            cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    if timeframe not in cfg.st_timeframes and timeframe not in ("4h", "1d", "1w"):
        raise HTTPException(status_code=400, detail="unknown timeframe")
    conn = _conn(cfg)
    try:
        rows = store.recent_candles(conn, timeframe, limit)
    finally:
        conn.close()
    return {"timeframe": timeframe, "candles": rows}


@app.get("/api/indicators")
def indicators(timeframe: str = Query("4h"),
               cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    conn = _conn(cfg)
    try:
        rows = store.recent_candles(conn, timeframe, 300)
    finally:
        conn.close()
    if len(rows) < 35:
        return {"timeframe": timeframe, "indicators": None, "n": len(rows)}
    df = pd.DataFrame(rows)
    df["open_time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    ind = shortterm.compute_indicators(df)
    score, comps = shortterm.st_composite(df, cfg)
    return {"timeframe": timeframe, "indicators": ind,
            "score": score, "state": shortterm.st_state(score, cfg), "components": comps}


@app.get("/api/derivs")
def derivs(limit: int = Query(200, le=1000),
           cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    conn = _conn(cfg)
    try:
        rows = store.recent_derivs(conn, limit)
    finally:
        conn.close()
    return {"derivs": rows}


@app.get("/api/alerts")
def alerts(limit: int = Query(50, le=500),
           cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    conn = _conn(cfg)
    try:
        st_rows = store.recent_st_alerts(conn, limit)
        lt_rows = store.recent_run_alerts(conn, 20)
    finally:
        conn.close()
    return {"short_term": st_rows, "long_term": lt_rows}

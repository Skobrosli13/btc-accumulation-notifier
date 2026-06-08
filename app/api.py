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

from . import alerting, scoring, shortterm, store
from .config import Config, load_config

app = FastAPI(title="BTC Signal API", version="1.0.0")

# Display labels (server-side single source of truth; the dashboard never re-derives).
_CATEGORY_LABELS = {
    "onchain": "On-chain valuation", "price": "Price structure",
    "macro": "Macro / liquidity", "sentiment": "Sentiment", "derivs": "Derivatives",
}
_COMPONENT_LABELS = {
    "trend": "Trend (EMA 9/21 spread)", "macd": "MACD histogram",
    "funding": "Funding positioning",
}


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


def _lt_breakdown(latest: dict, cfg: Config) -> dict:
    """Display-ready decomposition of the latest long-term run (reuses scoring labels)."""
    readings = latest.get("readings") or {}
    raw = readings.get("raw") or {}
    ps = readings.get("price_struct") or {}
    subs = readings.get("subscores") or {}
    cats = readings.get("category_scores") or {}
    active = {c for c in (latest.get("active_cats") or "").split(",") if c}

    categories = []
    for cat, inds in scoring.CATEGORY_INDICATORS.items():
        categories.append({
            "key": cat,
            "label": _CATEGORY_LABELS.get(cat, cat),
            "score": cats.get(cat),
            "weight": cfg.weights.get(cat),
            "active": cat in active,
            "indicators": [{
                "key": k,
                "label": scoring.INDICATOR_LABELS.get(k, k),
                "subscore": subs.get(k),
                "raw": raw.get(k),
                "in_zone": (subs.get(k) is not None and subs.get(k) >= scoring.IN_ZONE_THRESHOLD),
            } for k in inds],
        })

    p2w = ps.get("price_to_wma200")
    rr = raw.get("realized_ratio")
    levels = {
        "price": ps.get("price"), "wma200": ps.get("wma200"), "dma200": ps.get("dma200"),
        "price_to_wma200": p2w, "wma200_rel": (None if p2w is None else ("below" if p2w <= 1 else "above")),
        "mayer": ps.get("mayer_multiple"),
        "realized_ratio": rr, "realized_rel": (None if rr is None else ("below" if rr <= 1 else "above")),
        "drop_24_48h_pct": ps.get("drop_24_48h_pct"), "source": ps.get("source"),
    }

    days_since_ath = (datetime.now(timezone.utc).date() - cfg.ath_date).days
    cycle = {
        "ath_date": cfg.ath_date.isoformat(),
        "days_since_ath": days_since_ath,
        "typical_days": cfg.peak_to_trough_days,
        "window_lo": cfg.peak_to_trough_days - scoring.CYCLE_WINDOW_HALFWIDTH_DAYS,
        "window_hi": cfg.peak_to_trough_days + scoring.CYCLE_WINDOW_HALFWIDTH_DAYS,
        "in_window": abs(days_since_ath - cfg.peak_to_trough_days) <= scoring.CYCLE_WINDOW_HALFWIDTH_DAYS,
        "multiplier": readings.get("cycle_multiplier"),
    }

    return {
        "categories": categories,
        "in_zone": scoring.indicators_in_zone(subs),
        "levels": levels,
        "cycle": cycle,
        "tiers": {"watch": cfg.tier_watch, "accumulate": cfg.tier_accumulate,
                  "deep_value": cfg.tier_deepvalue},
    }


@app.get("/api/longterm/latest")
def longterm_latest(cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    conn = _conn(cfg)
    try:
        latest = store.latest_run(conn)
    finally:
        conn.close()
    if latest:
        latest["breakdown"] = _lt_breakdown(latest, cfg)
    return {"latest": latest}


def _enrich_st(conn, cfg: Config, sig: dict | None, tf: str,
               funding: float | None, oi_chg_pct: float | None) -> dict | None:
    """Add live bias components + currently-active triggers to a short-term signal
    (recomputed from recent candles, mirroring collect_once)."""
    if sig is None:
        return None
    sig["funding"] = funding
    sig["oi_chg_pct"] = oi_chg_pct
    sig["components"] = []
    sig["triggers"] = []
    rows = store.recent_candles(conn, tf, 300)
    if len(rows) < 35:
        return sig
    df = pd.DataFrame(rows)
    df["open_time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    try:
        _score, comps = shortterm.st_composite(df, cfg, funding)
        sig["components"] = [{"key": k, "label": _COMPONENT_LABELS.get(k, k), "value": v}
                             for k, v in comps.items()]
        state = sig.get("st_state", "NEUTRAL")
        sig["triggers"] = [{
            "key": t.key, "direction": t.direction, "label": t.label, "detail": t.detail,
            "counter_trend": alerting.is_counter_trend(t.direction, state),
        } for t in shortterm.detect_triggers(df, cfg, funding, oi_chg_pct)]
    except Exception:  # noqa: BLE001 - never 500 the dashboard over a recompute
        pass
    return sig


@app.get("/api/shortterm/latest")
def shortterm_latest(cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    conn = _conn(cfg)
    try:
        derivs = store.recent_derivs(conn, 1)
        funding = derivs[-1]["funding"] if derivs else None
        oi_chg_pct = derivs[-1]["oi_chg_pct"] if derivs else None
        out = {tf: _enrich_st(conn, cfg, store.latest_st_signal(conn, tf), tf, funding, oi_chg_pct)
               for tf in cfg.st_timeframes}
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


def _lt_alert_reason(row: dict) -> dict:
    readings = row.get("readings") or {}
    subs = readings.get("subscores") or {}
    ps = readings.get("price_struct") or {}
    raw = readings.get("raw") or {}
    tier = row.get("tier", "")
    return {
        "type": "flash" if row.get("flash_alerted") else "tier",
        "tier_label": alerting.TIER_LABELS.get(tier, tier),
        "headline": alerting.TIER_HEADLINES.get(tier, ""),
        "in_zone": scoring.indicators_in_zone(subs),
        "levels": {"price_to_wma200": ps.get("price_to_wma200"),
                   "realized_ratio": raw.get("realized_ratio"),
                   "mayer": ps.get("mayer_multiple")},
    }


@app.get("/api/alerts")
def alerts(limit: int = Query(50, le=500),
           cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    conn = _conn(cfg)
    try:
        st_rows = store.recent_st_alerts(conn, limit)
        lt_rows = store.recent_run_alerts(conn, 20)
    finally:
        conn.close()
    for r in lt_rows:
        try:
            r["reason"] = _lt_alert_reason(r)
        except Exception:  # noqa: BLE001
            r["reason"] = None
        r.pop("readings", None)  # keep payload lean
    return {"short_term": st_rows, "long_term": lt_rows}

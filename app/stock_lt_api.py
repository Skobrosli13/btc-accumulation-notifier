"""Read-only JSON API for the long-term "long buys" engine (FastAPI router).

Mounted into the same app at ``/api/stock/longterm/*``; shares the DB (read-only per
request) + bearer-token gate.
"""
from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timezone
from functools import lru_cache

from fastapi import APIRouter, Depends, Header, HTTPException

from . import stock_lt_store, stock_store, store
from .config import Config, load_config

router = APIRouter(prefix="/api/stock/longterm", tags=["stock-longterm"])
_STALE_HOURS = 24 * 9   # weekly cron; allow ~9 days before "stale"


@lru_cache(maxsize=1)
def _cfg() -> Config:
    return load_config()


def _require_token(authorization: str | None = Header(None)) -> None:
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


@router.get("/health")
def health(cfg: Config = Depends(_cfg), _=Depends(_require_token)) -> dict:
    now = datetime.now(timezone.utc)
    out = {"ok": True, "now": now.isoformat(), "massive": cfg.massive_active,
           "db_ok": False, "last_run": None, "run_age_hours": None, "stale": True,
           "universe_n": None, "financials_cached": None, "survivors_n": None}
    try:
        conn = store.connect_readonly(cfg.db_path)
        lr = stock_lt_store.last_lt_run_ts(conn)
        latest = stock_lt_store.latest_lt_run(conn)
        conn.close()
        out["db_ok"] = True
        out["last_run"] = lr.isoformat() if lr else None
        if latest:
            out["universe_n"] = latest.get("universe_n")
            out["survivors_n"] = latest.get("survivors_n")
            out["financials_cached"] = (latest.get("readings") or {}).get("financials_cached")
        age = (now - lr).total_seconds() / 3600.0 if lr else None
        out["run_age_hours"] = round(age, 2) if age is not None else None
        out["stale"] = age is None or age > _STALE_HOURS
    except sqlite3.Error as exc:
        out["ok"] = False
        out["error"] = str(exc)
    return out


def _longbuy(s: dict) -> dict:
    d = s.get("detail") or {}
    return {
        "ticker": s["ticker"], "rank": s["rank"], "conviction": s["conviction"],
        "sector": s.get("sector"), "price": s.get("price"), "surfaced": bool(s.get("surfaced")),
        "ranks": {"value": s.get("value_rank"), "quality": s.get("quality_rank"),
                  "momentum": s.get("momentum_rank")},
        "piotroski": s.get("piotroski"), "altman": d.get("altman"),
        "momentum_12_1_pct": d.get("momentum_12_1"),
        "fair_value": d.get("fair_value"), "metrics": d.get("metrics") or {},
    }


@router.get("/screener")
def screener(cfg: Config = Depends(_cfg), _=Depends(_require_token)) -> dict:
    """Latest conviction 'long buys' (surfaced top-N) + the rest of the survivors."""
    conn = _conn(cfg)
    try:
        latest = stock_lt_store.latest_lt_run(conn)
        signals = stock_lt_store.latest_lt_signals(conn)
    finally:
        conn.close()
    buys = [_longbuy(s) for s in signals]
    return {
        "run_ts": (latest or {}).get("run_ts"),
        "universe_n": (latest or {}).get("universe_n"),
        "scored_n": (latest or {}).get("scored_n"),
        "survivors_n": (latest or {}).get("survivors_n"),
        "long_buys": [b for b in buys if b["surfaced"]],
        "watchlist": [b for b in buys if not b["surfaced"]][:40],
        "note": ("Quality+value+momentum factor tilt (gate purges value traps). "
                 "Long-horizon accumulation, measured vs SPY over quarters. Not advice."),
    }


@router.get("/forward_test")
def forward_test(cfg: Config = Depends(_cfg), _=Depends(_require_token)) -> dict:
    """Benchmark-relative (vs SPY) forward-test of the conviction picks: realized excess
    return on closed holdings + live mark-to-market on open ones."""
    conn = _conn(cfg)
    try:
        openh = stock_lt_store.open_lt_holdings(conn)
        closed = stock_lt_store.closed_lt_holdings(conn)
        spy_now = store.get_meta(conn, "lt_spy_close")
        live = []
        for h in openh:
            bars = stock_store.recent_prices(conn, h["ticker"], 1)
            px = bars[-1]["close"] if bars else None
            if px and h["entry"] and spy_now and h["spy_entry"]:
                name_ret = px / h["entry"] - 1
                spy_ret = float(spy_now) / h["spy_entry"] - 1
                live.append({"ticker": h["ticker"], "excess_return": round(name_ret - spy_ret, 4),
                             "held_since": h["opened_run_ts"]})
    finally:
        conn.close()
    closed_ex = [c["excess_return"] for c in closed if c.get("excess_return") is not None]
    live_ex = [x["excess_return"] for x in live]
    def agg(xs):
        if not xs:
            return {"n": 0, "avg_excess": None, "win_rate": None}
        return {"n": len(xs), "avg_excess": round(sum(xs) / len(xs), 4),
                "win_rate": round(sum(1 for x in xs if x > 0) / len(xs), 3)}
    return {"open": agg(live_ex), "closed": agg(closed_ex),
            "n_open": len(openh), "n_closed": len(closed),
            "live": sorted(live, key=lambda x: x["excess_return"], reverse=True)[:30],
            "note": "Excess return vs SPY. Long-horizon edge shows over quarters; grows over time."}

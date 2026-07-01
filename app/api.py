"""Read-only JSON API for the dashboard (FastAPI + uvicorn).

Bound to localhost in production; the co-hosted Next.js server reads it over
127.0.0.1 and the human-facing gate is nginx HTTP basic auth in front of the
dashboard (Let's Encrypt TLS). A bearer token (cfg.api_token) is an internal
dashboard<->API secret: enforced when set, open when unset (local dev /
localhost-only).

The DB is opened READ-ONLY per request so the API can never corrupt collector
writes (WAL allows concurrent reads).

Run:  uvicorn app.api:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import pandas as pd
from fastapi import (BackgroundTasks, Depends, FastAPI, Header, HTTPException,
                     Query)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import (alerting, notify_email, perf, scoring, shortterm, stock_api,
               stock_store, store)
from .config import Config, load_config
from .sources import exchange

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = FastAPI(title="BTC Signal API", version="1.0.0")

# Stock swing tracker (second asset) — namespaced under /api/stock/*.
app.include_router(stock_api.router)


def _ensure_schema() -> None:
    """Best-effort: create both schemas so the READ-ONLY endpoints never hit a
    missing table on a fresh box (before the first collector cron has run). The
    API already writes for subscribe (_conn_rw/init_db), so this needs no new
    privilege; the stock tables are additive and idempotent."""
    try:
        conn = store.connect(load_config().db_path)
        store.init_db(conn)
        stock_store.init_stock_db(conn)
        conn.close()
    except Exception:  # noqa: BLE001 - never block startup on schema init
        pass


_ensure_schema()

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
    expected = f"Bearer {cfg.api_token}"
    # Constant-time compare so the token can't be recovered byte-by-byte via timing.
    # Compare bytes so a non-ASCII header can't raise TypeError -> 500 (compare_digest
    # rejects mixed/non-ASCII str); a bad header should be a clean 401.
    ok = bool(authorization) and secrets.compare_digest(
        (authorization or "").encode("utf-8", "ignore"), expected.encode("utf-8"))
    if not ok:
        raise HTTPException(status_code=401, detail="unauthorized")


def _conn(cfg: Config) -> sqlite3.Connection:
    try:
        return store.connect_readonly(cfg.db_path)
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}")


def _conn_rw(cfg: Config) -> sqlite3.Connection:
    """A short-lived read-WRITE connection for the subscribe/unsubscribe writes.

    The API is read-only by design; this is the one narrow exception. The
    subscribers table is separate from the collector's tables and WAL +
    busy_timeout handle the rare writer overlap. ``init_db`` is idempotent and
    guarantees the table exists even before the first collector run.
    """
    try:
        conn = store.connect(cfg.db_path)
        store.init_db(conn)
        return conn
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}")


# Long-term runs are a 6h cron; allow ~2 cadences + slack before "stale" so a
# single missed run isn't flagged, but a dead run_once (which the 10-min collector
# would otherwise mask) is caught.
_RUN_STALE_HOURS = 13.0


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
    try:
        conn = store.connect_readonly(cfg.db_path)
        lc = store.last_collect_ts(conn)
        lr = store.last_run_ts(conn)
        conn.close()
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


@lru_cache(maxsize=1)
def _st_winrates() -> dict:
    """Read app/st_winrates.json once (emitted by scripts/st_calibrate.py). {} if absent."""
    try:
        return json.loads(Path(__file__).with_name("st_winrates.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


@lru_cache(maxsize=1)
def _track_record_data() -> dict:
    """Read app/track_record.json once (emitted by scripts/calibrate.py). {} if absent."""
    try:
        return json.loads(Path(__file__).with_name("track_record.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


@app.get("/api/live_performance")
def live_performance(cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    """Forward-tested record of the system's OWN past signals (out-of-sample; grows
    over time). Long-term runs + fired swing alerts priced against stored candles."""
    conn = _conn(cfg)
    try:
        runs = store.all_runs(conn)
        candles = store.recent_candles(conn, "1d", 400)
        st_rows = store.recent_st_alerts(conn, 500)
    finally:
        conn.close()
    alerts = [{"ts": a.get("ts"), "direction": a.get("direction"), "price": a.get("price")}
              for a in st_rows]
    return {
        "long_term": perf.long_term_performance(runs, candles),
        "short_term": perf.short_term_performance(alerts, candles),
        "note": "Forward-tested on the system's live signals as they age — out-of-sample, grows over time.",
    }


@app.get("/api/track_record")
def track_record(_=Depends(require_token)) -> dict:
    """Historical forward-return hit-rate of the percentile backbone (illustrative,
    not a forecast). {"available": false} until the calibration script has run."""
    tr = _track_record_data()
    return {"available": True, **tr} if tr else {"available": False}


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
                # The raw value at which this indicator's badge lights ("what
                # flips this"), inverted through the same calibrated/linear
                # mapping the scorer uses, plus which side of it lights (le/ge).
                "zone_at": scoring.zone_boundary_raw(k),
                "zone_dir": ("le" if scoring.DIRECTION.get(k) == "lower_bullish" else "ge"),
                # representative key when k is part of a redundancy group (else None);
                # members of the same group count once toward the category score.
                "group": scoring.INDICATOR_GROUP.get(k),
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

    ath_d = cfg.ath_date
    if ps.get("ath_date"):
        try:
            ath_d = datetime.strptime(ps["ath_date"], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            pass
    days_since_ath = (datetime.now(timezone.utc).date() - ath_d).days
    cycle = {
        "ath_date": ath_d.isoformat(),
        "ath_price": ps.get("ath_price"),
        "days_since_ath": days_since_ath,
        "typical_days": cfg.peak_to_trough_days,
        "window_lo": cfg.peak_to_trough_days - scoring.CYCLE_WINDOW_HALFWIDTH_DAYS,
        "window_hi": cfg.peak_to_trough_days + scoring.CYCLE_WINDOW_HALFWIDTH_DAYS,
        "in_window": abs(days_since_ath - cfg.peak_to_trough_days) <= scoring.CYCLE_WINDOW_HALFWIDTH_DAYS,
        "multiplier": readings.get("cycle_multiplier"),
    }

    # Sell-side overheat read. Prefer the froth block PERSISTED by run_once (the
    # single source of truth — its band carries run-to-run hysteresis); fall back
    # to a request-time compute for runs recorded before froth existed. The
    # price-structure fields fall back to ps for runs predating their mirror
    # into raw.
    froth_input = dict(raw)
    if froth_input.get("price_to_wma200") is None:
        froth_input["price_to_wma200"] = ps.get("price_to_wma200")
    if froth_input.get("mayer") is None:
        froth_input["mayer"] = ps.get("mayer_multiple")
    # Known skew: stored subscores reflect the TOP_THRESHOLDS at run time while
    # zone_at below is recomputed from the current constants — after a future
    # threshold revision the latest run can disagree with its own "lights at"
    # hints for up to one 6h cadence. Display-only and self-healing.
    stored_fr = readings.get("froth")
    fr = (stored_fr if isinstance(stored_fr, dict) and "subscores" in stored_fr
          else scoring.froth_score(froth_input))
    froth = {
        "score": fr.get("score"),
        "band": fr.get("band") or scoring.froth_band(fr.get("score")),
        "active": fr.get("active"),
        "in_zone": fr.get("in_zone") or [],
        "indicators": [{
            "key": k,
            "label": scoring.INDICATOR_LABELS.get(k, k),
            "subscore": fr["subscores"].get(k),
            "raw": froth_input.get(k),
            "in_zone": (fr["subscores"].get(k) is not None
                        and fr["subscores"][k] >= scoring.IN_ZONE_THRESHOLD),
            # Froth indicators all light on the HIGH side.
            "zone_at": scoring.top_zone_boundary_raw(k),
            "zone_dir": "ge",
        } for k in scoring.TOP_THRESHOLDS],
        "note": ("heuristic top-signal — thresholds anchored to the 2017/2021/2025 "
                 "cycle tops via scripts/backtest_tops (1-3 cycles per indicator; "
                 "not a proven edge)"),
    }

    return {
        "categories": categories,
        "in_zone": scoring.indicators_in_zone(subs),
        "froth": froth,
        "levels": levels,
        "cycle": cycle,
        "tiers": {"watch": cfg.tier_watch, "accumulate": cfg.tier_accumulate,
                  "deep_value": cfg.tier_deepvalue},
        "playbook": readings.get("playbook"),
        "what_to_do": readings.get("what_to_do"),
        "agreement": readings.get("agreement"),   # category-agreement confidence proxy
        # context-only metrics (shown, not scored)
        "context": {
            "reserve_risk": raw.get("reserve_risk"), "rhodl": raw.get("rhodl"),
            # Free on-chain network-activity reads — FORWARD-TEST, not in the score.
            # Each carries the latest value + a trailing-90d z-score.
            "netactivity": {
                "active_addr": raw.get("na_active_addr"), "active_addr_z": raw.get("na_active_addr_z"),
                "tx_count": raw.get("na_tx_count"), "tx_count_z": raw.get("na_tx_count_z"),
                "transfers": raw.get("na_transfers"), "transfers_z": raw.get("na_transfers_z"),
                "addr_balance": raw.get("na_addr_balance"), "addr_balance_z": raw.get("na_addr_balance_z"),
            },
        },
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


@app.get("/api/longterm/history")
def longterm_history(limit: int = Query(0, ge=0, le=5000),
                     cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    """Composite/tier per long-term run, oldest->newest — the score-over-time
    series for the dashboard (streaks, cycle-best, trajectory). limit=0 -> all."""
    conn = _conn(cfg)
    try:
        rows = store.run_history(conn, limit)
    finally:
        conn.close()
    return {"runs": rows}


@app.get("/api/playbook")
def playbook_latest(cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    """The latest run's illustrative playbook — ladder + unified 'what to do now'."""
    conn = _conn(cfg)
    try:
        latest = store.latest_run(conn)
    finally:
        conn.close()
    readings = (latest or {}).get("readings") or {}
    return {"tier": (latest or {}).get("tier"),
            "conviction": readings.get("conviction"),
            "playbook": readings.get("playbook"),
            "what_to_do": readings.get("what_to_do")}


@app.get("/api/price")
def spot_price(cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    """Live spot price (exchange ticker; no DB) so the dashboard headline can
    refresh on its own 60s cadence instead of waiting for the 6h long-term run.
    ``price`` is None if every venue is unreachable — the dashboard then falls
    back to the stored long-term price."""
    return {"price": exchange.spot_price(cfg.symbol, prefer=cfg.exchange)}


def _drop_forming(df: pd.DataFrame) -> pd.DataFrame:
    """Drop the still-forming candle before recomputing indicators.

    The `candles` table does not persist the exchange ``confirmed`` flag, and the
    newest stored row is always the in-progress candle (re-upserted each collect).
    The collector and alert path evaluate on CLOSED candles only; this keeps the
    dashboard's live recompute consistent so it never shows a phantom trigger that
    the alerting path would never fire. Conservative if the collector is stale
    (drops one already-closed candle at worst)."""
    return df.iloc[:-1] if len(df) > 1 else df


def _enrich_st(conn, cfg: Config, sig: dict | None, tf: str,
               funding: float | None, oi_chg_pct: float | None,
               regime: str = "unknown") -> dict | None:
    """Add live bias components + currently-active triggers to a short-term signal
    (recomputed from recent candles, mirroring collect_once)."""
    if sig is None:
        return None
    sig["funding"] = funding
    sig["oi_chg_pct"] = oi_chg_pct
    sig["regime"] = regime
    sig["components"] = []
    sig["triggers"] = []
    # contiguous_source: never recompute indicators/triggers across a venue switch
    # (a fallback batch has a different quote currency + volume scale, which would
    # produce phantom vol-spike triggers right at the boundary).
    rows = store.recent_candles(conn, tf, 300, contiguous_source=True)
    if len(rows) < 35:
        return sig
    df = pd.DataFrame(rows)
    df["open_time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = _drop_forming(df)  # closed-candle-only, matching the collector/alert path
    try:
        _score, comps = shortterm.st_composite(df, cfg, funding)
        sig["components"] = [{"key": k, "label": _COMPONENT_LABELS.get(k, k), "value": v}
                             for k, v in comps.items()]
        state = sig.get("st_state", "NEUTRAL")
        st_price = sig.get("price")
        atr = (sig.get("indicators") or {}).get("atr")
        wr = _st_winrates().get("timeframes", {}).get(tf, {})
        live_trigs = shortterm.detect_triggers(df, cfg, funding, oi_chg_pct)
        dirs = [t.direction for t in live_trigs]
        sig["triggers"] = [{
            "key": t.key, "direction": t.direction, "label": t.label, "detail": t.detail,
            "counter_trend": alerting.is_counter_trend(t.direction, state),
            "regime_aligned": shortterm.regime_aligned(t.direction, regime),
            "confluence": shortterm.confluence_ok(
                dirs.count(t.direction), shortterm.regime_aligned(t.direction, regime),
                alerting.is_counter_trend(t.direction, state)),
            "levels": shortterm.trade_levels(t.direction, st_price, atr),
            "stats": wr.get(t.key),   # historical win-rate + ATR R-expectancy, or None
        } for t in live_trigs]
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
        rows1d = store.recent_candles(conn, "1d", 300, contiguous_source=True)
        # Drop the still-forming daily candle so the dashboard regime matches the
        # collector/alert path (which is closed-candle-only) — no intraday flip-flop
        # of regime_aligned/confluence near the 200DMA.
        closed1d = rows1d[:-1] if len(rows1d) > 1 else rows1d
        regime = shortterm.current_regime(
            pd.DataFrame(closed1d)["close"] if closed1d else None)
        out = {tf: _enrich_st(conn, cfg, store.latest_st_signal(conn, tf), tf,
                              funding, oi_chg_pct, regime)
               for tf in cfg.st_timeframes}
    finally:
        conn.close()
    return {"timeframes": out}


@app.get("/api/candles")
def candles(timeframe: str = Query("4h"), limit: int = Query(300, ge=1, le=1000),
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
        rows = store.recent_candles(conn, timeframe, 300, contiguous_source=True)
    finally:
        conn.close()
    if len(rows) < 35:
        return {"timeframe": timeframe, "indicators": None, "n": len(rows)}
    df = pd.DataFrame(rows)
    df["open_time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = _drop_forming(df)  # closed-candle-only, matching the collector/alert path
    ind = shortterm.compute_indicators(df)
    score, comps = shortterm.st_composite(df, cfg)
    return {"timeframe": timeframe, "indicators": ind,
            "score": score, "state": shortterm.st_state(score, cfg), "components": comps}


@app.get("/api/derivs")
def derivs(limit: int = Query(200, ge=1, le=1000),
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
        "changed": readings.get("changed"),
    }


@app.get("/api/alerts")
def alerts(limit: int = Query(50, ge=1, le=500),
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


# --- Email subscriptions -----------------------------------------------------

class SubscribeIn(BaseModel):
    email: str


def _send_welcome(cfg: Config, email: str, unsubscribe_url: str) -> None:
    """Confirmation email (also carries the unsubscribe link). Best-effort."""
    subject = "You're subscribed to BTC signal alerts"
    body = (
        "You'll now receive Bitcoin long-term accumulation alerts at this address:\n\n"
        "  • Tier changes — WATCH → ACCUMULATE → DEEP_VALUE\n"
        "  • Capitulation flash — an acute, oversold-fear washout\n\n"
        "These are infrequent, high-confluence signals (not the noisier short-term "
        "swing triggers, which stay on the dashboard). Not financial advice — "
        "long-term is buy-only accumulation; you decide whether, how much, and where."
    )
    notify_email.send_email(cfg, subject, body, to=email, unsubscribe_url=unsubscribe_url)


@app.post("/api/subscribe")
def subscribe(body: SubscribeIn, background: BackgroundTasks,
              cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    """Add an email to the alert broadcast list (token-gated; called by the
    dashboard's server-side proxy). Sends a confirmation/welcome email."""
    email = (body.email or "").strip().lower()
    if len(email) > 254 or not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="invalid email")
    token = secrets.token_urlsafe(32)
    conn = _conn_rw(cfg)
    try:
        token, is_new = store.upsert_subscriber(
            conn, email=email, token=token,
            created_at=datetime.now(timezone.utc).isoformat())
    finally:
        conn.close()
    # Only send the welcome on a genuinely NEW subscription — re-POSTing an existing
    # address (a refresh, or an abuse loop) must not re-send mail (Resend quota /
    # reputation / unsolicited-mail vector).
    if cfg.resend_api_key and is_new:
        unsub = f"{cfg.public_base_url}/api/unsubscribe?token={token}"
        background.add_task(_send_welcome, cfg, email, unsub)
    return {"ok": True, "email": email, "new": is_new}


def _unsub_page(message: str, *, body_html: str = "") -> HTMLResponse:
    safe = message  # message is one of our own fixed strings (no user input)
    html_doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>BTC alerts</title>
<style>
  html,body{{margin:0;height:100%;background:#0b0d12;color:#e8eaf0;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
  .box{{max-width:440px;margin:14vh auto 0;padding:32px;background:#14181f;
    border:1px solid rgba(255,255,255,.06);border-radius:16px;text-align:center;
    box-shadow:0 8px 24px rgba(0,0,0,.22)}}
  h1{{font-size:18px;margin:0 0 10px}}
  p{{color:#98a1b2;font-size:14px;line-height:1.5;margin:0 0 16px}}
  button{{background:#e8eaf0;color:#0b0d12;border:0;border-radius:10px;
    padding:11px 22px;font-size:14px;font-weight:600;cursor:pointer}}
</style></head>
<body><div class="box"><h1>BTC signal alerts</h1><p>{safe}</p>{body_html}</div></body></html>"""
    return HTMLResponse(content=html_doc)


def _do_unsubscribe(token: str, cfg: Config) -> str | None:
    if not token:
        return None
    conn = _conn_rw(cfg)
    try:
        return store.deactivate_subscriber(conn, token)
    finally:
        conn.close()


@app.get("/api/unsubscribe", response_class=HTMLResponse)
def unsubscribe_confirm(token: str = Query(""),
                        cfg: Config = Depends(get_config)) -> HTMLResponse:
    """Public confirmation page — does NOT mutate.

    A GET must be side-effect-free: corporate/AV mail link scanners (Outlook
    SafeLinks, Gmail/Yahoo prefetch, Mimecast) issue GETs on every link in an
    email, which previously unsubscribed subscribers the moment a message was
    merely scanned. The actual deactivation happens on the POST below — triggered
    either by the button here or by an RFC 8058 one-click request from the mail
    client. (The token in the URL remains the capability.)
    """
    form = (f'<form method="post" action="/api/unsubscribe?token={token}">'
            f'<button type="submit">Unsubscribe</button></form>') if token else ""
    return _unsub_page(
        "Click below to stop receiving BTC signal alerts at your address."
        if token else "This unsubscribe link is invalid.",
        body_html=form)


@app.post("/api/unsubscribe", response_class=HTMLResponse)
def unsubscribe_do(token: str = Query(""),
                   cfg: Config = Depends(get_config)) -> HTMLResponse:
    """Public (no bearer token) — the unguessable ``token`` is the capability.

    Handles both the confirmation-page button and RFC 8058 one-click POSTs
    (``List-Unsubscribe-Post: List-Unsubscribe=One-Click``) from Gmail/Yahoo.
    Idempotent.
    """
    email = _do_unsubscribe(token, cfg)
    if email:
        return _unsub_page(
            f"You’ve been unsubscribed. {email} will no longer receive alerts.")
    return _unsub_page(
        "This unsubscribe link is invalid or has already been used. "
        "If you keep receiving alerts, reply to one of them.")

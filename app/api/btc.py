"""BTC signal endpoints: long-term accumulation composite, short-term swing,
candles/indicators/derivs, alerts, and the live forward-test / track record.

Read-only: every handler opens the DB read-only (``conn_ro``) so the API can
never corrupt collector writes. Display labels live here as the single
server-side source of truth (the dashboard never re-derives them).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query

from .. import alerting, perf, scoring, shortterm, store
from ..api_deps import conn_ro as _conn
from ..api_deps import get_config, require_token
from ..config import Config
from ..sources import exchange

router = APIRouter()

# The committed calibration JSONs live in app/ (one level up from app/api/).
_APP_DIR = Path(__file__).resolve().parent.parent

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
def _st_winrates() -> dict:
    """Read app/st_winrates.json once (emitted by scripts/st_calibrate.py). {} if absent."""
    try:
        return json.loads((_APP_DIR / "st_winrates.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _trigger_stats(wr: dict, key: str) -> dict:
    """Historical win-rate cell for a trigger key, or an explicit
    {"unmeasured": true} marker when the calibration JSON carries no cell for it
    (funding/OI and order-flow triggers are not replayable from candle history,
    so they can never have one). Serving None would render as blank conviction
    rather than "no coverage". Tolerates both the old and new st_winrates shapes."""
    return wr.get(key) or {"unmeasured": True}


@lru_cache(maxsize=1)
def _track_record_data() -> dict:
    """Read app/track_record.json once (emitted by scripts/calibrate.py). {} if absent."""
    try:
        return json.loads((_APP_DIR / "track_record.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


@router.get("/api/live_performance")
def live_performance(cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    """Forward-tested record of the system's OWN past signals (out-of-sample; grows
    over time). Long-term runs + fired swing alerts priced against stored candles.

    Fetched UNBOUNDED (and 1d candles / alerts are never pruned — see store.prune)
    so the record genuinely covers the full history instead of quietly becoming a
    rolling window that sheds the oldest matured outcomes."""
    conn = _conn(cfg)
    try:
        runs = store.all_runs(conn)
        candles = store.candles_since(conn, "1d")
        st_rows = store.st_alerts_since(conn)
    finally:
        conn.close()
    # Drop the newest (possibly still-forming) daily candle so a provisional
    # close can never price a "matured" outcome.
    candles = candles[:-1] if len(candles) > 1 else candles
    alerts = [{"ts": a.get("ts"), "direction": a.get("direction"), "price": a.get("price"),
               "trigger_key": a.get("trigger_key"), "created_at": a.get("created_at")}
              for a in st_rows]
    return {
        "long_term": perf.long_term_performance(runs, candles),
        "short_term": perf.short_term_performance(alerts, candles),
        "note": ("Forward-tested on the system's live signals as they age — out-of-sample, "
                 "grows over time and covers the full stored history. Long-term "
                 "episodes_effective (episode starts spaced >= the horizon; the CI's "
                 "population) is the honest sample size — raw run and episode counts "
                 "overlap; swing win rates are cost-adjusted and only meaningful "
                 "once each cell has matured outcomes."),
    }


@router.get("/api/track_record")
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

    # Cycle panel: derive from the SAME ATH the run's multiplier actually used —
    # run_once persists it as readings["cycle_ath"] (date/price/source, including
    # the stored-1d-history override). Fall back to the venue price_struct only
    # for runs recorded before cycle_ath existed, then to the config date, so
    # ath_date/days_since_ath/in_window can never contradict the multiplier
    # served beside them.
    cyc_ath = readings.get("cycle_ath") or {}
    ath_d, ath_price, ath_source = cfg.ath_date, ps.get("ath_price"), "config"
    for cand_date, cand_price, cand_src in (
            (cyc_ath.get("date"), cyc_ath.get("price"), cyc_ath.get("source") or "run"),
            (ps.get("ath_date"), ps.get("ath_price"), "venue")):
        if not cand_date:
            continue
        try:
            ath_d = datetime.strptime(cand_date, "%Y-%m-%d").date()
            ath_price, ath_source = cand_price, cand_src
            break
        except (ValueError, TypeError):
            continue
    days_since_ath = (datetime.now(timezone.utc).date() - ath_d).days
    cycle = {
        "ath_date": ath_d.isoformat(),
        "ath_price": ath_price,
        # Which read fed this panel: "stored"/"venue"/"config" as recorded by the
        # run (or "venue"/"config" via the pre-migration fallback path here).
        "ath_source": ath_source,
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
        "note": ("heuristic top-signal — thresholds tuned IN-SAMPLE on the "
                 "2017/2021/2025 cycle tops, the same events scripts/backtest_tops "
                 "then evaluates (1-3 cycles per indicator, one cycle for the "
                 "on-chain members; circular fit, not a proven edge)"),
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


@router.get("/api/longterm/latest")
def longterm_latest(cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    conn = _conn(cfg)
    try:
        latest = store.latest_run(conn)
    finally:
        conn.close()
    if latest:
        latest["breakdown"] = _lt_breakdown(latest, cfg)
    return {"latest": latest}


@router.get("/api/longterm/history")
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


@router.get("/api/playbook")
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


@router.get("/api/price")
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
        # Confluence counts CANDLE triggers only (funding/OI triggers are
        # context-only), mirroring the collector's gate exactly so the dashboard
        # never shows a confluence verdict the alert path wouldn't reach.
        dirs = shortterm.confluence_directions(live_trigs)
        sig["triggers"] = [{
            "key": t.key, "direction": t.direction, "label": t.label, "detail": t.detail,
            "counter_trend": alerting.is_counter_trend(t.direction, state),
            "regime_aligned": shortterm.regime_aligned(t.direction, regime),
            "confluence": shortterm.confluence_ok(
                dirs.count(t.direction), shortterm.regime_aligned(t.direction, regime),
                alerting.is_counter_trend(t.direction, state)),
            "levels": shortterm.trade_levels(t.direction, st_price, atr),
            # historical win-rate + ATR R-expectancy, or {"unmeasured": true}
            "stats": _trigger_stats(wr, t.key),
        } for t in live_trigs]
    except Exception:  # noqa: BLE001 - never 500 the dashboard over a recompute
        pass
    return sig


@router.get("/api/shortterm/latest")
def shortterm_latest(cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    conn = _conn(cfg)
    try:
        derivs_rows = store.recent_derivs(conn, 1)
        funding = derivs_rows[-1]["funding"] if derivs_rows else None
        oi_chg_pct = derivs_rows[-1]["oi_chg_pct"] if derivs_rows else None
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


@router.get("/api/candles")
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


@router.get("/api/indicators")
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


@router.get("/api/derivs")
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


@router.get("/api/alerts")
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

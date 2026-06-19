"""Short-term collector entrypoint (cron */10).

Mirrors run_once's philosophy: a short, idempotent fetch -> store -> score ->
decide -> notify cycle, with SQLite as the only state. Fetches 4h/1d klines plus
funding/OI, upserts the time-series, computes the short-term swing signal per
timeframe, and fires cooldown-debounced alerts on the latest CLOSED candle.

    python -m app.collect_once            # live
    python -m app.collect_once --dry-run  # compute & print; no notify, no DB write
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from . import alerting, flow, notify, shortterm, store
from .config import Config, load_config
from .sources import coinalyze, exchange, price

log = logging.getLogger("btc-collect")

# OI baseline lookback target (~1h ago) and the oldest a baseline may be before it
# is rejected as stale (so a collector outage can't turn a slow bleed into a flush).
_OI_BASELINE_MS = 3600_000
_OI_BASELINE_MAX_AGE_MS = 2 * 3600_000


def _candle_rows(df) -> list[tuple]:
    rows = []
    for r in df.itertuples(index=False):
        rows.append((
            int(r.open_time.timestamp() * 1000),
            float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume),
        ))
    return rows


def _closed(rows: list[dict]) -> list[dict]:
    """Drop the last (possibly still-forming) Coinalyze history bar so flow signals
    evaluate on CLOSED bars only — mirrors the candle path's closed-only doctrine.
    Conservative: at worst this lags one already-closed bar."""
    return rows[:-1] if len(rows) > 1 else rows


def _collect_flow(cfg: Config) -> dict | None:
    """Fetch the Coinalyze order-flow series for the PRIMARY short-term timeframe
    and reduce them to (cvd frame, participant read, liquidation flush, readings).

    Computed once per run on the first ST timeframe (order flow lives on the
    shorter horizon); the triggers are attached to that timeframe so they share
    the existing confluence/cooldown machinery. None when the layer is inactive or
    every series came back empty (the collector then runs exactly as before).
    """
    if not cfg.coinalyze_active:
        return None
    tf = cfg.st_timeframes[0]
    interval = coinalyze.INTERVAL_MAP.get(tf)
    if interval is None:
        return None
    ih = coinalyze.INTERVAL_HOURS.get(interval, 4)
    hours = (cfg.flow_cvd_lookback + 5) * ih  # +5 bars of slack over the divergence window
    key, sym = cfg.coinalyze_api_key, cfg.coinalyze_symbol

    cvd_rows = _closed(coinalyze.ohlcv_history(sym, interval, hours, key))
    oi_rows = _closed(coinalyze.oi_history(sym, interval, hours, key))
    liq_rows = _closed(coinalyze.liquidations_history(sym, interval, hours, key))
    if not cvd_rows and not liq_rows:
        log.warning("Coinalyze key set but flow series empty (check symbol %r / plan); "
                    "layer dark this run", sym)
        return None

    # Staleness gate: if the latest closed bar is far older than the interval, the
    # feed gapped — every read (divergence / participant / flush) would be
    # unreliable, so the whole layer goes dark. Mirrors the OI-baseline recency
    # doctrine that stops a gap from reading as a phantom flush.
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    interval_ms = int(ih * 3600_000)
    last_tss = [rows[-1]["ts"] for rows in (cvd_rows, oi_rows, liq_rows) if rows]
    if last_tss and now_ms - max(last_tss) > 3 * interval_ms:
        log.warning("Coinalyze flow data stale (last bar %.1fh old); layer dark this run",
                    (now_ms - max(last_tss)) / 3600_000)
        return None

    cvd_df = flow.build_cvd(cvd_rows)
    part = flow.participant_aligned(cvd_rows, oi_rows, cfg.flow_oi_bar_surge_pct)
    liq_flush = flow.liquidation_flush(liq_rows, cfg.flow_liq_spike_mult, cfg.flow_liq_min_usd)
    div = flow.cvd_divergence(cvd_df, cfg.flow_cvd_lookback)
    readings = {
        "source": "coinalyze", "symbol": sym, "interval": interval,
        "cvd": float(cvd_df["cvd"].iloc[-1]) if not cvd_df.empty else None,
        "cvd_delta_last": float(cvd_df["delta"].iloc[-1]) if not cvd_df.empty else None,
        "cvd_divergence": div,
        "participant": part,
        "liq_long_usd": liq_rows[-1]["long"] if liq_rows else None,
        "liq_short_usd": liq_rows[-1]["short"] if liq_rows else None,
        "liq_flush": (liq_flush[0] if liq_flush else None),
    }
    return {"tf": tf, "cvd_df": cvd_df, "participant": part,
            "liq_flush": liq_flush, "readings": readings}


def run(cfg: Config, *, dry_run: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    conn = store.connect(cfg.db_path)
    store.init_db(conn)

    frames = price.get_intraday_frames(cfg.symbol, cfg.st_timeframes, prefer=cfg.exchange)

    # 200-day macro regime (price vs 200DMA) — context for triggers; optionally
    # suppresses counter-regime alerts (ST_REGIME_SUPPRESS).
    daily_df = frames.get("1d")
    regime = shortterm.current_regime(
        exchange.closed_only(daily_df)["close"] if daily_df is not None and not daily_df.empty else None)

    # Derivatives (best-effort) -> derivs time-series + OI change over ~1h. The
    # baseline is timestamp-bounded (target ~1h ago, rejected if older than ~2h) so
    # a gap after a collector outage can't read a slow bleed as a phantom flush.
    funding = exchange.funding_latest(cfg.symbol)
    oi = exchange.open_interest(cfg.symbol)
    oi_chg_pct = None
    now_ms = int(now.timestamp() * 1000)
    if oi is not None:
        base = store.oi_at_or_before(conn, now_ms - _OI_BASELINE_MS,
                                     not_before_ms=now_ms - _OI_BASELINE_MAX_AGE_MS)
        if base:
            oi_chg_pct = (oi / base - 1.0) * 100.0
    if not dry_run:
        store.record_derivs(conn, ts=now_ms, funding=funding, oi=oi, oi_chg_pct=oi_chg_pct)

    # Free aggregated order-flow layer (Coinalyze): CVD divergence, OI participant
    # and liquidation flush on the primary ST timeframe. None when no key is set.
    flow_data = _collect_flow(cfg)

    summary = {"now": now.isoformat(), "funding": funding, "oi": oi,
               "oi_chg_pct": oi_chg_pct,
               "flow": (flow_data["readings"] if flow_data else None),
               "timeframes": {}, "alerts": []}

    # Collect every eligible (passes suppression + confluence + cooldown) trigger
    # across all timeframes, then send ONE batched email per direction below.
    eligible: list[dict] = []
    for tf, df in frames.items():
        if not dry_run:
            store.upsert_candles(conn, tf, _candle_rows(df),
                                 source=df.attrs.get("source"))

        ev = shortterm.evaluate(df, cfg, funding=funding, oi_chg_pct=oi_chg_pct)
        ev_ts = ev.get("ts")
        if ev_ts is None:
            log.info("%s: insufficient candles for a signal yet", tf)
            continue

        # Attach the Coinalyze order-flow layer to its (primary) timeframe: merge
        # the readings into the stored indicators and fold the flow triggers into
        # the same confluence/cooldown loop the candle triggers already use.
        if flow_data and tf == flow_data["tf"]:
            ev["indicators"]["flow"] = flow_data["readings"]
            ev["triggers"] = list(ev["triggers"]) + flow.detect_flow_triggers(
                flow_data["cvd_df"], flow_data["participant"],
                flow_data["liq_flush"], cfg)

        if not dry_run:
            store.record_st_signal(conn, ts=ev_ts, timeframe=tf, price=ev["price"],
                                   st_score=ev["score"], st_state=ev["state"],
                                   indicators=ev["indicators"])

        passed = []
        dirs = [t.direction for t in ev["triggers"]]
        for trig in ev["triggers"]:
            if cfg.st_regime_suppress and shortterm.regime_aligned(trig.direction, regime) is False:
                log.info("%s/%s suppressed (counter-%s-regime)", tf, trig.key, regime)
                continue
            if cfg.st_require_confluence and not shortterm.confluence_ok(
                    dirs.count(trig.direction),
                    shortterm.regime_aligned(trig.direction, regime),
                    alerting.is_counter_trend(trig.direction, ev["state"])):
                log.info("%s/%s suppressed (no confluence)", tf, trig.key)
                continue
            last = store.last_st_alert(conn, trig.key, tf)
            if not alerting.decide_st_alert(candle_ts=ev_ts, last_alert=last, now=now,
                                            cooldown_hours=cfg.st_cooldown_hours):
                continue
            eligible.append({"trigger": trig, "timeframe": tf, "ts": ev_ts,
                             "score": ev["score"], "state": ev["state"],
                             "price": ev["price"], "indicators": ev["indicators"],
                             "regime": regime})
            passed.append(trig.key)

        log.info("%s: score=%.1f state=%s triggers=%s eligible=%s",
                 tf, ev["score"], ev["state"], [t.key for t in ev["triggers"]], passed)
        summary["timeframes"][tf] = {"score": ev["score"], "state": ev["state"],
                                     "triggers": [t.key for t in ev["triggers"]],
                                     "eligible": passed}

    # One email per direction. Record each trigger's cooldown row ONLY on a
    # successful send, so a failed send is retried next run (see store.last_st_alert,
    # which counts sent=1 rows only) rather than silently swallowed + cooled down.
    fired: list[str] = []
    for direction in ("BUY", "SELL"):
        items = [e for e in eligible if e["trigger"].direction == direction]
        if not items:
            continue
        title, body = alerting.build_st_batch_message(items, direction)
        if dry_run:
            log.info("[dry-run] ST ALERT %s x%d\n%s\n%s", direction, len(items), title, body)
            fired.extend(f"{e['timeframe']}/{e['trigger'].key}" for e in items)
            continue
        # Owner-only (no `conn` -> no subscriber broadcast): swing triggers are
        # frequent; subscribers only get the infrequent long-term tier/flash alerts.
        ok = notify.send(cfg, title, body)
        if not ok:
            log.warning("ST %s batch (%d triggers) send failed; will retry next run",
                        direction, len(items))
            continue
        for e in items:
            store.record_st_alert(conn, ts=e["ts"], created_at=now.isoformat(),
                                  trigger_key=e["trigger"].key, timeframe=e["timeframe"],
                                  direction=direction, price=e["price"],
                                  message=body, sent=True)
            fired.append(f"{e['timeframe']}/{e['trigger'].key}")

    summary["alerts"] = fired
    if not dry_run:
        store.prune(conn, 400)
    conn.close()
    return summary


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="BTC short-term collector (one run).")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute and print; do not send notifications or write the ledger")
    args = parser.parse_args(argv)

    cfg = load_config()
    try:
        run(cfg, dry_run=args.dry_run)
        return 0
    except Exception:  # noqa: BLE001 - clean exit code for cron
        log.exception("collect run failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())

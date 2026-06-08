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

from . import alerting, notify, shortterm, store
from .config import Config, load_config
from .sources import exchange, price

log = logging.getLogger("btc-collect")

# OI baseline lookback: ~the sample stored ~1h ago (6 x 10-min samples).
_OI_BASELINE_SAMPLES = 6


def _candle_rows(df) -> list[tuple]:
    rows = []
    for r in df.itertuples(index=False):
        rows.append((
            int(r.open_time.timestamp() * 1000),
            float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume),
        ))
    return rows


def run(cfg: Config, *, dry_run: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    conn = store.connect(cfg.db_path)
    store.init_db(conn)

    frames = price.get_intraday_frames(cfg.symbol, cfg.st_timeframes, prefer=cfg.exchange)

    # Derivatives (best-effort) -> derivs time-series + OI change over ~1h.
    funding = exchange.funding_latest(cfg.symbol)
    oi = exchange.open_interest(cfg.symbol)
    oi_chg_pct = None
    hist = store.recent_derivs(conn, _OI_BASELINE_SAMPLES)
    if oi is not None and hist:
        base = hist[0].get("oi")
        if base:
            oi_chg_pct = (oi / base - 1.0) * 100.0
    now_ms = int(now.timestamp() * 1000)
    if not dry_run:
        store.record_derivs(conn, ts=now_ms, funding=funding, oi=oi, oi_chg_pct=oi_chg_pct)

    summary = {"now": now.isoformat(), "funding": funding, "oi": oi,
               "oi_chg_pct": oi_chg_pct, "timeframes": {}, "alerts": []}

    for tf, df in frames.items():
        if not dry_run:
            store.upsert_candles(conn, tf, _candle_rows(df))

        ev = shortterm.evaluate(df, cfg, funding=funding, oi_chg_pct=oi_chg_pct)
        ev_ts = ev.get("ts")
        if ev_ts is None:
            log.info("%s: insufficient candles for a signal yet", tf)
            continue

        if not dry_run:
            store.record_st_signal(conn, ts=ev_ts, timeframe=tf, price=ev["price"],
                                   st_score=ev["score"], st_state=ev["state"],
                                   indicators=ev["indicators"])

        fired = []
        for trig in ev["triggers"]:
            last = store.last_st_alert(conn, trig.key, tf)
            if not alerting.decide_st_alert(candle_ts=ev_ts, last_alert=last, now=now,
                                            cooldown_hours=cfg.st_cooldown_hours):
                continue
            title, body = alerting.build_st_message(
                trigger=trig, timeframe=tf, score=ev["score"], state=ev["state"],
                price=ev["price"], indicators=ev["indicators"])
            if dry_run:
                log.info("[dry-run] ST ALERT %s/%s\n%s\n%s", tf, trig.key, title, body)
            else:
                notify.send(cfg, title, body, conn=conn)
                store.record_st_alert(conn, ts=ev_ts, created_at=now.isoformat(),
                                      trigger_key=trig.key, timeframe=tf,
                                      direction=trig.direction, price=ev["price"],
                                      message=body, sent=True)
            fired.append(trig.key)

        log.info("%s: score=%.1f state=%s triggers=%s fired=%s",
                 tf, ev["score"], ev["state"], [t.key for t in ev["triggers"]], fired)
        summary["timeframes"][tf] = {"score": ev["score"], "state": ev["state"],
                                     "triggers": [t.key for t in ev["triggers"]],
                                     "fired": fired}
        summary["alerts"].extend(fired)

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

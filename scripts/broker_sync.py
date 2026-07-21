"""Live Alpaca paper-broker sync — the intraday driver of the ``@broker`` track.

Cadence: ~every 15 min during US market hours + once after close (Task Scheduler
/ cron). Submits day-limit orders for new PENDING intents, reconciles async
fills, and marks the ``@broker`` NAV from real account equity. Writes via
``store.connect`` (WAL) exactly like the collectors — no API write path is added.

No-op (exit 0) unless ``BROKER_PAPER_ENABLED=true`` and the Alpaca keys are set,
so the box behaves exactly as before until the owner opts in.

    python -m scripts.broker_sync
"""
from __future__ import annotations

import logging
import sqlite3

from app import store
from app.config import load_config
from app.harness import schema
from app.portfolio import broker

log = logging.getLogger("broker_sync")


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    if not cfg.broker_active:
        log.info("broker track disabled (BROKER_PAPER_ENABLED / Alpaca keys) — nothing to do")
        return 0

    from app.data.equities import prices as eq_prices
    from app.data_lake import Lake

    lake = Lake(cfg.data_lake_path)
    conn = store.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    schema.init_harness_db(conn)
    try:
        api = broker.AlpacaPaper(cfg.alpaca_api_key, cfg.alpaca_secret_key,
                                 base=cfg.broker_base_url)
        tickers = [r[0] for r in conn.execute(
            "SELECT DISTINCT ticker FROM paper_positions "
            "WHERE status='PENDING' AND ticker IS NOT NULL")]
        ref_px: dict[str, float] = {}
        adv_bars: dict[str, list[dict]] = {}
        if tickers:
            for t, bl in eq_prices.sep_bars_bulk(lake, tickers, limit=60).items():
                if bl:
                    adv_bars[t] = bl
                    ref_px[t] = bl[-1]["close"]
        spy = eq_prices.sep_bars_bulk(lake, ["SPY"], limit=5000,
                                      table="sfp").get("SPY", [])
        sub = broker.submit_pending(conn, cfg, api, ref_px=ref_px, adv_bars=adv_bars)
        rec = broker.reconcile(conn, cfg, api)
        nav = broker.mark_broker_nav(conn, cfg, api, spy)
        log.info("broker sync: submit=%s reconcile=%s nav_rows=%d", sub, rec, nav)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

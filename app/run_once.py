"""Entrypoint: fetch all available data -> score -> decide -> notify -> persist.

Short, idempotent, crash-resistant. Designed to be run by cron every 6h. Crypto
trades 24/7 so there is no market-hours logic. Run with:

    python -m app.run_once            # live: score, alert on changes, persist
    python -m app.run_once --dry-run  # compute & print only; no notify, no DB write
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from . import alerting, notify, scoring, store
from .config import Config, load_config
from .sources import derivatives, etf_flows, funding, macro, onchain, price, sentiment

log = logging.getLogger("btc-accum")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def gather_readings(cfg: Config) -> tuple[dict, dict]:
    """Fetch every available source. Returns (readings, price_struct).

    ``readings`` holds scorer-keyed indicator values (None where unavailable).
    Price is mandatory; everything else degrades to None on absence/failure.
    """
    price_struct = price.price_structure(cfg.symbol)  # mandatory; may raise

    readings: dict[str, float | None] = {
        # price structure
        "price_to_wma200": price_struct.get("price_to_wma200"),
        "mayer": price_struct.get("mayer_multiple"),
        "drop_24_48h_pct": price_struct.get("drop_24_48h_pct"),
    }
    readings.update(funding.funding_7d_avg(cfg.symbol))   # funding
    readings.update(sentiment.fear_greed())               # fng
    readings.update(macro.macro())                        # m2_yoy, hy_spread, real_yield, (dgs10/dxy ctx)
    readings.update(etf_flows.etf_flows())                # etf_flow
    readings.update(onchain.onchain(price_struct.get("price")))  # mvrv_z, realized_ratio, nupl, sopr, puell
    readings.update(derivatives.derivatives())            # liq_magnitude, oi_flush
    return readings, price_struct


def run(cfg: Config, *, dry_run: bool = False) -> dict:
    """Execute one full cycle. Returns a summary dict (also used by tests/CLI)."""
    now = datetime.now(timezone.utc)
    run_ts = now.isoformat()

    readings, price_struct = gather_readings(cfg)

    # SQLite is the only state; open it now — used both to derive the free
    # oi_flush below (before scoring) and for the ledger reads/writes later.
    conn = store.connect(cfg.db_path)
    store.init_db(conn)

    # Free long-term oi_flush: % OI change over a window from the OKX open-interest
    # the short-term collector already stores. Only when no paid Coinglass value
    # was produced (don't clobber it). Needs ~1 window of collector history.
    if readings.get("oi_flush") is None:
        now_ms = int(now.timestamp() * 1000)
        base = store.oi_at_or_before(conn, now_ms - int(cfg.oi_flush_window_hours * 3600_000))
        cur = store.latest_oi(conn)
        if base and cur:
            readings["oi_flush"] = (cur / base - 1.0) * 100.0

    # Score.
    subscores = scoring.score_indicators(readings)
    cat_scores = scoring.category_scores(subscores)
    mult = scoring.cycle_multiplier(now.date(), cfg.ath_date, cfg.peak_to_trough_days)
    composite_score, active_cats = scoring.composite(cat_scores, cfg.weights, mult)
    current_tier = scoring.tier(
        composite_score, price_struct["price"], price_struct.get("wma200"),
        cfg.tier_watch, cfg.tier_accumulate, cfg.tier_deepvalue,
    )

    # Decide (needs ledger state).
    prev_tier = store.last_tier(conn)
    prev_flash_at = store.last_flash_at(conn)
    # Fresh acute funding/OI from the short-term collector (≤10min old) so the
    # capitulation flash is responsive on the free tier — see evaluate_flash.
    latest_derivs = store.recent_derivs(conn, 1)
    acute = latest_derivs[-1] if latest_derivs else {}
    flash_now = alerting.evaluate_flash(
        readings, cfg,
        acute_funding=acute.get("funding"),
        acute_oi_chg_pct=acute.get("oi_chg_pct"),
    )
    decisions = alerting.decide_alerts(
        current_tier, prev_tier, flash_now, prev_flash_at, cfg.flash_debounce_days, now
    )

    log.info(
        "composite=%.1f tier=%s (was %s) active=%s mult=%.3f flash_now=%s decisions=%s",
        composite_score, current_tier, prev_tier, ",".join(active_cats), mult,
        flash_now, decisions,
    )

    # Notify.
    msg_kwargs = dict(composite=composite_score, tier=current_tier, subscores=subscores,
                      price_struct=price_struct, readings=readings, active_cats=active_cats,
                      onchain_active=cfg.onchain_active)
    if decisions["tier_alert"]:
        title, body = alerting.build_tier_message(**msg_kwargs)
        if dry_run:
            log.info("[dry-run] TIER ALERT\n%s\n%s", title, body)
        else:
            notify.send(cfg, title, body, conn=conn)
    if decisions["flash_alert"]:
        title, body = alerting.build_flash_message(**msg_kwargs)
        if dry_run:
            log.info("[dry-run] FLASH ALERT\n%s\n%s", title, body)
        else:
            notify.send(cfg, title, body, conn=conn)

    # Persist (full readings + sub-scores + category scores for later calibration).
    record = {
        "raw": readings,
        "price_struct": price_struct,
        "subscores": subscores,
        "category_scores": cat_scores,
        "cycle_multiplier": mult,
    }
    if not dry_run:
        store.record_run(
            conn,
            run_ts=run_ts,
            price=price_struct.get("price"),
            composite=composite_score,
            tier=current_tier,
            active_cats=active_cats,
            readings=record,
            tier_alerted=decisions["tier_alert"],
            flash_alerted=decisions["flash_alert"],
        )
    conn.close()

    return {
        "run_ts": run_ts,
        "composite": composite_score,
        "tier": current_tier,
        "prev_tier": prev_tier,
        "active_cats": active_cats,
        "cycle_multiplier": mult,
        "flash_now": flash_now,
        "decisions": decisions,
        "subscores": subscores,
        "category_scores": cat_scores,
        "price_struct": price_struct,
    }


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(description="BTC accumulation-zone notifier (one run).")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute and print; do not send notifications or write the ledger")
    args = parser.parse_args(argv)

    cfg = load_config()
    try:
        run(cfg, dry_run=args.dry_run)
        return 0
    except Exception:  # noqa: BLE001 - top-level guard so cron gets a clean exit code
        log.exception("run failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())

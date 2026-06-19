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

from . import alerting, notify, playbook, scoring, store
from .config import Config, load_config
from .sources import (derivatives, etf_flows, funding, macro, miner, onchain,
                      price, sentiment, stablecoins)

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
    readings.update(stablecoins.ssr())                    # ssr (crypto dry-powder)
    readings.update(onchain.onchain(price_struct.get("price")))  # mvrv_z, realized_ratio, nupl, sopr, puell, reserve_risk
    readings.update(miner.hash_ribbon())                  # hash_ribbon (miner capitulation->recovery)
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
        window_ms = int(cfg.oi_flush_window_hours * 3600_000)
        # Floor the baseline age at one extra window below the target so a stale
        # sample (collector outage) can't turn a slow bleed into a phantom flush.
        base = store.oi_at_or_before(conn, now_ms - window_ms,
                                     not_before_ms=now_ms - 2 * window_ms)
        cur = store.latest_oi(conn)
        if base and cur:
            readings["oi_flush"] = (cur / base - 1.0) * 100.0

    # Score.
    subscores = scoring.score_indicators(readings)
    cat_scores = scoring.category_scores(subscores)
    # Cycle timing keys off the ATH derived from price history (config date is a
    # fallback only), with a soft, config-tunable swing. The CoinGecko fallback only
    # spans 365 days, so its "ATH" is a 1-year max, not a cycle top — do NOT let it
    # override the config date (that would mistime the cycle multiplier whenever the
    # real top is >365d old). Trust only multi-year sources (exchange/coinbase).
    ath = cfg.ath_date
    ath_iso = price_struct.get("ath_date")
    if ath_iso and price_struct.get("source") != "coingecko":
        try:
            ath = datetime.strptime(ath_iso, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            pass
    mult = scoring.cycle_multiplier(now.date(), ath, cfg.peak_to_trough_days,
                                    swing=cfg.cycle_mult_swing)
    composite_score, active_cats = scoring.composite(cat_scores, cfg.weights, mult)
    # Hysteresis: a tier change must clear the threshold by a margin so a composite
    # hovering on a cutoff doesn't whipsaw the tier (and spam alerts). Keyed off the
    # last *computed* tier (display/state continuity), not the alert cursor.
    prev_tier = store.last_tier(conn)
    prev_active_cats = store.last_active_cats(conn)
    current_tier = scoring.tier_hysteresis(
        composite_score, price_struct["price"], price_struct.get("wma200"),
        prev_tier, cfg.tier_watch, cfg.tier_accumulate, cfg.tier_deepvalue,
        margin=cfg.tier_hysteresis_margin)
    # Confidence proxy: how much the active categories agree.
    agreement = scoring.category_agreement(cat_scores)

    # Sell-side overheat: score + hysteresis band (band continuity comes from the
    # previous run's stored froth block, mirroring tier hysteresis).
    froth = scoring.froth_score(readings)
    froth["band"] = scoring.froth_band(froth["score"], store.last_froth_band(conn))

    # Decide (needs ledger state). The tier decision compares against the last
    # SUCCESSFULLY NOTIFIED tier so a failed send is retried, not lost.
    prev_notified_tier = store.last_notified_tier(conn)
    prev_flash_at = store.last_flash_at(conn)
    # Fresh acute funding/OI from the short-term collector (≤10min old) so the
    # capitulation flash is responsive on the free tier — see evaluate_flash. Guard
    # against a STALE acute row (collector down): an hours-old funding/OI reading is
    # not an "acute" capitulation leg, so ignore it past a freshness window.
    latest_derivs = store.recent_derivs(conn, 1)
    acute = latest_derivs[-1] if latest_derivs else {}
    acute_age_ms = (int(now.timestamp() * 1000) - acute["ts"]) if acute.get("ts") else None
    acute_fresh = acute_age_ms is not None and acute_age_ms <= 30 * 60 * 1000  # 30 min
    flash_now = alerting.evaluate_flash(
        readings, cfg,
        acute_funding=acute.get("funding") if acute_fresh else None,
        acute_oi_chg_pct=acute.get("oi_chg_pct") if acute_fresh else None,
    )
    decisions = alerting.decide_alerts(
        current_tier, prev_notified_tier, flash_now, prev_flash_at,
        cfg.flash_debounce_days, now,
        prev_active_cats=prev_active_cats, active_cats=active_cats,
    )

    log.info(
        "composite=%.1f tier=%s (was %s, notified %s) active=%s mult=%.3f flash_now=%s decisions=%s",
        composite_score, current_tier, prev_tier, prev_notified_tier, ",".join(active_cats),
        mult, flash_now, decisions,
    )

    # Playbook (illustrative, display-only): conviction-scaled ladder, unified
    # "what to do now", and "what changed since the last alert".
    conv = playbook.conviction(composite_score, current_tier,
                               cfg.tier_watch, cfg.tier_accumulate, cfg.tier_deepvalue)
    rr = readings.get("realized_ratio")
    realized_price = (price_struct["price"] / rr) if (rr and price_struct.get("price")) else None
    plan = playbook.laddering_plan(
        composite=composite_score, tier=current_tier, conviction_=conv,
        price=price_struct.get("price"), wma200=price_struct.get("wma200"),
        realized_price=realized_price, atr_daily=None)
    st_sig = store.latest_st_signal(conn)
    what_to_do = playbook.what_to_do_now(
        long_tier=current_tier, long_conviction=conv,
        st_state=(st_sig or {}).get("st_state"), st_triggers=[])
    prev_alert = store.last_alerted_run(conn)
    prev_for_diff = ({"composite": prev_alert["composite"], "tier": prev_alert["tier"],
                      "subscores": (prev_alert.get("readings") or {}).get("subscores"),
                      "run_ts": prev_alert["run_ts"]} if prev_alert else None)
    changed = alerting.diff_since(
        prev_for_diff, {"composite": composite_score, "tier": current_tier, "subscores": subscores})

    # Notify. Capture each send's success so the ledger only advances the alert
    # cursors (notified_tier / flash_alerted) when the user was actually reached —
    # a failed send is retried next run instead of being silently swallowed.
    msg_kwargs = dict(composite=composite_score, tier=current_tier, subscores=subscores,
                      price_struct=price_struct, readings=readings, active_cats=active_cats,
                      onchain_active=cfg.onchain_active,
                      changed=changed, what_to_do=what_to_do, plan=plan)
    tier_send_ok = True   # True when nothing needed sending (cursor may advance freely)
    if decisions["tier_alert"]:
        title, body = alerting.build_tier_message(cats_changed=decisions["cats_changed"], **msg_kwargs)
        if dry_run:
            log.info("[dry-run] TIER ALERT\n%s\n%s", title, body)
        else:
            tier_send_ok = notify.send(cfg, title, body, conn=conn)
    elif decisions["exit_alert"]:
        title, body = alerting.build_exit_message(prev_tier=prev_notified_tier,
                                                  cats_changed=decisions["cats_changed"], **msg_kwargs)
        if dry_run:
            log.info("[dry-run] EXIT ALERT\n%s\n%s", title, body)
        else:
            tier_send_ok = notify.send(cfg, title, body, conn=conn)

    flash_send_ok = False
    if decisions["flash_alert"]:
        title, body = alerting.build_flash_message(**msg_kwargs)
        if dry_run:
            log.info("[dry-run] FLASH ALERT\n%s\n%s", title, body)
            flash_send_ok = True
        else:
            flash_send_ok = notify.send(cfg, title, body, conn=conn)

    # Sell-side overheat crossing (OWNER-ONLY: no conn => no subscriber
    # broadcast — the froth side is a small-sample heuristic, not the product).
    prev_notified_froth = store.last_notified_froth_band(conn)
    froth_alert = alerting.decide_froth_alert(froth["band"], prev_notified_froth)
    froth_send_ok = True
    if froth_alert:
        title, body = alerting.build_froth_message(
            froth=froth, band=froth["band"], price=price_struct.get("price"),
            composite=composite_score, tier=current_tier)
        if dry_run:
            log.info("[dry-run] FROTH ALERT\n%s\n%s", title, body)
        else:
            froth_send_ok = notify.send(cfg, title, body)
    # Cursor semantics (incl. the oscillation debounce) live in next_froth_cursor.
    notified_froth_band = alerting.next_froth_cursor(
        froth["band"], prev_notified_froth, froth_alert, froth_send_ok)

    # Alert cursors. notified_tier advances to the current tier only if any needed
    # tier/exit alert was delivered; otherwise it holds so the next run retries.
    tier_communicated = decisions["tier_alert"] or decisions["exit_alert"]
    notified_tier = current_tier if (not tier_communicated or tier_send_ok) else prev_notified_tier
    # flash_alerted records a DELIVERED flash only, so the debounce isn't started by
    # a flash nobody received.
    flash_recorded = decisions["flash_alert"] and flash_send_ok
    tier_recorded = tier_communicated and tier_send_ok

    # Persist (full readings + sub-scores + category scores for later calibration).
    record = {
        "raw": readings,
        "price_struct": price_struct,
        "subscores": subscores,
        "category_scores": cat_scores,
        "cycle_multiplier": mult,
        "conviction": conv,
        "agreement": agreement,
        "froth": froth,
        "playbook": plan,
        "what_to_do": what_to_do,
        "changed": changed,
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
            tier_alerted=tier_recorded,
            flash_alerted=flash_recorded,
            notified_tier=notified_tier,
            froth=froth.get("score"),
            notified_froth_band=notified_froth_band,
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
        "froth": froth,
        "froth_alert": froth_alert,
        "decisions": decisions,
        "subscores": subscores,
        "category_scores": cat_scores,
        "price_struct": price_struct,
        "conviction": conv,
        "agreement": agreement,
        "playbook": plan,
        "what_to_do": what_to_do,
        "changed": changed,
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

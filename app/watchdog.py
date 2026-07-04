"""Dead-man's-switch.

Emails if ANY pipeline has gone stale — so a broken pipeline (e.g. an exchange
API change) never silently masquerades as "quiet market, no signal". Crucially the
pipelines are checked SEPARATELY against their own cadence: the 10-min collector
keeps the DB "fresh" essentially always, so checking only the most-recent activity
would let the 6h long-term run die for weeks unnoticed (the headline product).

  * collector (collect_once, */10):        stale after WATCHDOG_STALE_HOURS (default 3h)
  * long-term (run_once, every 6h):         stale after ~2 cadences + slack (13h)
  * stock swing (stock_collect, daily):     stale after ~50h  (~2 cadences + slack)
  * stock long-term (stock_lt_collect, wk): stale after ~200h (~1 cadence  + slack)

The two stock pipelines are OPTIONAL layers — deployed only when their API keys are
set. A stock pipeline that has NEVER produced a run means "not enabled on this box",
not "dead", so each is watched only once it has recorded at least one run; a
previously-live pipeline going dark still alerts. (Both record a run row on every
cron invocation, even a closed-market day, so the thresholds track wall-clock.)

Re-alerts are DEBOUNCED with escalation (first hit, then ~6h, then daily) so a
weekend outage doesn't produce ~48 identical emails. Sent over ALL configured
transports, since the breakage may be the channel the pipeline alerts on.

NOTE: detection latency is bounded by the watchdog's own cron cadence, so run it
at least as often as WATCHDOG_STALE_HOURS (e.g. hourly for a 3h threshold).

    python -m app.watchdog
    python -m app.watchdog --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from . import notify, stock_lt_store, stock_store, store
from .config import Config, load_config

log = logging.getLogger("btc-watchdog")

# Long-term run cadence is 6h; flag only after ~2 missed cadences + slack.
RUN_STALE_HOURS = 13.0
# Optional stock pipelines (watched only once each has recorded a run — see module docstring).
STOCK_SWING_STALE_HOURS = 50.0    # daily cron; ~2 missed cadences + slack
STOCK_LT_STALE_HOURS = 200.0      # weekly cron; ~1 missed cadence + slack
# Debounce: don't re-alert until this many hours since the last watchdog alert.
_REALERT_AFTER_HOURS = 6.0
_META_KEY = "watchdog_last_alert"


def check(cfg: Config, *, dry_run: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    conn = store.connect(cfg.db_path)
    store.init_db(conn)
    # Ensure the optional stock schemas exist so their "last run" reads return
    # None (not-enabled) instead of raising on a box that never ran them.
    stock_store.init_stock_db(conn)
    stock_lt_store.init_stock_lt_db(conn)

    last_collect = store.last_collect_ts(conn)
    last_run = store.last_run_ts(conn)
    last_stock = stock_store.last_stock_run_ts(conn)
    last_lt = stock_lt_store.last_lt_run_ts(conn)

    def _age_h(t):
        return (now - t).total_seconds() / 3600.0 if t is not None else None

    collect_age = _age_h(last_collect)
    run_age = _age_h(last_run)
    stock_age = _age_h(last_stock)
    lt_age = _age_h(last_lt)
    collect_stale = collect_age is None or collect_age > cfg.watchdog_stale_hours
    run_stale = run_age is None or run_age > RUN_STALE_HOURS
    # Optional layers: "stale" only once seen at least once (never-run == not enabled).
    stock_stale = last_stock is not None and stock_age > STOCK_SWING_STALE_HOURS
    lt_stale = last_lt is not None and lt_age > STOCK_LT_STALE_HOURS
    stale = collect_stale or run_stale or stock_stale or lt_stale

    result = {
        "last_collect": last_collect.isoformat() if last_collect else None,
        "last_run": last_run.isoformat() if last_run else None,
        "last_stock_run": last_stock.isoformat() if last_stock else None,
        "last_lt_run": last_lt.isoformat() if last_lt else None,
        "collect_age_hours": collect_age, "run_age_hours": run_age,
        "stock_age_hours": stock_age, "lt_age_hours": lt_age,
        "collect_stale": collect_stale, "run_stale": run_stale,
        "stock_stale": stock_stale, "lt_stale": lt_stale,
        "stale": stale, "alerted": False,
    }

    if not stale:
        log.info("watchdog: healthy (collect %.1fh, run %.1fh, stock %s, lt %s)",
                 collect_age or 0.0, run_age or 0.0,
                 f"{stock_age:.1f}h" if stock_age is not None else "n/a",
                 f"{lt_age:.1f}h" if lt_age is not None else "n/a")
        conn.close()
        return result

    # Debounce repeats while the condition persists.
    last_alert_raw = store.get_meta(conn, _META_KEY)
    should_alert = True
    if last_alert_raw:
        try:
            since_h = (now - datetime.fromisoformat(last_alert_raw)).total_seconds() / 3600.0
            should_alert = since_h >= _REALERT_AFTER_HOURS
        except ValueError:
            should_alert = True

    def _when(age):
        return f"{age:.1f}h ago" if age is not None else "never"

    problems = []
    if collect_stale:
        problems.append(f"BTC short-term collector (last {_when(collect_age)}, "
                        f"threshold {cfg.watchdog_stale_hours:.0f}h)")
    if run_stale:
        problems.append(f"BTC long-term run (last {_when(run_age)}, "
                        f"threshold {RUN_STALE_HOURS:.0f}h)")
    if stock_stale:
        problems.append(f"stock swing collector (last {_when(stock_age)}, "
                        f"threshold {STOCK_SWING_STALE_HOURS:.0f}h)")
    if lt_stale:
        problems.append(f"stock long-term run (last {_when(lt_age)}, "
                        f"threshold {STOCK_LT_STALE_HOURS:.0f}h)")
    title = "Signal system WATCHDOG: pipeline stale"
    body = (
        "A pipeline has stopped producing data:\n  - " + "\n  - ".join(problems) + "\n\n"
        "'No alerts' right now does NOT mean 'quiet market' — it likely means the "
        "pipeline stopped (an exchange API change, a crashed service, or a host issue).\n\n"
        "Check: logs, `systemctl status btc-api btc-dashboard`, cron, and the exchange endpoints."
    )

    if dry_run:
        log.info("[dry-run] WATCHDOG would alert (should_alert=%s):\n%s\n%s",
                 should_alert, title, body)
        result["alerted"] = should_alert
    elif should_alert:
        # Send over every configured transport (the broken piece may be one of them).
        sent = notify.send(cfg, title, body, severity="FAIL")   # §8 instant tier
        if not sent:
            log.error("watchdog: STALE but NO transport delivered the alert")
        store.set_meta(conn, _META_KEY, now.isoformat())
        result["alerted"] = True
    else:
        log.warning("watchdog: STALE but within debounce window; not re-alerting")
    log.warning("watchdog: STALE (%s)", "; ".join(problems))
    conn.close()
    return result


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="BTC notifier dead-man's-switch.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    cfg = load_config()
    try:
        check(cfg, dry_run=args.dry_run)
        return 0
    except Exception:  # noqa: BLE001
        log.exception("watchdog failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())

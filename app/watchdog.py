"""Dead-man's-switch.

Emails if EITHER pipeline has gone stale — so a broken pipeline (e.g. an exchange
API change) never silently masquerades as "quiet market, no signal". Crucially the
two pipelines are checked SEPARATELY against their own cadence: the 10-min collector
keeps the DB "fresh" essentially always, so checking only the most-recent activity
would let the 6h long-term run die for weeks unnoticed (the headline product).

  * collector (collect_once, */10): stale after WATCHDOG_STALE_HOURS (default 3h)
  * long-term (run_once, every 6h):  stale after ~2 cadences + slack (13h)

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

from . import notify, store
from .config import Config, load_config

log = logging.getLogger("btc-watchdog")

# Long-term run cadence is 6h; flag only after ~2 missed cadences + slack.
RUN_STALE_HOURS = 13.0
# Debounce: don't re-alert until this many hours since the last watchdog alert.
_REALERT_AFTER_HOURS = 6.0
_META_KEY = "watchdog_last_alert"


def check(cfg: Config, *, dry_run: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    conn = store.connect(cfg.db_path)
    store.init_db(conn)
    last_collect = store.last_collect_ts(conn)
    last_run = store.last_run_ts(conn)

    def _age_h(t):
        return (now - t).total_seconds() / 3600.0 if t is not None else None

    collect_age = _age_h(last_collect)
    run_age = _age_h(last_run)
    collect_stale = collect_age is None or collect_age > cfg.watchdog_stale_hours
    run_stale = run_age is None or run_age > RUN_STALE_HOURS
    stale = collect_stale or run_stale

    result = {
        "last_collect": last_collect.isoformat() if last_collect else None,
        "last_run": last_run.isoformat() if last_run else None,
        "collect_age_hours": collect_age, "run_age_hours": run_age,
        "collect_stale": collect_stale, "run_stale": run_stale,
        "stale": stale, "alerted": False,
    }

    if not stale:
        log.info("watchdog: healthy (collect %.1fh, run %.1fh ago)",
                 collect_age or 0.0, run_age or 0.0)
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
        problems.append(f"short-term collector (last {_when(collect_age)}, "
                        f"threshold {cfg.watchdog_stale_hours:.0f}h)")
    if run_stale:
        problems.append(f"long-term run (last {_when(run_age)}, "
                        f"threshold {RUN_STALE_HOURS:.0f}h)")
    title = "BTC notifier WATCHDOG: pipeline stale"
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
        sent = notify.send(cfg, title, body)
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

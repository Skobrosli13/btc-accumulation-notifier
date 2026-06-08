"""Dead-man's-switch (cron 0 */8).

Emails once if no successful short-term collect (or long-term run) has landed
within WATCHDOG_STALE_HOURS, so a broken pipeline (e.g. an exchange API change)
never silently masquerades as "quiet market, no signal".

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


def check(cfg: Config, *, dry_run: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    conn = store.connect(cfg.db_path)
    store.init_db(conn)
    last_collect = store.last_collect_ts(conn)
    last_run = store.last_run_ts(conn)
    conn.close()

    # The most recent of either pipeline.
    candidates = [t for t in (last_collect, last_run) if t is not None]
    latest = max(candidates) if candidates else None

    stale = True
    age_h = None
    if latest is not None:
        age_h = (now - latest).total_seconds() / 3600.0
        stale = age_h > cfg.watchdog_stale_hours

    result = {"latest": latest.isoformat() if latest else None,
              "age_hours": age_h, "stale": stale, "alerted": False}

    if stale:
        when = f"{age_h:.1f}h ago" if age_h is not None else "never"
        title = "BTC notifier WATCHDOG: pipeline stale"
        body = (
            f"No successful data collection in the last {cfg.watchdog_stale_hours:.0f}h "
            f"(last activity: {when}).\n\n"
            "The collector or long-term run may be broken (e.g. an exchange API change, "
            "a crashed service, or a host issue). 'No alerts' right now does NOT mean "
            "'quiet market' — it likely means the pipeline stopped.\n\n"
            "Check: logs, `systemctl status btc-api`, cron, and the exchange endpoints."
        )
        if dry_run:
            log.info("[dry-run] WATCHDOG would alert:\n%s\n%s", title, body)
        else:
            notify.send(cfg, title, body)
        result["alerted"] = True
        log.warning("watchdog: STALE (%s)", when)
    else:
        log.info("watchdog: healthy (last activity %.1fh ago)", age_h or 0.0)
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

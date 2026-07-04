"""Daily digest (owner-only) — §8's ONE scheduled email.

    python -m scripts.send_digest [--dry-run]

Renders the SAME aggregation as the Today page (/api/today's aggregate_today),
so the email and the screen can never disagree. Sent only when there is
something to say (act rows, or a study status changed since the last digest);
instant pushes remain reserved for ACT/RISK/FAIL. Box cron: 12:45 UTC Mon–Fri
(pre-open ET).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import notify_email, store              # noqa: E402
from app.api.today import aggregate_today        # noqa: E402
from app.config import load_config               # noqa: E402
from app.harness import schema                   # noqa: E402

log = logging.getLogger("digest")
_META_KEY = "digest_last_testing"


def render(payload: dict) -> tuple[str, str]:
    lines: list[str] = []
    if payload["act"]:
        lines.append("ACT — since the previous business day:")
        for a in payload["act"]:
            when = (datetime.fromtimestamp(a["ts"] / 1000, tz=timezone.utc)
                    .date().isoformat() if a.get("ts") else "")
            # Gap C: a stale-synced event is recording, not a fresh pick.
            label = a["label"] + (" — STALE FEED" if a.get("stale") else "")
            lines.append(f"  • [{label}] {a['ticker']} "
                         f"{a.get('direction') or ''} {a.get('detail') or ''} {when}".rstrip())
    else:
        lines.append("Nothing needs you today.")
    p = payload["paper"]
    if p["nav"] is not None:
        lines.append("")
        at = (f", after-tax {p['nav_after_tax']:.4f}"
              if p.get("nav_after_tax") is not None else "")
        lines.append(f"Paper book: NAV {p['nav']:.4f}{at} vs SPY {p['bench']:.4f} "
                     f"({p['open']} open, {p['pending']} pending, {p['closed']} closed)")
    lines.append("")
    lines.append("Testing:")
    for t in payload["testing"]:
        lines.append(f"  {t['name']} = {t['status']} — {t.get('next_decision', '')}".rstrip(" —"))
    # §4: the digest carries the health summary — quiet ≠ healthy.
    h = payload.get("health") or {}
    lab = payload["lab_sync"]
    def _age(v):  # noqa: E306
        return f"{v:.1f}h" if v is not None else "never"
    lines.append("")
    lines.append(f"Health: collector {_age(h.get('collect_age_hours'))}"
                 f"{' STALE' if h.get('collect_stale') else ''} · "
                 f"LT run {_age(h.get('run_age_hours'))}"
                 f"{' STALE' if h.get('run_stale') else ''} · "
                 f"lab sync {_age(lab.get('age_hours'))}"
                 f"{' OVERDUE' if lab.get('overdue') else ''}")
    if lab.get("overdue"):
        lines.append("⚠ Lab sync OVERDUE — the laptop nightly missed; stock event "
                     "rows may be missing or stale.")
    lines.append("")
    lines.append(payload["note"])
    n_act = len(payload["act"])
    title = (f"Daily digest — {n_act} item{'s' if n_act != 1 else ''} to review"
             if n_act else "Daily digest — nothing to do")
    return title, "\n".join(lines)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    cfg = load_config()
    conn = store.connect(cfg.db_path)
    store.init_db(conn)
    schema.init_harness_db(conn)
    try:
        payload = aggregate_today(conn)
        testing_now = ";".join(f"{t['name']}={t['status']}" for t in payload["testing"])
        testing_prev = store.get_meta(conn, _META_KEY)
        has_news = bool(payload["act"]) or testing_now != (testing_prev or "")
        title, body = render(payload)
        if args.dry_run:
            log.info("[dry-run] has_news=%s\n%s\n\n%s", has_news, title, body)
            return 0
        if not has_news:
            log.info("digest: nothing new — skipped (fatigue budget)")
            return 0
        # Owner-only: the digest carries PROMOTED output (directive 6) — it
        # must never broadcast to subscribers, hence notify_email directly.
        if cfg.resend_api_key and cfg.email_to:
            notify_email.send_email(cfg, title, body, to=cfg.email_to)
            store.set_meta(conn, _META_KEY, testing_now)
            log.info("digest sent to owner")
        else:
            log.info("digest: email not configured; skipped")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

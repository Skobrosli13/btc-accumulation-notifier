"""Notification transports: ntfy.sh (default) and Telegram (alternative).

Both are no-ops when unconfigured, so the app runs end-to-end with no
notification setup (the run still logs and persists to the ledger).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

import requests

from .config import Config

log = logging.getLogger(__name__)


# §8 fatigue budget: instant push carries exactly three severities. Everything
# else belongs in the daily digest (severity=None -> default priority).
#   ACT  — execution-required (a proposed order)        -> high
#   RISK — drawdown threshold / forced de-risk           -> high
#   FAIL — QA red / job dead / dead-man missed           -> urgent
_SEVERITY_PRIORITY = {"ACT": "high", "RISK": "high", "FAIL": "urgent"}


def _in_quiet_hours(window: str, now: datetime | None = None) -> bool:
    """True when ``now`` (UTC) falls in an "HH-HH" quiet window (may wrap
    midnight, e.g. "22-06"). Malformed/empty windows disable quiet hours."""
    try:
        lo_s, hi_s = window.split("-")
        lo, hi = int(lo_s), int(hi_s)
    except (ValueError, AttributeError):
        return False
    h = (now or datetime.now(timezone.utc)).hour
    return lo <= h < hi if lo <= hi else (h >= lo or h < hi)


def _push_priority(cfg: Config, severity: str | None) -> str:
    """§4: severity tier, muted by quiet hours for everything except FAIL —
    the dead-man's-switch must always ring; nothing else gets to at 3am."""
    pri = _SEVERITY_PRIORITY.get(severity or "", "default")
    if severity != "FAIL" and _in_quiet_hours(cfg.quiet_hours_utc):
        return "min"
    return pri


def _send_ntfy(cfg: Config, title: str, body: str,
               severity: str | None = None) -> bool:
    url = f"{cfg.ntfy_server}/{cfg.ntfy_topic}"
    try:
        r = requests.post(
            url,
            data=body.encode("utf-8"),
            headers={"Title": title,
                     "Priority": _push_priority(cfg, severity),
                     "Tags": "chart_with_downwards_trend"},
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("ntfy send failed: %s", exc)
        return False


def _send_telegram(cfg: Config, title: str, body: str) -> bool:
    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
    # Send as PLAIN TEXT (no parse_mode): our bodies contain underscores and arrows
    # (e.g. "tier ACCUMULATE→DEEP_VALUE") that legacy-Markdown parses as unmatched
    # entities -> Telegram 400 -> a silently-failed send on exactly the important
    # alerts. Plain text can't be malformed.
    text = f"{title}\n\n{body}"
    try:
        r = requests.post(
            url,
            json={"chat_id": cfg.telegram_chat_id, "text": text},
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Telegram send failed: %s", exc)
        return False


def has_transport(cfg: Config) -> bool:
    """True when at least one notification transport is configured.

    Callers use this to tell a RETRYABLE send failure (configured transport,
    transient error) apart from the documented empty-.env no-op, where ``send``
    always returns False — e.g. the stock cooldown arms off alert creation when
    there is nothing to send through, instead of waiting for a send that can
    never succeed."""
    return cfg.notifications_configured()


def _email_recipients(cfg: Config, conn: sqlite3.Connection | None) -> dict[str, str | None]:
    """Build the email broadcast list as {email: unsubscribe_token | None}.

    The owner (EMAIL_TO) is always included (config-controlled, no unsubscribe
    link). Active dashboard subscribers are layered on top, each carrying their
    own token; deduped by lowercased address (a subscriber entry wins, so the
    owner gets the unsubscribe footer too if they also subscribed via the form).
    """
    from . import store

    recipients: dict[str, str | None] = {}
    if cfg.email_to:
        recipients[cfg.email_to.strip().lower()] = None
    if conn is not None:
        for email, token in store.list_active_subscribers(conn):
            recipients[email.strip().lower()] = token
    return recipients


def send(cfg: Config, title: str, body: str,
         conn: sqlite3.Connection | None = None,
         severity: str | None = None) -> bool:
    """Send via whatever transports are configured. Returns True if any succeeded.

    Email (Resend) is the primary channel after the pivot; ntfy/Telegram are
    optional secondaries. All are no-ops when unconfigured. When ``conn`` is
    given, the email goes to the owner AND every active dashboard subscriber,
    each with a personal unsubscribe link. ``severity`` ('ACT'|'RISK'|'FAIL')
    raises the push priority — reserved for the §8 instant tier; everything
    else rides the daily digest.
    """
    from . import notify_email

    sent = False
    if cfg.resend_api_key:
        for email, token in _email_recipients(cfg, conn).items():
            unsub = (f"{cfg.public_base_url}/api/unsubscribe?token={token}"
                     if token else None)
            sent = notify_email.send_email(cfg, title, body, to=email,
                                           unsubscribe_url=unsub) or sent
    if cfg.ntfy_topic:
        sent = _send_ntfy(cfg, title, body, severity=severity) or sent
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        sent = _send_telegram(cfg, title, body) or sent
    if not cfg.notifications_configured():
        log.info("No notification transport configured; alert not sent:\n%s\n%s", title, body)
    return sent

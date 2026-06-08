"""Notification transports: ntfy.sh (default) and Telegram (alternative).

Both are no-ops when unconfigured, so the app runs end-to-end with no
notification setup (the run still logs and persists to the ledger).
"""
from __future__ import annotations

import logging
import sqlite3

import requests

from .config import Config

log = logging.getLogger(__name__)


def _send_ntfy(cfg: Config, title: str, body: str) -> bool:
    url = f"{cfg.ntfy_server}/{cfg.ntfy_topic}"
    try:
        r = requests.post(
            url,
            data=body.encode("utf-8"),
            headers={"Title": title, "Priority": "default", "Tags": "chart_with_downwards_trend"},
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("ntfy send failed: %s", exc)
        return False


def _send_telegram(cfg: Config, title: str, body: str) -> bool:
    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
    text = f"*{title}*\n\n{body}"
    try:
        r = requests.post(
            url,
            json={"chat_id": cfg.telegram_chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Telegram send failed: %s", exc)
        return False


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
         conn: sqlite3.Connection | None = None) -> bool:
    """Send via whatever transports are configured. Returns True if any succeeded.

    Email (Resend) is the primary channel after the pivot; ntfy/Telegram are
    optional secondaries. All are no-ops when unconfigured. When ``conn`` is
    given, the email goes to the owner AND every active dashboard subscriber,
    each with a personal unsubscribe link.
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
        sent = _send_ntfy(cfg, title, body) or sent
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        sent = _send_telegram(cfg, title, body) or sent
    if not cfg.notifications_configured():
        log.info("No notification transport configured; alert not sent:\n%s\n%s", title, body)
    return sent

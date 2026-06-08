"""Notification transports: ntfy.sh (default) and Telegram (alternative).

Both are no-ops when unconfigured, so the app runs end-to-end with no
notification setup (the run still logs and persists to the ledger).
"""
from __future__ import annotations

import logging

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


def send(cfg: Config, title: str, body: str) -> bool:
    """Send via whatever transports are configured. Returns True if any succeeded."""
    sent = False
    if cfg.ntfy_topic:
        sent = _send_ntfy(cfg, title, body) or sent
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        sent = _send_telegram(cfg, title, body) or sent
    if not cfg.notifications_configured():
        log.info("No notification transport configured; alert not sent:\n%s\n%s", title, body)
    return sent

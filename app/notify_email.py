"""Email transport via Resend.

Primary notification channel after the pivot. Sandbox mode sends from
``onboarding@resend.dev`` and can only reach the account's own verified address
(set EMAIL_TO to your verified Gmail); after verifying a domain, set EMAIL_FROM to
``you@yourdomain`` to send anywhere. No-op (returns False) when unconfigured, and
swallows exceptions like the other transports.
"""
from __future__ import annotations

import logging

from .config import Config

log = logging.getLogger(__name__)


def _html(body: str) -> str:
    """Wrap a plain-text alert body in minimal HTML (preserves line breaks)."""
    safe = (body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;'
        'font-size:14px;line-height:1.5;white-space:pre-wrap">'
        f"{safe}</div>"
    )


def send_email(cfg: Config, subject: str, body: str) -> bool:
    """Send one alert email. Returns True on success, False otherwise."""
    if not cfg.email_active:
        log.info("Email not configured (need RESEND_API_KEY + EMAIL_TO); skipping")
        return False
    try:
        import resend

        resend.api_key = cfg.resend_api_key
        resend.Emails.send({
            "from": cfg.email_from,
            "to": [cfg.email_to],
            "subject": subject,
            "html": _html(body),
            "text": body,
        })
        return True
    except Exception as exc:  # noqa: BLE001 - graceful like ntfy/telegram
        log.warning("Resend email failed: %s", exc)
        return False

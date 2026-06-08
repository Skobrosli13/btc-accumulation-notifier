"""Email transport via Resend.

Primary notification channel after the pivot. Sandbox mode sends from
``onboarding@resend.dev`` and can only reach the account's own verified address
(set EMAIL_TO to your verified Gmail); after verifying a domain, set EMAIL_FROM to
``you@yourdomain`` to send anywhere. No-op (returns False) when unconfigured, and
swallows exceptions like the other transports.
"""
from __future__ import annotations

import html
import logging

from .config import Config

log = logging.getLogger(__name__)


def _html(body: str, unsubscribe_url: str | None = None) -> str:
    """Wrap a plain-text alert body in minimal HTML (preserves line breaks),
    optionally appending a clickable unsubscribe footer."""
    safe = html.escape(body)
    footer = ""
    if unsubscribe_url:
        safe_url = html.escape(unsubscribe_url, quote=True)
        footer = (
            '<div style="margin-top:18px;padding-top:12px;'
            'border-top:1px solid rgba(0,0,0,.1);font-size:12px;color:#6b7280">'
            "You're receiving this because you subscribed to BTC signal alerts.<br>"
            f'<a href="{safe_url}" style="color:#6b7280">Unsubscribe</a>'
            "</div>"
        )
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;'
        'font-size:14px;line-height:1.5;white-space:pre-wrap">'
        f"{safe}</div>{footer}"
    )


def send_email(cfg: Config, subject: str, body: str, *,
               to: str | None = None, unsubscribe_url: str | None = None) -> bool:
    """Send one alert email to ``to`` (defaults to cfg.email_to).

    When ``unsubscribe_url`` is given, a footer link and a List-Unsubscribe header
    are added. Returns True on success, False otherwise.
    """
    recipient = to or cfg.email_to
    if not cfg.resend_api_key or not recipient:
        log.info("Email not configured (need RESEND_API_KEY + a recipient); skipping")
        return False

    text = body
    if unsubscribe_url:
        text = (f"{body}\n\n—\n"
                "You're receiving this because you subscribed to BTC signal alerts.\n"
                f"Unsubscribe: {unsubscribe_url}")
    try:
        import resend

        resend.api_key = cfg.resend_api_key
        payload = {
            "from": cfg.email_from,
            "to": [recipient],
            "subject": subject,
            "html": _html(body, unsubscribe_url),
            "text": text,
        }
        if unsubscribe_url:
            payload["headers"] = {"List-Unsubscribe": f"<{unsubscribe_url}>"}
        resend.Emails.send(payload)
        return True
    except Exception as exc:  # noqa: BLE001 - graceful like ntfy/telegram
        log.warning("Resend email failed: %s", exc)
        return False

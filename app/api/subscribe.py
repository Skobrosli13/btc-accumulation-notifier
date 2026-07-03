"""Email subscription endpoints (double opt-in) + the unsubscribe pages.

``/api/subscribe`` is token-gated (called by the dashboard's server-side proxy).
The unsubscribe GET/POST are public — the unguessable token is the capability —
and are hardened against reflected XSS (token gating + html.escape + a
locked-down CSP); see the regression tests in tests/test_api.py.
"""
from __future__ import annotations

import html
import re
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .. import notify_email, store
from ..api_deps import conn_rw as _conn_rw
from ..api_deps import get_config, require_token
from ..config import Config

router = APIRouter()

# Reflected into the unsubscribe HTML — reject any address carrying characters
# that could break out of an HTML attribute/text node (belt-and-suspenders with
# html.escape at render time; also stops such rows entering the DB via subscribe).
_EMAIL_RE = re.compile(r"^[^@\s<>\"'&]+@[^@\s<>\"'&]+\.[^@\s<>\"'&]+$")
# Subscriber tokens are secrets.token_urlsafe(32) -> URL-safe base64 [A-Za-z0-9_-].
# Only render the unsubscribe form for a well-formed token; anything else is a
# malformed/hostile link and gets the "invalid" page (no form, no reflection).
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,}$")


class SubscribeIn(BaseModel):
    email: str


def _send_welcome(cfg: Config, email: str, unsubscribe_url: str) -> None:
    """Confirmation email (also carries the unsubscribe link). Best-effort."""
    subject = "You're subscribed to BTC signal alerts"
    body = (
        "You'll now receive Bitcoin long-term accumulation alerts at this address:\n\n"
        "  • Tier changes — WATCH → ACCUMULATE → DEEP_VALUE\n"
        "  • Capitulation flash — an acute, oversold-fear washout\n\n"
        "These are infrequent, high-confluence signals (not the noisier short-term "
        "swing triggers, which stay on the dashboard). Not financial advice — "
        "long-term is buy-only accumulation; you decide whether, how much, and where."
    )
    notify_email.send_email(cfg, subject, body, to=email, unsubscribe_url=unsubscribe_url)


@router.post("/api/subscribe")
def subscribe(body: SubscribeIn, background: BackgroundTasks,
              cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    """Add an email to the alert broadcast list (token-gated; called by the
    dashboard's server-side proxy). Sends a confirmation/welcome email."""
    email = (body.email or "").strip().lower()
    if len(email) > 254 or not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="invalid email")
    token = secrets.token_urlsafe(32)
    conn = _conn_rw(cfg)
    try:
        token, is_new = store.upsert_subscriber(
            conn, email=email, token=token,
            created_at=datetime.now(timezone.utc).isoformat())
    finally:
        conn.close()
    # Only send the welcome on a genuinely NEW subscription — re-POSTing an existing
    # address (a refresh, or an abuse loop) must not re-send mail (Resend quota /
    # reputation / unsolicited-mail vector).
    if cfg.resend_api_key and is_new:
        unsub = f"{cfg.public_base_url}/api/unsubscribe?token={token}"
        background.add_task(_send_welcome, cfg, email, unsub)
    return {"ok": True, "email": email, "new": is_new}


def _unsub_page(message: str, *, body_html: str = "") -> HTMLResponse:
    safe = message  # message is one of our own fixed strings (no user input)
    html_doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>BTC alerts</title>
<style>
  html,body{{margin:0;height:100%;background:#0b0d12;color:#e8eaf0;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
  .box{{max-width:440px;margin:14vh auto 0;padding:32px;background:#14181f;
    border:1px solid rgba(255,255,255,.06);border-radius:16px;text-align:center;
    box-shadow:0 8px 24px rgba(0,0,0,.22)}}
  h1{{font-size:18px;margin:0 0 10px}}
  p{{color:#98a1b2;font-size:14px;line-height:1.5;margin:0 0 16px}}
  button{{background:#e8eaf0;color:#0b0d12;border:0;border-radius:10px;
    padding:11px 22px;font-size:14px;font-weight:600;cursor:pointer}}
</style></head>
<body><div class="box"><h1>BTC signal alerts</h1><p>{safe}</p>{body_html}</div></body></html>"""
    # default-src 'none' neutralises any injected <script>/<img>/etc. even if a
    # reflection slipped past escaping — this is a static, no-asset page.
    return HTMLResponse(content=html_doc,
                        headers={"Content-Security-Policy": "default-src 'none'"})


def _do_unsubscribe(token: str, cfg: Config) -> str | None:
    if not token:
        return None
    conn = _conn_rw(cfg)
    try:
        return store.deactivate_subscriber(conn, token)
    finally:
        conn.close()


@router.get("/api/unsubscribe", response_class=HTMLResponse)
def unsubscribe_confirm(token: str = Query(""),
                        cfg: Config = Depends(get_config)) -> HTMLResponse:
    """Public confirmation page — does NOT mutate.

    A GET must be side-effect-free: corporate/AV mail link scanners (Outlook
    SafeLinks, Gmail/Yahoo prefetch, Mimecast) issue GETs on every link in an
    email, which previously unsubscribed subscribers the moment a message was
    merely scanned. The actual deactivation happens on the POST below — triggered
    either by the button here or by an RFC 8058 one-click request from the mail
    client. (The token in the URL remains the capability.)
    """
    valid = bool(_TOKEN_RE.match(token))
    safe_token = html.escape(token, quote=True)
    form = (f'<form method="post" action="/api/unsubscribe?token={safe_token}">'
            f'<button type="submit">Unsubscribe</button></form>') if valid else ""
    return _unsub_page(
        "Click below to stop receiving BTC signal alerts at your address."
        if valid else "This unsubscribe link is invalid.",
        body_html=form)


@router.post("/api/unsubscribe", response_class=HTMLResponse)
def unsubscribe_do(token: str = Query(""),
                   cfg: Config = Depends(get_config)) -> HTMLResponse:
    """Public (no bearer token) — the unguessable ``token`` is the capability.

    Handles both the confirmation-page button and RFC 8058 one-click POSTs
    (``List-Unsubscribe-Post: List-Unsubscribe=One-Click``) from Gmail/Yahoo.
    Idempotent.
    """
    email = _do_unsubscribe(token, cfg)
    if email:
        # email comes from our DB, but escape defensively — historical rows may
        # predate the tightened _EMAIL_RE above.
        return _unsub_page(
            f"You’ve been unsubscribed. {html.escape(email)} will no longer "
            "receive alerts.")
    return _unsub_page(
        "This unsubscribe link is invalid or has already been used. "
        "If you keep receiving alerts, reply to one of them.")

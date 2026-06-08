"""Small HTTP helpers shared by the source adapters.

``get_json`` / ``get_text`` swallow network and decode errors and return None so
that an optional source can degrade gracefully. The mandatory price source does
NOT use the swallowing variants for its primary fetch — it lets failures surface.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20


def get_json(url: str, params: dict | None = None, headers: dict | None = None,
             timeout: int = DEFAULT_TIMEOUT):
    """GET and parse JSON; return None on any network/HTTP/decode error."""
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001 - graceful degradation is the contract
        log.warning("GET %s failed: %s", url, exc)
        return None


def get_text(url: str, params: dict | None = None, headers: dict | None = None,
             timeout: int = DEFAULT_TIMEOUT) -> str | None:
    """GET and return body text; return None on any network/HTTP error."""
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as exc:  # noqa: BLE001
        log.warning("GET %s failed: %s", url, exc)
        return None

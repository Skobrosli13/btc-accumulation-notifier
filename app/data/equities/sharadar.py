"""Sharadar Core US Equities Bundle adapter (Nasdaq Data Link datatables API).

Fetches a ``SHARADAR/{table}`` as row-dicts, paging through the API cursor.
Fail-soft (returns [] on any error) like every adapter in this codebase; the
ingest job writes the results into the Parquet lake and point-in-time discipline
lives downstream (``datekey`` for fundamentals, ``lastupdated`` for incremental
refresh).

Bundle tables: SEP (EOD prices, incl. delisted), SF1 (PIT fundamentals via
``datekey``), SF2 (insider Form 3/4/5), SF3/SF3A/SF3B (13F), DAILY (mcap/EV),
TICKERS (security master, ``permaticker``), ACTIONS (corporate actions incl.
delisting), plus EVENTS/METRICS/SP500/SFP/INDICATORS.

Secret hygiene: the api_key travels as a query param (the documented method) but
is SCRUBBED from every log line — a 4xx error string from ``requests`` would
otherwise echo the full URL (key included).
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

_BASE = "https://data.nasdaq.com/api/v3/datatables/SHARADAR/{table}.json"
_TIMEOUT = 60
_DEFAULT_MAX_PAGES = 1000     # runaway guard; a full table pages in well under this

# Tables in the purchased bundle — a validation guard so a typo fails fast rather
# than 404-ing live.
TABLES = frozenset({
    "SEP", "SF1", "SF2", "SF3", "SF3A", "SF3B", "DAILY", "TICKERS", "ACTIONS",
    "EVENTS", "METRICS", "SP500", "SFP", "INDICATORS",
})


def datatable_rows(payload: dict) -> list[dict]:
    """Map a datatables payload's parallel columns+data arrays to row dicts (pure)."""
    dt = (payload or {}).get("datatable") or {}
    cols = [c.get("name") for c in (dt.get("columns") or [])]
    return [dict(zip(cols, row)) for row in (dt.get("data") or [])]


def next_cursor(payload: dict) -> str | None:
    """The pagination cursor for the next page, or None when the table is exhausted."""
    meta = (payload or {}).get("meta") or {}
    return meta.get("next_cursor_id")


def _scrub(text: str, secret: str | None) -> str:
    return text.replace(secret, "***") if secret else text


def _get(url: str, params: dict, secret: str | None) -> dict | None:
    """Single GET -> parsed JSON, or None; never raises, never logs the key."""
    try:
        r = requests.get(url, params=params, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001 - fail-soft; scrub the key from the message
        log.warning("sharadar GET %s failed: %s", url, _scrub(str(exc), secret))
        return None


def request_bulk(table: str, api_key: str) -> dict | None:
    """One GET of the bulk-export endpoint -> the ``file`` block
    ``{link, status, data_snapshot_time}`` (or None). ``status`` is ``fresh`` when
    the zipped-CSV snapshot is ready to download, else it is being regenerated."""
    if table not in TABLES or not api_key:
        return None
    payload = _get(_BASE.format(table=table),
                   {"qopts.export": "true", "api_key": api_key}, api_key)
    if not isinstance(payload, dict):
        return None
    return ((payload.get("datatable_bulk_download") or {}).get("file")) or None


def bulk_link(table: str, api_key: str, *, poll_interval: float = 10.0,
              max_wait: float = 1800.0) -> str | None:
    """Poll the bulk-export endpoint until the snapshot is ``fresh``; return the
    download link (a single zipped CSV of the whole table), or None on timeout."""
    import time
    waited = 0.0
    while True:
        f = request_bulk(table, api_key)
        if not f:
            return None
        if f.get("status") == "fresh" and f.get("link"):
            return f["link"]
        if waited >= max_wait:
            log.warning("sharadar bulk %s not fresh after %.0fs (status=%s)",
                        table, waited, f.get("status"))
            return None
        time.sleep(poll_interval)
        waited += poll_interval


def fetch_table(table: str, api_key: str, *, params: dict | None = None,
                max_pages: int = _DEFAULT_MAX_PAGES) -> list[dict]:
    """All rows of ``SHARADAR/{table}`` matching ``params``, following the cursor.

    ``params`` carries datatable filters (e.g. ``{"ticker": "AAPL",
    "dimension": "ARQ"}``) and qopts (e.g. ``{"qopts.per_page": 10000}``).
    Returns [] on any failure or an unknown table; caps at ``max_pages`` (logs if
    the cap trips, so a silently-truncated ingest is visible)."""
    if table not in TABLES:
        log.warning("sharadar: unknown table %r (not in the bundle)", table)
        return []
    if not api_key:
        return []
    base = dict(params or {})
    base["api_key"] = api_key
    url = _BASE.format(table=table)
    rows: list[dict] = []
    cursor: str | None = None
    for _page in range(max_pages):
        p = dict(base)
        if cursor:
            p["qopts.cursor_id"] = cursor
        payload = _get(url, p, api_key)
        if not isinstance(payload, dict):
            break
        rows.extend(datatable_rows(payload))
        cursor = next_cursor(payload)
        if not cursor:
            break
    else:
        log.warning("sharadar: %s hit max_pages=%d cap — data may be truncated", table, max_pages)
    return rows

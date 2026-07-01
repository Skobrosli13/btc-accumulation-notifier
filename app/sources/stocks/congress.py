"""Congressional-trade adapter (free community PTR dataset) — Phase 3 forward-test.

The STOCK Act disclosures are public but the timely, structured feeds are paid
(Quiver ~$25-30/mo). The free path is the community house/senate-stock-watcher JSON
mirrors. OFF by default (``STOCK_CONGRESS=false``); when on it is a **forward-test /
context** read only — disclosures lag up to 45 days, so this is never a timely swing
trigger, just a slow confirmation overlay. Fail-soft: ``[]`` on any failure.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .._http import get_json

log = logging.getLogger(__name__)

HOUSE_URL = "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json"


def _date_ms(s: str) -> int | None:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            d = datetime.strptime(s[:10], fmt)
            return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
        except (ValueError, TypeError):
            continue
    return None


def recent_house_trades(since_ts: int, tickers: set[str] | None = None,
                        user_agent: str = "riverviewweb-signal admin@riverviewweb.com"
                        ) -> list[dict]:
    """Recent US House trades disclosed since ``since_ts``, optionally filtered to a
    ticker set. [] on failure. Normalized -> {ticker, member, type, amount, txn_ts}."""
    data = get_json(HOUSE_URL, headers={"User-Agent": user_agent})
    if not isinstance(data, list):
        return []
    out = []
    for r in data:
        sym = (r.get("ticker") or "").upper()
        if not sym or sym in ("--", "N/A"):
            continue
        if tickers is not None and sym not in tickers:
            continue
        tts = _date_ms(r.get("transaction_date", ""))
        if tts is None or tts < since_ts:
            continue
        out.append({"ticker": sym, "member": r.get("representative"),
                    "type": r.get("type"), "amount": r.get("amount"), "txn_ts": tts})
    return out

"""Sentiment: the Crypto Fear & Greed Index (alternative.me, free, no key).

Extreme Fear (<=10) historically clusters near cycle bottoms; 40 is treated as
neutral. The payload's own timestamp is checked against the freshness budget so
a frozen feed reads as missing rather than scoring yesterday's fear forever.
"""
from __future__ import annotations

import logging

from ._http import get_json, is_stale

log = logging.getLogger(__name__)

FNG_URL = "https://api.alternative.me/fng/"


def fear_greed() -> dict:
    """Return {'fng': <0-100 index>} or {'fng': None}."""
    data = get_json(FNG_URL, params={"limit": 1})
    if not data:
        return {"fng": None}
    try:
        row = data["data"][0]
        value = float(row["value"])
    except (KeyError, IndexError, TypeError, ValueError):
        return {"fng": None}
    ts = row.get("timestamp") if isinstance(row, dict) else None
    if ts is not None:
        try:
            ts_s = float(ts)
            if ts_s > 1e12:  # defensively accept epoch milliseconds
                ts_s /= 1000.0
            if is_stale(ts_s):
                log.warning("Fear & Greed latest point is stale; treated as missing")
                return {"fng": None}
        except (TypeError, ValueError):
            pass  # undated payload: no freshness check possible
    return {"fng": value}

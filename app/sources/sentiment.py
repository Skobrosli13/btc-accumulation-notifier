"""Sentiment: the Crypto Fear & Greed Index (alternative.me, free, no key).

Extreme Fear (<=10) historically clusters near cycle bottoms; 40 is treated as
neutral.
"""
from __future__ import annotations

from ._http import get_json

FNG_URL = "https://api.alternative.me/fng/"


def fear_greed() -> dict:
    """Return {'fng': <0-100 index>} or {'fng': None}."""
    data = get_json(FNG_URL, params={"limit": 1})
    if not data:
        return {"fng": None}
    try:
        return {"fng": float(data["data"][0]["value"])}
    except (KeyError, IndexError, TypeError, ValueError):
        return {"fng": None}

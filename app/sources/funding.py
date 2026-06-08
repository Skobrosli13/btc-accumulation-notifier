"""Funding rate.

Free proxy: Binance USD-M futures funding history (no auth). The 7-day average
funding rate is the reading; persistently negative funding -> longs paying to be
short -> capitulation/bullish for accumulation.

If a Coinglass key is present, ``derivatives.py`` supplies a richer OI-weighted
funding figure; this module is the always-available free baseline.
"""
from __future__ import annotations

from ._http import get_json

BINANCE_FUNDING = "https://fapi.binance.com/fapi/v1/fundingRate"

# Binance pays funding every 8h -> 3/day -> ~21 settlements over 7 days.
_SETTLEMENTS_7D = 21


def funding_7d_avg(symbol: str = "BTCUSDT") -> dict:
    """Return {'funding': <7d avg 8h funding fraction>} or {'funding': None}."""
    data = get_json(BINANCE_FUNDING, params={"symbol": symbol, "limit": 1000})
    if not data:
        return {"funding": None}
    try:
        recent = data[-_SETTLEMENTS_7D:]
        rates = [float(d["fundingRate"]) for d in recent if d.get("fundingRate") is not None]
        if not rates:
            return {"funding": None}
        return {"funding": sum(rates) / len(rates)}
    except (KeyError, TypeError, ValueError):
        return {"funding": None}

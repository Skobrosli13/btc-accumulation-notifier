"""Funding rate (free) via the exchange adapter (OKX perpetual).

The 7-day average funding rate is the long-term reading; persistently negative
funding -> longs paying shorts -> capitulation/bullish for accumulation.
Degrades to None on any failure. (Binance's funding endpoint is geo-blocked from
US/AWS, so this now reads OKX through ``exchange.py``.)
"""
from __future__ import annotations

from . import exchange

# OKX funds every 8h -> 3/day -> ~21 settlements over 7 days.
_SETTLEMENTS_7D = 21


def funding_7d_avg(symbol: str = "BTC-USDT") -> dict:
    """Return {'funding': <7d avg 8h funding fraction>} or {'funding': None}."""
    hist = exchange.funding_history(limit=_SETTLEMENTS_7D, symbol=symbol)
    if not hist:
        return {"funding": None}
    rates = [r for _, r in hist]
    return {"funding": sum(rates) / len(rates)}

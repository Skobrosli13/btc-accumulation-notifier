"""Funding rate (free) via the exchange adapter (OKX perpetual).

The 7-day average funding rate is the long-term reading; persistently negative
funding -> longs paying shorts -> capitulation/bullish for accumulation.
Degrades to None on any failure. (Binance's funding endpoint is geo-blocked from
US/AWS, so this now reads OKX through ``exchange.py``.)

The 7-day window is selected by TIMESTAMP, not by a fixed settlement count: OKX
switches some instruments to 4h funding in volatile regimes, so a hardcoded "21
settlements" would silently cover ~3.5 days (halving the effective averaging
window and roughly doubling the spike sensitivity) exactly when it matters most.
The mean is normalized to a per-8h rate from the observed settlement spacing so
the scored value stays on the same scale regardless of the venue's interval.
"""
from __future__ import annotations

import time

from . import exchange

_SEVEN_DAYS_MS = 7 * 86_400_000
_EIGHT_HOURS_MS = 8 * 3600_000


def funding_7d_avg(symbol: str = "BTC-USDT") -> dict:
    """Return {'funding': <7d avg, normalized to a per-8h fraction>} or {'funding': None}."""
    hist = exchange.funding_history(limit=100, symbol=symbol)
    if not hist:
        return {"funding": None}
    now_ms = int(time.time() * 1000)
    window = [(ts, r) for ts, r in hist if ts >= now_ms - _SEVEN_DAYS_MS]
    if not window:
        window = hist  # all older than 7d (sparse history) -> use what we have
    rates = [r for _, r in window]
    avg = sum(rates) / len(rates)
    # Normalize to per-8h using the median spacing between settlements (if the venue
    # is on 4h funding, each print is ~half an 8h rate, so scale up by 2).
    if len(window) >= 2:
        ts_sorted = sorted(ts for ts, _ in window)
        gaps = [b - a for a, b in zip(ts_sorted, ts_sorted[1:]) if b > a]
        if gaps:
            gaps.sort()
            median_gap = gaps[len(gaps) // 2]
            if median_gap > 0:
                avg *= _EIGHT_HOURS_MS / median_gap
    return {"funding": avg}

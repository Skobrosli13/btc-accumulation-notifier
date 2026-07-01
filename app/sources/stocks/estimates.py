"""Analyst recommendation-trend snapshot (Finnhub free) — revision-momentum proxy.

Reliable HISTORICAL estimate revisions are the biggest paid-only gap (Zacks ZEEH),
so we ACCRUE our own: snapshot the free recommendation trend each run and store it.
The delta between two snapshots (net analyst sentiment shift) is the forward-test
revision-momentum read. Not scored until enough history accrues + a backtest
justifies promotion (Phase 3).
"""
from __future__ import annotations

import logging

from .._http import get_json

log = logging.getLogger(__name__)

FINNHUB = "https://finnhub.io/api/v1"


def recommendation(ticker: str, api_key: str | None) -> dict | None:
    """Latest analyst recommendation counts for a ticker. None if no key / on error."""
    if not api_key:
        return None
    data = get_json(f"{FINNHUB}/stock/recommendation",
                    params={"symbol": ticker, "token": api_key})
    if not isinstance(data, list) or not data:
        return None
    r = data[0]  # newest period first
    return {
        "period": r.get("period"),
        "strong_buy": r.get("strongBuy"), "buy": r.get("buy"), "hold": r.get("hold"),
        "sell": r.get("sell"), "strong_sell": r.get("strongSell"),
    }


def _rec_score(snap: dict) -> float:
    """Weighted net-bullishness of one recommendation snapshot."""
    return (2 * (snap.get("strong_buy") or 0) + (snap.get("buy") or 0)
            - (snap.get("sell") or 0) - 2 * (snap.get("strong_sell") or 0))


def revision_delta(two_snaps: list[dict]) -> dict | None:
    """Net analyst-sentiment change between the two most recent snapshots (newest
    first). None if fewer than two snapshots have accrued yet."""
    if not two_snaps or len(two_snaps) < 2:
        return None
    net = _rec_score(two_snaps[0]) - _rec_score(two_snaps[1])
    return {"net_delta": net, "from_period": two_snaps[1].get("period"),
            "to_period": two_snaps[0].get("period")}

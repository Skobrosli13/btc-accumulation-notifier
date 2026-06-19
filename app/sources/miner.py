"""Miner / hashrate signals — the Hash Ribbon.

Free, no key. Daily hashrate from mempool.space (primary) or blockchain.com
(fallback). The Hash Ribbon compares the 30-day vs 60-day hashrate moving
averages: when the 30d falls below the 60d the network is in *miner capitulation*
(unprofitable miners powering down), and the bullish signal is the RECOVERY — the
30d crossing back above the 60d — which has historically clustered near macro
bottoms (2015, late-2018, Mar-2020, mid/late-2022, 2024 post-halving shakeout).

This adapter emits ONE [0,1] reading ``hash_ribbon``:
  1.0  fresh recovery out of a recent capitulation (the classic buy window)
  0.3  currently in capitulation (stress; watch for the cross)
  0.0  normal healthy regime
The scorer maps it through an identity threshold band — the regime logic lives
here because this is a cross/regime read, not a monotonic level. Scoring raw "gap
depth" would peak at MAXIMUM capitulation, which is backwards: the buy is the
recovery, not the bleed.
"""
from __future__ import annotations

import logging

from ._http import get_json

log = logging.getLogger(__name__)

MEMPOOL_HASHRATE = "https://mempool.space/api/v1/mining/hashrate/1y"
BLOCKCHAIN_HASHRATE = "https://api.blockchain.info/charts/hash-rate"

_NONE = {"hash_ribbon": None}


def _mempool_series() -> list[float]:
    """Daily avg hashrate (H/s), oldest->newest, from mempool.space. [] on failure."""
    data = get_json(MEMPOOL_HASHRATE)
    if not isinstance(data, dict):
        return []
    out: list[float] = []
    for row in data.get("hashrates") or []:
        v = row.get("avgHashrate") if isinstance(row, dict) else None
        if v is not None:
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                continue
    return out


def _blockchain_series() -> list[float]:
    """Daily hashrate (TH/s), oldest->newest, from blockchain.com. [] on failure.

    Units differ from mempool (TH/s vs H/s) but the Hash Ribbon uses only the
    30d/60d RATIO, so the absolute scale is irrelevant.
    """
    data = get_json(BLOCKCHAIN_HASHRATE, params={"timespan": "1year", "format": "json"})
    if not isinstance(data, dict):
        return []
    out: list[float] = []
    for row in data.get("values") or []:
        v = row.get("y") if isinstance(row, dict) else None
        if v is not None:
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                continue
    return out


def _ribbon_score(series: list[float], *, short: int = 30, long_: int = 60,
                  lookback: int = 90) -> float | None:
    """Map a daily hashrate series to the [0,1] Hash Ribbon reading.

    Needs >= ``long_`` + 1 daily points; returns None otherwise (degrades).
    """
    if len(series) < long_ + 1:
        return None

    def sma_at(i: int, n: int) -> float | None:
        if i + 1 < n:
            return None
        return sum(series[i + 1 - n:i + 1]) / n

    last = len(series) - 1
    s30, s60 = sma_at(last, short), sma_at(last, long_)
    if s30 is None or s60 is None:
        return None
    recovering = s30 >= s60

    # Was there a capitulation (30d below 60d) anywhere in the recent window?
    capitulated = False
    start = max(long_ - 1, last - lookback)
    for i in range(start, last + 1):
        a, b = sma_at(i, short), sma_at(i, long_)
        if a is not None and b is not None and a < b:
            capitulated = True
            break

    if recovering and capitulated:
        return 1.0   # fresh recovery out of a recent capitulation = buy window
    if not recovering:
        return 0.3   # in capitulation now: stress, watch for the cross
    return 0.0       # healthy/normal regime


def hash_ribbon() -> dict:
    """Hash Ribbon reading keyed for the scorer. Fails soft to {"hash_ribbon": None}."""
    try:
        series = _mempool_series() or _blockchain_series()
        return {"hash_ribbon": _ribbon_score(series)}
    except Exception as exc:  # noqa: BLE001 - fail soft; never break the long-term run
        log.warning("hash_ribbon() failed (%s); miner layer skipped", exc)
        return dict(_NONE)

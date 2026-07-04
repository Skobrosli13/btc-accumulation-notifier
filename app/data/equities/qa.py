"""Data-quality detectors for the equities lake — §4.8.

Pure checks that surface silent corruption before it reaches a study: an
unadjusted split (a huge move with no corporate action), a coverage hole, a
frozen fundamental feed, a reused-ticker collision. Findings are dicts
``{check, ticker, ...}``; the ingest/QA job persists them to ``data_qa`` and
feeds ``/api/health``, and a red finding for a name blocks study runs on it.

Kept pure (series/date inputs) so the thresholds are fixture-tested; the lake
reads + persistence are orchestration.
"""
from __future__ import annotations

from datetime import date

import numpy as np

# Thresholds (all overridable per call).
SPIKE_RET = 0.60          # |1-day return| above this with no ACTION is suspicious
GAP_SESSIONS = 5          # more than this many missing sessions is a coverage hole
STALE_FUNDAMENTAL_DAYS = 400


def _to_date(d) -> date | None:
    if isinstance(d, date):
        return d
    try:
        return date.fromisoformat(str(d)[:10])
    except (ValueError, TypeError):
        return None


def detect_price_spikes(ticker: str, dates: list, closes: list[float],
                        action_dates=frozenset(), *, threshold: float = SPIKE_RET) -> list[dict]:
    """Flag |1-day return| > threshold on a day with NO corporate action — the
    signature of an unadjusted split/consolidation leaking into raw prices.
    ``action_dates`` is the set of ISO dates carrying a SHARADAR/ACTIONS row."""
    actions = {str(d)[:10] for d in action_dates}
    out: list[dict] = []
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if prev in (None, 0) or cur is None:
            continue
        ret = cur / prev - 1.0
        d = str(dates[i])[:10]
        if abs(ret) > threshold and d not in actions:
            out.append({"check": "price_spike", "ticker": ticker, "date": d,
                        "ret": round(ret, 4)})
    return out


def detect_gaps(ticker: str, dates: list, *, max_sessions: int = GAP_SESSIONS) -> list[dict]:
    """Flag consecutive available dates more than ``max_sessions`` trading days
    apart (business-day counted, so weekends/normal spacing don't false-positive)."""
    ds = [d for d in (_to_date(x) for x in dates) if d is not None]
    out: list[dict] = []
    for a, b in zip(ds, ds[1:]):
        # business days strictly between a and b (exclusive of a, exclusive of b)
        missing = int(np.busday_count(a.isoformat(), b.isoformat())) - 1
        if missing > max_sessions:
            out.append({"check": "gap", "ticker": ticker,
                        "from": a.isoformat(), "to": b.isoformat(), "missing_sessions": missing})
    return out


def detect_stale_fundamental(ticker: str, latest_datekey, as_of,
                             *, max_days: int = STALE_FUNDAMENTAL_DAYS) -> dict | None:
    """Flag a fundamental feed whose newest ``datekey`` is more than ``max_days``
    behind ``as_of`` (a delisted/frozen name that would otherwise score on stale data)."""
    ld, ao = _to_date(latest_datekey), _to_date(as_of)
    if ld is None or ao is None:
        return None
    age = (ao - ld).days
    if age > max_days:
        return {"check": "stale_fundamental", "ticker": ticker,
                "latest_datekey": ld.isoformat(), "age_days": age}
    return None


def detect_duplicate_permaticker(tickers_rows: list[dict]) -> list[dict]:
    """Flag a ticker currently mapping to more than one still-listed permaticker —
    an ambiguous join that needs PIT ticker-change resolution."""
    listed: dict[str, set] = {}
    for r in tickers_rows:
        if str(r.get("isdelisted", "")).upper() != "N":
            continue
        tk, pt = r.get("ticker"), r.get("permaticker")
        if tk and pt is not None:
            listed.setdefault(tk, set()).add(pt)
    return [{"check": "duplicate_permaticker", "ticker": tk,
             "permatickers": sorted(pts)}
            for tk, pts in listed.items() if len(pts) > 1]

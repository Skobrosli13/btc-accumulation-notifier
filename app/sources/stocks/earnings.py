"""Earnings adapter (Finnhub free tier) — the PEAD feed.

Two endpoints:
- ``/calendar/earnings?from&to`` returns EVERY US report in a date window in ONE
  call (efficient for a universe scan): date, epsActual, epsEstimate, hour, quarter.
- ``/stock/earnings?symbol`` is the per-symbol surprise history (for the backtest).

Free tier is 60 calls/min. Fail-soft: returns ``[]`` when no key / on any error, so
the screener degrades to the keyless technical archetypes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .._http import get_json  # app.sources.stocks -> app.sources._http

log = logging.getLogger(__name__)

FINNHUB = "https://finnhub.io/api/v1"


def _date_to_ms(datestr: str) -> int | None:
    try:
        d = datetime.strptime(datestr[:10], "%Y-%m-%d")
        return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _norm(row: dict) -> dict | None:
    """Normalize a Finnhub earnings row -> our stock_earnings shape (or None if no actual)."""
    sym = (row.get("symbol") or "").upper()
    actual, est = row.get("epsActual"), row.get("epsEstimate")
    rts = _date_to_ms(row.get("date", ""))
    if not sym or actual is None or rts is None:
        return None
    surprise = (actual - est) if est is not None else None
    surprise_pct = (surprise / abs(est) * 100.0) if (surprise is not None and est) else None
    # Revenue surprise (confluence with EPS strengthens the drift; divergence weakens it).
    rev_a, rev_e = row.get("revenueActual"), row.get("revenueEstimate")
    rev_surprise_pct = ((rev_a - rev_e) / abs(rev_e) * 100.0) if (rev_a is not None and rev_e) else None
    q, y = row.get("quarter"), row.get("year")
    period = f"{y}Q{q}" if (q and y) else row.get("date")
    return {"ticker": sym, "period": period, "report_ts": rts,
            "hour": (row.get("hour") or ""), "actual": actual, "estimate": est,
            "surprise": surprise, "surprise_pct": surprise_pct,
            "rev_actual": rev_a, "rev_estimate": rev_e, "rev_surprise_pct": rev_surprise_pct}


def earnings_calendar(api_key: str | None, from_date: str, to_date: str) -> list[dict]:
    """All US earnings WITH a reported actual in [from_date, to_date] (YYYY-MM-DD).
    One request covers the whole universe. [] if no key / on failure."""
    if not api_key:
        return []
    data = get_json(f"{FINNHUB}/calendar/earnings",
                    params={"from": from_date, "to": to_date, "token": api_key})
    rows = (data or {}).get("earningsCalendar") or []
    out = []
    for r in rows:
        n = _norm(r)
        if n:
            out.append(n)
    return out


def surprise_history(ticker: str, api_key: str | None, limit: int = 12) -> list[dict]:
    """Per-symbol actual-vs-estimate history (newest first) for the backtest. []."""
    if not api_key:
        return []
    data = get_json(f"{FINNHUB}/stock/earnings",
                    params={"symbol": ticker, "limit": limit, "token": api_key})
    if not isinstance(data, list):
        return []
    out = []
    for r in data:
        rts = _date_to_ms(r.get("period", ""))
        actual, est = r.get("actual"), r.get("estimate")
        if rts is None or actual is None:
            continue
        surprise = (actual - est) if est is not None else None
        out.append({
            "ticker": ticker, "period": r.get("period"), "report_ts": rts, "hour": "",
            "actual": actual, "estimate": est, "surprise": surprise,
            "surprise_pct": (r.get("surprisePercent")
                             if r.get("surprisePercent") is not None
                             else (surprise / abs(est) * 100.0 if surprise is not None and est else None)),
            # /stock/earnings has no revenue; backtest PEAD runs without rev confluence
            "rev_actual": None, "rev_estimate": None, "rev_surprise_pct": None,
        })
    return out

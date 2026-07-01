"""Earnings adapter (Finnhub free tier) — the PEAD feed.

Two endpoints:
- ``/calendar/earnings?from&to[&symbol]`` returns reports in a date window with the
  ANNOUNCEMENT ``date`` plus ``hour`` (bmo/amc), epsActual/epsEstimate and revenue.
  This is the only endpoint whose dates are safe to align price bars against.
- ``/stock/earnings?symbol`` is the per-symbol surprise history, but its ``period``
  field is the FISCAL QUARTER END (the announcement lands weeks later), so it is
  never usable as a report date — it is only joined on (year, quarter) to backfill
  a missing actual/estimate on a calendar row.

Free tier is 60 calls/min. Fail-soft: returns ``[]`` when no key / on any error, so
the screener degrades to the keyless technical archetypes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .._http import get_json  # app.sources.stocks -> app.sources._http

log = logging.getLogger(__name__)

FINNHUB = "https://finnhub.io/api/v1"

_HISTORY_WINDOW_DAYS = 90    # one calendar page ~ a fiscal quarter
_MAX_HISTORY_WINDOWS = 24    # hard cap on paging (~6 years)


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


def _quarter_key(row: dict) -> tuple[int, int] | None:
    q, y = row.get("quarter"), row.get("year")
    try:
        return (int(y), int(q)) if (q and y) else None
    except (TypeError, ValueError):
        return None


def surprise_history(ticker: str, api_key: str | None, years: float = 4.5) -> list[dict]:
    """Announcement-dated actual-vs-estimate history (newest first) for the backtest.

    Pages ``/calendar/earnings`` per symbol over quarterly windows — its ``date`` is
    the real announcement date and it carries ``hour`` (bmo/amc), so the reaction
    session and the drift window start where the market actually learned the number.
    A calendar row missing actual/estimate is backfilled from ``/stock/earnings``
    joined on (year, quarter); the fiscal ``period`` end date from that endpoint is
    deliberately never used as ``report_ts``. [] when no key / on failure."""
    if not api_key:
        return []
    # Join feed: per-symbol surprises keyed by fiscal (year, quarter).
    hist = get_json(f"{FINNHUB}/stock/earnings",
                    params={"symbol": ticker, "limit": 60, "token": api_key})
    by_quarter: dict[tuple[int, int], dict] = {}
    for r in (hist if isinstance(hist, list) else []):
        k = _quarter_key(r)
        if k:
            by_quarter[k] = r
    out: list[dict] = []
    end = datetime.now(timezone.utc)
    windows = max(1, min(_MAX_HISTORY_WINDOWS, int(years * 365 / _HISTORY_WINDOW_DAYS) + 1))
    for w in range(windows):
        to_d = end - timedelta(days=w * _HISTORY_WINDOW_DAYS)
        frm_d = to_d - timedelta(days=_HISTORY_WINDOW_DAYS - 1)
        data = get_json(f"{FINNHUB}/calendar/earnings",
                        params={"from": frm_d.strftime("%Y-%m-%d"),
                                "to": to_d.strftime("%Y-%m-%d"),
                                "symbol": ticker, "token": api_key})
        for r in (data or {}).get("earningsCalendar") or []:
            if (r.get("symbol") or "").upper() != ticker.upper():
                continue
            joined = by_quarter.get(_quarter_key(r) or (-1, -1)) or {}
            merged = dict(r)
            if merged.get("epsActual") is None:
                merged["epsActual"] = joined.get("actual")
            if merged.get("epsEstimate") is None:
                merged["epsEstimate"] = joined.get("estimate")
            n = _norm(merged)
            if n:
                out.append(n)
    # De-dup window-edge overlaps; newest first.
    seen: set[tuple] = set()
    uniq: list[dict] = []
    for n in sorted(out, key=lambda r: r["report_ts"], reverse=True):
        key = (n["report_ts"], n["period"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(n)
    return uniq


_QUARTER_END_DAYS = {(3, 31), (6, 30), (9, 30), (12, 31)}


def quarter_end_fraction(report_ts_values) -> float:
    """Fraction of report timestamps landing on the LAST day of a calendar quarter.

    Real announcement dates trail the fiscal quarter end by weeks, so a map
    dominated by Mar 31 / Jun 30 / Sep 30 / Dec 31 is the signature of the fiscal
    period end being mistaken for the announcement date (look-ahead). 0.0 when empty."""
    vals = list(report_ts_values)
    if not vals:
        return 0.0
    qe = 0
    for ts in vals:
        d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        if (d.month, d.day) in _QUARTER_END_DAYS:
            qe += 1
    return qe / len(vals)

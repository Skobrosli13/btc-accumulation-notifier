"""massive.com data adapter (Polygon.io-shaped US market data).

Free tier (key-gated): delayed EOD, 5 calls/min. We use it for what it uniquely
gives us cheaply:
- **financials** — full SEC-standardized statements (income / balance sheet / cash
  flow) → the long-term fundamentals engine.
- **ticker reference** — market cap + sector.
- **grouped daily** — whole-market EOD in ONE call (append-daily price source;
  optional, since the swing collector already maintains stock_prices via Yahoo).
- **daily aggregates** — per-ticker history for gap-fill.

Fail-soft: every function returns None/[]/{} on any failure and never raises.
Massive is a newer provider — treat as primary-when-keyed but keep Yahoo/Finnhub
fallbacks (not a single point of failure).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .._http import get_json

log = logging.getLogger(__name__)

BASE = "https://api.massive.com"


def _date_to_ms(datestr: str) -> int | None:
    try:
        d = datetime.strptime(datestr[:10], "%Y-%m-%d")
        return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def financials(ticker: str, key: str | None, limit: int = 2,
               timeframe: str = "annual") -> list[dict]:
    """Raw financial-statement periods (newest first) for a ticker. [] on failure.

    Each item: {fiscal_period, fiscal_year, start_date, end_date,
                financials: {income_statement/balance_sheet/cash_flow_statement/...:
                             {field: {value, unit, label}}}}.
    ``timeframe='annual'`` (for Piotroski/YoY deltas) or 'quarterly'/'ttm'."""
    if not key:
        return []
    data = get_json(f"{BASE}/vX/reference/financials",
                    params={"ticker": ticker, "limit": limit, "timeframe": timeframe,
                            "order": "desc", "sort": "period_of_report_date", "apiKey": key})
    return (data or {}).get("results") or []


def ticker_reference(ticker: str, key: str | None) -> dict | None:
    """Reference/overview: market_cap, sector (sic), shares, name. None on failure."""
    if not key:
        return None
    data = get_json(f"{BASE}/v3/reference/tickers/{ticker}", params={"apiKey": key})
    r = (data or {}).get("results")
    if not r:
        return None
    return {"market_cap": r.get("market_cap"), "name": r.get("name"),
            "sic": r.get("sic_code"), "sic_desc": r.get("sic_description"),
            "shares": r.get("weighted_shares_outstanding") or r.get("share_class_shares_outstanding"),
            "primary_exchange": r.get("primary_exchange"), "active": r.get("active")}


def grouped_daily(date_str: str, key: str | None) -> dict[str, tuple]:
    """Whole-market EOD bars for a date in ONE call -> {TICKER: (ts_ms,o,h,l,c,v)}.
    {} on failure. ``date_str`` = 'YYYY-MM-DD'."""
    if not key:
        return {}
    data = get_json(f"{BASE}/v2/aggs/grouped/locale/us/market/stocks/{date_str}",
                    params={"adjusted": "true", "apiKey": key})
    out: dict[str, tuple] = {}
    for r in (data or {}).get("results") or []:
        try:
            out[r["T"].upper()] = (int(r["t"]), r["o"], r["h"], r["l"], r["c"], r.get("v", 0))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def daily_bars(ticker: str, key: str | None, start: str, end: str) -> list[tuple] | None:
    """Per-ticker daily aggregates [(ts_ms,o,h,l,c,v), ...] oldest->newest (gap-fill).
    None on failure. start/end = 'YYYY-MM-DD'."""
    if not key:
        return None
    data = get_json(f"{BASE}/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
                    params={"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": key})
    res = (data or {}).get("results")
    if not res:
        return None
    out = []
    for r in res:
        try:
            out.append((int(r["t"]), r["o"], r["h"], r["l"], r["c"], r.get("v", 0)))
        except (KeyError, TypeError, ValueError):
            continue
    return out or None


def news(ticker: str, key: str | None, limit: int = 5) -> list[dict]:
    """Recent news headlines for a ticker. [] on failure."""
    if not key:
        return []
    data = get_json(f"{BASE}/v2/reference/news",
                    params={"ticker": ticker, "limit": limit, "apiKey": key})
    out = []
    for r in (data or {}).get("results") or []:
        out.append({"title": r.get("title"), "publisher": (r.get("publisher") or {}).get("name"),
                    "published_utc": r.get("published_utc"), "url": r.get("article_url")})
    return out

"""US spot-BTC-ETF net flows (best-effort).

Order of preference:
  1. SoSoValue API  (if SOSOVALUE_API_KEY is set)
  2. Farside scrape (https://farside.co.uk/btc/ — an HTML page, no clean API)
  3. skip (return None)

Reading is the trailing ~30-day net flow in USD billions; persistent inflows
during a drawdown are bullish. This indicator is the flakiest of the free set —
it must never break the run, and silently degrades to None.
"""
from __future__ import annotations

import logging

from ._http import get_json, get_text

log = logging.getLogger(__name__)

SOSOVALUE_URL = "https://api.sosovalue.xyz/openapi/v2/etf/historicalInflowChart"
FARSIDE_URL = "https://farside.co.uk/btc/"
_TRAILING_DAYS = 30


def _from_sosovalue(api_key: str) -> float | None:
    """Best-effort SoSoValue call. Returns trailing net flow in $bn or None."""
    data = get_json(
        SOSOVALUE_URL,
        params={"type": "us-btc-spot"},
        headers={"x-soso-api-key": api_key},
    )
    if not data:
        return None
    try:
        # API shape varies; accept a list of {date, totalNetInflow} under data/result.
        rows = data.get("data") or data.get("result") or []
        if isinstance(rows, dict):
            rows = rows.get("list") or rows.get("data") or []
        vals = []
        for r in rows[-_TRAILING_DAYS:]:
            v = r.get("totalNetInflow", r.get("netInflow"))
            if v is not None:
                vals.append(float(v))
        if not vals:
            return None
        # SoSoValue reports USD; convert to $bn.
        return sum(vals) / 1e9
    except (KeyError, TypeError, ValueError):
        return None


def _from_farside() -> float | None:
    """Best-effort Farside HTML scrape. Needs an lxml/html5lib parser for
    pandas.read_html; if unavailable, returns None rather than failing."""
    html = get_text(FARSIDE_URL, headers={"User-Agent": "Mozilla/5.0 (compatible; btc-accum/1.0)"})
    if not html:
        return None
    try:
        import pandas as pd

        tables = pd.read_html(html)  # may raise if no parser installed
    except Exception as exc:  # noqa: BLE001
        log.info("Farside parse unavailable (%s); skipping ETF flows", exc)
        return None

    try:
        # The main table has a 'Total' column of daily net flows in $m.
        for t in tables:
            cols = [str(c) for c in t.columns.get_level_values(-1)] \
                if hasattr(t.columns, "get_level_values") else [str(c) for c in t.columns]
            total_col = next((c for c in t.columns if "Total" in str(c)), None)
            if total_col is None:
                continue
            series = pd.to_numeric(
                t[total_col].astype(str).str.replace(",", "").str.replace("(", "-").str.replace(")", ""),
                errors="coerce",
            ).dropna()
            tail = series.tail(_TRAILING_DAYS)
            if tail.empty:
                continue
            return float(tail.sum()) / 1000.0  # $m -> $bn
        return None
    except Exception as exc:  # noqa: BLE001
        log.info("Farside table extraction failed (%s); skipping ETF flows", exc)
        return None


def etf_flows() -> dict:
    """Return {'etf_flow': <trailing net flow $bn>} or {'etf_flow': None}."""
    from ..config import load_config

    cfg = load_config()
    if cfg.sosovalue_api_key:
        val = _from_sosovalue(cfg.sosovalue_api_key)
        if val is not None:
            return {"etf_flow": val}
    return {"etf_flow": _from_farside()}

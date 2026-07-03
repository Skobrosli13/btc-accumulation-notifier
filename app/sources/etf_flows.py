"""US spot-BTC-ETF net flows (best-effort, free-only).

Order of preference:
  1. Farside scrape (https://farside.co.uk/btc/ — an HTML page, no clean API)
  2. skip (return None)

Reading is the trailing ~30-day net flow in USD billions; persistent inflows
during a drawdown are bullish. This indicator is the flakiest of the free set —
it must never break the run, and silently degrades to None.

(The paid SoSoValue path was removed in Phase 0 — BTC data is free-only by owner
decision; the ``btc_etf_flow`` ALPHA study sources flows the same free way.)
"""
from __future__ import annotations

import logging

from ._http import get_text

log = logging.getLogger(__name__)

FARSIDE_URL = "https://farside.co.uk/btc/"
_TRAILING_DAYS = 30


def _from_farside() -> float | None:
    """Best-effort Farside HTML scrape. Needs an lxml/html5lib parser for
    pandas.read_html; if unavailable, returns None rather than failing."""
    html = get_text(FARSIDE_URL, headers={"User-Agent": "Mozilla/5.0 (compatible; btc-accum/1.0)"})
    if not html:
        return None
    try:
        import io

        import pandas as pd

        # pandas >= 2.x deprecated and 3.x REMOVED passing a literal HTML string
        # to read_html (it now treats the arg as a path -> FileNotFoundError).
        # Wrap the body in a StringIO so the scrape keeps working across versions.
        tables = pd.read_html(io.StringIO(html))  # may raise if no parser installed
    except Exception as exc:  # noqa: BLE001
        log.info("Farside parse unavailable (%s); skipping ETF flows", exc)
        return None

    try:
        import pandas as pd

        # The main table has a 'Total' column of daily net flows in $m, plus a
        # first column of dates and a handful of FOOTER rows (Total / Average /
        # Maximum / Minimum) at the bottom. Those footers survive to_numeric and,
        # if included, fold the all-time cumulative total into the trailing-30d
        # window — pegging the sub-score at max-bullish every run. We therefore
        # keep ONLY rows whose date column parses to a real date.
        for t in tables:
            total_col = next((c for c in t.columns if "Total" in str(c)), None)
            if total_col is None:
                continue

            # Date column: normally the first column. Parse it; rows that don't
            # parse to a date (the footer labels, blank separators) are dropped.
            date_col = t.columns[0]
            dates = pd.to_datetime(t[date_col].astype(str), errors="coerce",
                                   dayfirst=True)
            daily = t[dates.notna()]
            if daily.empty:
                continue

            series = pd.to_numeric(
                daily[total_col].astype(str)
                .str.replace(",", "", regex=False)
                .str.replace("(", "-", regex=False)
                .str.replace(")", "", regex=False),
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
    """Return {'etf_flow': <trailing net flow $bn>} or {'etf_flow': None}.

    Blanket-wrapped so this public entry point can ONLY return its normal dict
    (never raise) into run_once.gather_readings — matching onchain()/derivatives().
    """
    try:
        return {"etf_flow": _from_farside()}
    except Exception as exc:  # noqa: BLE001 - fail soft; never break the long-term run
        log.warning("etf_flows() failed (%s); ETF flow reading skipped", exc)
        return {"etf_flow": None}

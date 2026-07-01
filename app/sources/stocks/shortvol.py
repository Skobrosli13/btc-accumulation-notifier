"""FINRA daily short-volume adapter (keyless flat files).

One request returns the WHOLE consolidated-tape short-volume file for a date
(pipe-delimited), so a universe scan is a single fetch. HONEST caveat carried into
the UI: this is short-sale *volume* (off-exchange / media-reported only), NOT short
*interest* (outstanding positions, reported bi-monthly). A rising short-volume
ratio with price strength is squeeze fuel; it is a context layer, never a driver.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .._http import get_text

log = logging.getLogger(__name__)

CNMS_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{ymd}.txt"
_UA = "riverviewweb-signal stock tracker admin@riverviewweb.com"


def _midnight_ms(d: datetime) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _parse(txt: str) -> dict[str, dict]:
    """Parse a CNMS short-volume file body -> {TICKER: {short_vol, short_exempt, total_vol}}."""
    out: dict[str, dict] = {}
    for line in txt.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 5 or parts[0].lower() == "date" or not parts[0].isdigit():
            continue
        sym = parts[1].upper()
        try:
            out[sym] = {"short_vol": float(parts[2]), "short_exempt": float(parts[3]),
                        "total_vol": float(parts[4])}
        except (ValueError, IndexError):
            continue
    return out


def short_volume_for(date: datetime, user_agent: str = _UA) -> tuple[int, dict[str, dict]] | None:
    """(bar_ts_ms, {ticker: {...}}) for a specific date, or None if no file that day."""
    ymd = date.strftime("%Y%m%d")
    txt = get_text(CNMS_URL.format(ymd=ymd), headers={"User-Agent": user_agent})
    if not txt or "|" not in txt:
        return None
    parsed = _parse(txt)
    if not parsed:
        return None
    return _midnight_ms(date), parsed


def latest_short_volume(user_agent: str = _UA, lookback_days: int = 6
                        ) -> tuple[int, dict[str, dict]] | None:
    """Most recent available daily short-volume file, walking back from today.
    (Files lag ~1 day and skip weekends/holidays.) None if none found in the window."""
    today = datetime.now(timezone.utc)
    for i in range(lookback_days):
        res = short_volume_for(today - timedelta(days=i), user_agent)
        if res is not None:
            return res
    return None


def short_ratio(row: dict | None) -> float | None:
    """short_vol / total_vol for one ticker's row, or None."""
    if not row or not row.get("total_vol"):
        return None
    return row["short_vol"] / row["total_vol"]

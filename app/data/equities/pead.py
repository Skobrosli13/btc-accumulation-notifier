"""PEAD events from SRW-SUE + EDGAR announcement timing (§4.5/§6-2).

Ties the two free EDGAR reads together into a tradeable earnings event:
  * the **surprise magnitude** — the standardized SUE from XBRL diluted EPS
    (:mod:`app.data.equities.edgar.xbrl_eps`);
  * the **event timestamp** — the 8-K/Item-2.02 acceptance instant + BMO/AMC
    session (:mod:`app.sources.stocks.edgar_earnings`).

The join is by DATE, PIT-safe: a SUE quarter ending on date E is matched to the
FIRST earnings announcement on/after E (within a quarter's window), so a surprise
is only ever paired with the announcement that actually revealed it — never a
later restatement or the wrong fiscal period.

``sue_events`` returns ``[{report_ts, hour, sue, fy, quarter, period_end}]``
newest-first — the shape the screener's PEAD archetype and the sue_pead study
both consume. Pure ``match_events`` is fixture-tested; the two fetches are thin.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ...sources.stocks import edgar_earnings
from .edgar import xbrl_eps

log = logging.getLogger(__name__)

# A quarter's earnings 8-K lands within ~this many days after the period end.
_MATCH_WINDOW_DAYS = 120


def _end_ms(iso_end: str) -> int | None:
    try:
        d = datetime.strptime(str(iso_end)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(d.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def match_events(sue: dict[tuple[int, int], float],
                 ends: dict[tuple[int, int], str],
                 announcements: list[dict]) -> list[dict]:
    """Join each SUE quarter to the first announcement on/after its period end (pure).

    ``announcements`` are ``{report_ts, hour, ...}`` (edgar_earnings shape). Each
    is used at most once (earliest unclaimed within the window), so two close
    quarters can't collide onto the same 8-K.
    """
    anns = sorted((a for a in announcements if a.get("report_ts") is not None),
                  key=lambda a: a["report_ts"])
    used = [False] * len(anns)
    window_ms = _MATCH_WINDOW_DAYS * 86_400_000
    out: list[dict] = []
    for (fy, q), end in sorted(ends.items()):
        if (fy, q) not in sue:
            continue
        e_ms = _end_ms(end)
        if e_ms is None:
            continue
        for i, a in enumerate(anns):
            if used[i]:
                continue
            if e_ms <= a["report_ts"] <= e_ms + window_ms:
                used[i] = True
                out.append({"report_ts": a["report_ts"], "hour": a.get("hour", ""),
                            "sue": sue[(fy, q)], "fy": fy, "quarter": q,
                            "period_end": str(end)[:10]})
                break
    out.sort(key=lambda ev: ev["report_ts"], reverse=True)
    return out


def sue_events(cik: str, user_agent: str, *, adjust=None) -> list[dict]:
    """End-to-end PEAD events for a CIK from free EDGAR. [] on any failure."""
    payload = xbrl_eps.fetch_diluted_eps(cik, user_agent)
    if not payload:
        return []
    sue = xbrl_eps.seasonal_sue(xbrl_eps.parse_companyconcept(payload), adjust=adjust)
    ends = xbrl_eps.period_ends(payload)
    anns = edgar_earnings.announcement_dates("", user_agent=user_agent, cik=cik)
    return match_events(sue, ends, anns)

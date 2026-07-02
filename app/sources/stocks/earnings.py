"""Earnings adapter (Finnhub free tier) — the PEAD feed.

Two endpoints:
- ``/calendar/earnings?from&to[&symbol]`` returns reports in a date window with the
  ANNOUNCEMENT ``date`` plus ``hour`` (bmo/amc), epsActual/epsEstimate and revenue.
  This is the only endpoint whose dates are safe to align price bars against.
- ``/stock/earnings?symbol`` is the per-symbol surprise history, but its ``period``
  field is the FISCAL QUARTER END (the announcement lands weeks later), so it is
  never usable as a report date — it is only joined on (year, quarter) to backfill
  a missing actual/estimate on a calendar row.

Free tier is 60 calls/min, so every Finnhub call in this module is paced to
<= 55/min (monotonic-clock throttle) — the paged history pull alone is ~25
calls/ticker and an unpaced multi-ticker backtest regen would silently lose
windows to 429s. Fail-soft: returns ``[]`` when no key / on any error, so
the screener degrades to the keyless technical archetypes.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from .._http import get_json  # app.sources.stocks -> app.sources._http
from . import edgar_earnings

log = logging.getLogger(__name__)

FINNHUB = "https://finnhub.io/api/v1"

_HISTORY_WINDOW_DAYS = 90    # one calendar page ~ a fiscal quarter
_MAX_HISTORY_WINDOWS = 24    # hard cap on paging (~6 years)
_MIN_INTERVAL_S = 60.0 / 55.0   # pace Finnhub calls to <= 55/min (free tier: 60/min)
_MIN_WINDOW_COVERAGE = 0.90  # <90% of history windows answered -> drop the ticker
_last_call = 0.0             # monotonic ts of the last Finnhub call (module-wide)

# When the announcement-dated calendar yields FEWER than this many rows we treat
# it as free-tier-empty (the free tier answers only FUTURE windows, so every
# historical window returns []) and fall back to EDGAR announcement dates joined
# to /stock/earnings surprises. 1 => only a genuinely EMPTY calendar triggers the
# fallback; a paid tier returning even one historical row keeps the calendar path
# (its dates and hour are authoritative and its rev-surprise is richer than the
# EDGAR join can reconstruct).
_MIN_CALENDAR_ROWS = 1
# An EDGAR announcement is joined to the Finnhub surprise row whose fiscal
# period-END is the nearest one at/just before it. Real reports land ~2-6 weeks
# after quarter-end; this cap rejects an accidental match to a stale quarter.
_MAX_PERIOD_LAG_DAYS = 120


def _pace() -> None:
    """Monotonic-clock throttle: sleep so consecutive Finnhub calls stay
    >= ``_MIN_INTERVAL_S`` apart (<= 55 calls/min against the 60/min free tier)."""
    global _last_call
    if _MIN_INTERVAL_S <= 0:
        return
    wait = _last_call + _MIN_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


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
    _pace()
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


def _fetch_surprise_rows(ticker: str, api_key: str) -> list[dict]:
    """Finnhub ``/stock/earnings`` per-symbol surprise history (raw list). [] on failure.

    These carry actual/estimate/surprisePercent keyed by fiscal (year, quarter),
    with ``period`` = the fiscal-quarter-END date (weeks before the announcement).
    Used both to backfill the calendar path and as the surprise feed for the EDGAR
    join — but its ``period`` is NEVER used as ``report_ts``."""
    _pace()
    hist = get_json(f"{FINNHUB}/stock/earnings",
                    params={"symbol": ticker, "limit": 60, "token": api_key})
    return hist if isinstance(hist, list) else []


def _norm_surprise_row(ticker: str, sur: dict, report_ts: int, hour: str) -> dict | None:
    """Shape a ``/stock/earnings`` surprise row into our schema using an EDGAR-sourced
    announcement ``report_ts``/``hour`` (NEVER the fiscal period end). None w/o an actual.

    Emits the SAME keys as ``_norm`` so the two paths are interchangeable downstream;
    revenue-surprise fields are None (``/stock/earnings`` carries EPS only)."""
    actual, est = sur.get("actual"), sur.get("estimate")
    if actual is None:
        return None
    surprise = (actual - est) if est is not None else None
    surprise_pct = sur.get("surprisePercent")
    if surprise_pct is None and surprise is not None and est:
        surprise_pct = surprise / abs(est) * 100.0
    q, y = sur.get("quarter"), sur.get("year")
    period = f"{y}Q{q}" if (q and y) else sur.get("period")
    return {"ticker": ticker.upper(), "period": period, "report_ts": report_ts,
            "hour": hour or "", "actual": actual, "estimate": est,
            "surprise": surprise, "surprise_pct": surprise_pct,
            "rev_actual": None, "rev_estimate": None, "rev_surprise_pct": None}


def _period_end_ms(sur: dict) -> int | None:
    """Epoch-ms of a ``/stock/earnings`` row's fiscal ``period`` (quarter-END date)."""
    return _date_to_ms(sur.get("period", "") or "")


def _edgar_fallback(ticker: str, surprise_rows: list[dict],
                    sec_user_agent: str | None) -> list[dict]:
    """EDGAR announcement dates JOINED to Finnhub /stock/earnings surprises.

    Free-tier ``/calendar/earnings`` only answers FUTURE windows, so the historical
    announcement dates come from SEC 8-K/Item-2.02 filings. Each EDGAR announcement
    is joined to the surprise row whose fiscal period-END is the nearest one AT or
    BEFORE it (within ``_MAX_PERIOD_LAG_DAYS``) — robust to offset fiscal years
    (NVDA Jan-end, AAPL Sep-end) where a Feb announcement is a prior-FY Q4.

    Coverage guard (mirrors the calendar path but measured over quarters that have
    BOTH an EDGAR date AND a Finnhub surprise): below ``_MIN_WINDOW_COVERAGE`` of
    the joinable EDGAR announcements matched to a surprise, the whole history is
    dropped so a lopsided partial record can't pass for complete. [] on failure."""
    ann = edgar_earnings.announcement_dates(ticker, user_agent=sec_user_agent)
    if not ann or not surprise_rows:
        return []
    # Surprise rows sorted by fiscal period-end (oldest first) for a nearest-preceding
    # match; keep only those we can date.
    dated = [(_period_end_ms(s), s) for s in surprise_rows]
    dated = sorted(((ms, s) for ms, s in dated if ms is not None), key=lambda t: t[0])
    if not dated:
        return []
    lag_ms = _MAX_PERIOD_LAG_DAYS * 86_400_000
    out: list[dict] = []
    matched = 0
    used_periods: set[int] = set()
    for a in ann:
        rts = a["report_ts"]
        # Best = the latest period-end that is <= the announcement and within the lag.
        best = None
        for pe_ms, s in dated:
            if pe_ms <= rts and (rts - pe_ms) <= lag_ms:
                best = (pe_ms, s)          # dated is ascending -> keeps the latest valid
            elif pe_ms > rts:
                break
        if best is None:
            continue
        pe_ms, sur = best
        if pe_ms in used_periods:          # one announcement per fiscal period
            continue
        n = _norm_surprise_row(ticker, sur, rts, a.get("hour", ""))
        if n is None:
            continue
        used_periods.add(pe_ms)
        matched += 1
        out.append(n)
    # Coverage guard measured over distinct fiscal QUARTERS that have BOTH an EDGAR
    # date and a joinable Finnhub surprise. Counting announcements (not period-ends)
    # would let a single stray Item-2.02 (a mid-quarter guidance pre-announcement or
    # corrective 8-K sharing a quarter's period-end) inflate the denominator and drop
    # an otherwise-clean ticker; N clean quarters + 1 stray then reads N/(N+1) instead
    # of N/N. Deep EDGAR history beyond the limited /stock/earnings window has no
    # surprise to join and is legitimately unmatched, so it never enters the count.
    joinable_periods = {
        pe for pe, _ in dated
        if any(pe <= a["report_ts"] and (a["report_ts"] - pe) <= lag_ms for a in ann)
    }
    joinable = len(joinable_periods)
    if joinable and matched / joinable < _MIN_WINDOW_COVERAGE:
        log.warning("%s: only %d/%d joinable EDGAR announcements matched a Finnhub "
                    "surprise — DROPPING the ticker's history (partial join)", ticker,
                    matched, joinable)
        return []
    return sorted(out, key=lambda r: r["report_ts"], reverse=True)


def surprise_history(ticker: str, api_key: str | None, years: float = 4.5,
                     sec_user_agent: str | None = None) -> list[dict]:
    """Announcement-dated actual-vs-estimate history (newest first) for the backtest.

    Paid tier: pages ``/calendar/earnings`` per symbol over quarterly windows — its
    ``date`` is the real announcement date with ``hour`` (bmo/amc), so the reaction
    session and drift window start where the market actually learned the number.
    A calendar row missing actual/estimate is backfilled from ``/stock/earnings``
    joined on (year, quarter); that endpoint's fiscal ``period`` end date is
    deliberately never used as ``report_ts``.

    Free tier: ``/calendar/earnings`` only answers FUTURE windows, so every
    historical window comes back empty. When the calendar yields fewer than
    ``_MIN_CALENDAR_ROWS`` rows we FALL BACK to SEC EDGAR 8-K/Item-2.02 announcement
    dates joined to the ``/stock/earnings`` surprises (see ``_edgar_fallback``). The
    return schema and the coverage-drop guard are preserved either way. [] when no
    key / on failure.

    Coverage guard (calendar path): a window whose request FAILED (``get_json`` ->
    None, e.g. a 429 after retries) is not the same as a window with no reports. If
    fewer than ``_MIN_WINDOW_COVERAGE`` of the requested windows answered, the whole
    history is dropped ([]), so a partial, rate-limit-biased record can't masquerade
    as a complete one downstream (the backtest PEAD map)."""
    if not api_key:
        return []
    # Join feed: per-symbol surprises keyed by fiscal (year, quarter).
    surprise_rows = _fetch_surprise_rows(ticker, api_key)
    by_quarter: dict[tuple[int, int], dict] = {}
    for r in surprise_rows:
        k = _quarter_key(r)
        if k:
            by_quarter[k] = r
    out: list[dict] = []
    end = datetime.now(timezone.utc)
    windows = max(1, min(_MAX_HISTORY_WINDOWS, int(years * 365 / _HISTORY_WINDOW_DAYS) + 1))

    def _window(w: int):
        to_d = end - timedelta(days=w * _HISTORY_WINDOW_DAYS)
        frm_d = to_d - timedelta(days=_HISTORY_WINDOW_DAYS - 1)
        _pace()
        return get_json(f"{FINNHUB}/calendar/earnings",
                        params={"from": frm_d.strftime("%Y-%m-%d"),
                                "to": to_d.strftime("%Y-%m-%d"),
                                "symbol": ticker, "token": api_key})

    def _merge(data: dict) -> None:
        for r in data.get("earningsCalendar") or []:
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

    # Fast free-tier detection folded into the first window (no extra call on the paid
    # tier): the calendar answers only FUTURE windows on the free tier, so a recent
    # PAST window that comes back answered-but-empty is the free-tier signature — skip
    # straight to EDGAR instead of paging ~25 empty rate-limit-paced windows. A failed
    # (None) first window is ambiguous and pages on as a normal failure.
    failed = 0
    first = _window(0)
    if first is not None and not (first.get("earningsCalendar") or []):
        edgar = _edgar_fallback(ticker, surprise_rows, sec_user_agent)
        return edgar if edgar else []
    if first is None:
        failed += 1
    else:
        _merge(first)
    for w in range(1, windows):
        data = _window(w)
        if data is None:            # request failed — distinct from an empty window
            failed += 1
            continue
        _merge(data)
    # De-dup window-edge overlaps; newest first.
    seen: set[tuple] = set()
    uniq: list[dict] = []
    for n in sorted(out, key=lambda r: r["report_ts"], reverse=True):
        key = (n["report_ts"], n["period"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(n)
    # Calendar coverage guard: a rate-limited partial record must NOT masquerade as
    # complete — and must NOT silently be swapped for EDGAR either (that would hide a
    # transient outage). This is distinct from the free-tier signature (windows all
    # ANSWERED, just empty), which has failed==0 and falls through to EDGAR below.
    coverage_bad = bool(failed) and (windows - failed) / windows < _MIN_WINDOW_COVERAGE
    if coverage_bad:
        log.warning("%s: only %d/%d earnings-history windows answered (rate-limited?) — "
                    "DROPPING the ticker's history so a partial record can't pass for a "
                    "complete one", ticker, windows - failed, windows)
        return []
    # Free-tier empty calendar (every window answered but returned no historical
    # rows) -> fall back to EDGAR announcement dates joined to the surprises.
    if len(uniq) < _MIN_CALENDAR_ROWS:
        edgar = _edgar_fallback(ticker, surprise_rows, sec_user_agent)
        if edgar:
            return edgar
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

"""SEC EDGAR announcement-date adapter for PEAD (keyless; User-Agent + <=10 req/s).

The PEAD backtest needs, per historical earnings event, the real ANNOUNCEMENT
date + session (bmo/amc). Finnhub's ``/calendar/earnings`` carries that, but its
free tier only answers FUTURE windows — every historical query returns an empty
``earningsCalendar``. So historical announcement dates come from here instead.

An 8-K carrying **Item 2.02** ("Results of Operations and Financial Condition")
is the quarterly earnings-release filing; its EDGAR ``acceptanceDateTime`` is the
public-announcement timestamp. We read the free ``data.sec.gov/submissions`` JSON
(``filings.recent`` parallel arrays + every ``filings.files[]`` history shard —
a chatty mega-cap keeps only ~1yr in ``recent`` so the shards are mandatory to
cover a multi-year backtest), filter ``form == "8-K"`` and ``"2.02" in items``,
and map:

- ``acceptanceDateTime`` (parsed as UTC, the trailing ``Z`` is literal) -> ``report_ts``
- that timestamp converted to America/New_York -> session:
  ``>= 16:00 ET`` -> ``amc`` (after-market), ``< 09:30 ET`` -> ``bmo`` (before-market),
  otherwise ``""`` (intraday / unknown).

The surprise (actual vs estimate) is NOT here — it is joined on in
``earnings.surprise_history`` from Finnhub's ``/stock/earnings`` per-symbol
history, keyed by fiscal (year, quarter). The ``(year, quarter)`` we emit is a
PROVISIONAL calendar key (the calendar quarter the announcement's *most recent
preceding* quarter-end falls in) — the actual join is by nearest-preceding
period-end so offset fiscal years (NVDA Jan-end, AAPL Sep-end) still line up.

Reuses the CIK map (``universe.sec_ticker_map``) and HTTP helper the insider
adapter uses. Fail-soft: any failure / missing CIK -> ``[]`` (the fallback just
stays dark and PEAD runs without that ticker).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .._http import get_json
from . import universe

log = logging.getLogger(__name__)

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
SHARD_URL = "https://data.sec.gov/submissions/{name}"
_ET = ZoneInfo("America/New_York")

# EDGAR asks <= 10 req/s. Each ticker is 1 recent call + a few history shards,
# so a gentle floor keeps a full-universe backfill polite without being slow.
_MIN_INTERVAL_S = 0.15          # ~6-7 req/s
_last_call = 0.0                # monotonic ts of the last EDGAR call (module-wide)

# amc/bmo cut-offs in America/New_York.
_AMC_HOUR = 16                  # >= 16:00 ET -> after-market
_BMO_HOUR, _BMO_MIN = 9, 30     # < 09:30 ET -> before-market


def _pace() -> None:
    """Monotonic-clock throttle so consecutive EDGAR calls stay polite (<=10 req/s)."""
    global _last_call
    if _MIN_INTERVAL_S <= 0:
        return
    wait = _last_call + _MIN_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _acceptance_to_ts(accept: str | None) -> int | None:
    """Parse an ``acceptanceDateTime`` (UTC, e.g. ``2024-02-01T21:30:30.000Z``) -> epoch ms.

    The trailing ``Z`` is a literal UTC marker (verified: JPM's known 06:45 ET bmo
    release accepts at 10:45Z, i.e. UTC not ET). None if unparseable."""
    if not accept:
        return None
    s = accept.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _session_from_ts(report_ts: int) -> str:
    """bmo/amc/'' from an announcement epoch-ms, via America/New_York wall clock.

    Convert UTC->ET (handles EST/EDT DST automatically), then: hour >= 16:00 -> amc,
    before 09:30 -> bmo, otherwise '' (intraday). A fixed -5 offset would mislabel
    ~8 months of the year, so the tz conversion is load-bearing."""
    et = datetime.fromtimestamp(report_ts / 1000, tz=timezone.utc).astimezone(_ET)
    if et.hour >= _AMC_HOUR:
        return "amc"
    if et.hour < _BMO_HOUR or (et.hour == _BMO_HOUR and et.minute < _BMO_MIN):
        return "bmo"
    return ""


def _trading_date_ms(acceptance_ts: int) -> int:
    """Midnight UTC of the ET *trading date* of an intraday acceptance instant.

    Daily bars are keyed at midnight UTC of the trading day and the PEAD reaction
    lookup (``stock_scoring._earnings_reaction``) finds the first bar with
    ``ts >= report_ts`` — so report_ts MUST share the bars' date granularity, or an
    intraday acceptance (e.g. 21:30Z) skips to the next day and an ``amc`` release
    double-shifts past the actual reaction session (silently rejecting the setup).
    Floor by the *ET* date, not the UTC date: a 21:30Z amc release is 16:30 ET on
    the SAME ET session, and this matches the Finnhub-calendar path's midnight-of-
    ``date`` convention so both feeds drive the reaction identically."""
    et = datetime.fromtimestamp(acceptance_ts / 1000, tz=timezone.utc).astimezone(_ET)
    return int(datetime(et.year, et.month, et.day, tzinfo=timezone.utc).timestamp() * 1000)


def _preceding_quarter(dt: datetime) -> tuple[int, int]:
    """(year, quarter) of the calendar quarter-end most recently PRECEDING ``dt``.

    An earnings 8-K lands weeks after the period it reports, so the announcement's
    nearest preceding quarter-end is a stable provisional fiscal key. E.g. a
    2024-02-01 announcement -> 2023 Q4; 2024-05-02 -> 2024 Q1."""
    # Quarter index 0..3 for the quarter the announcement date itself is in.
    q = (dt.month - 1) // 3          # 0-based current quarter
    # Step back one quarter (the most recent COMPLETED one before the announcement).
    if q == 0:
        return (dt.year - 1, 4)
    return (dt.year, q)


def _is_earnings_8k(form: str, items: str) -> bool:
    """True for an 8-K whose items[] string contains Item 2.02 (earnings release).

    ``items`` is a comma-joined string per filing (e.g. ``"2.02,9.01"``); we match
    the 2.02 token exactly so a spurious substring (there is none in the 8-K
    taxonomy, but be strict) can't false-positive."""
    if (form or "").strip().upper() != "8-K":
        return False
    tokens = {t.strip() for t in (items or "").split(",")}
    return "2.02" in tokens


def _rows_from_arrays(filings: dict) -> list[dict]:
    """Extract earnings-8-K rows from one parallel-array block (recent[] or a shard).

    ``filings`` has parallel lists ``form[] filingDate[] acceptanceDateTime[]
    items[]``; index-align them, keep the Item-2.02 8-Ks, drop rows we can't
    timestamp."""
    forms = filings.get("form") or []
    accepts = filings.get("acceptanceDateTime") or []
    items = filings.get("items") or []
    filed = filings.get("filingDate") or []
    n = len(forms)
    out: list[dict] = []
    for i in range(n):
        form = forms[i] if i < len(forms) else ""
        item = items[i] if i < len(items) else ""
        if not _is_earnings_8k(form, item):
            continue
        accept_ts = _acceptance_to_ts(accepts[i] if i < len(accepts) else None)
        if accept_ts is not None:
            # report_ts is floored to the ET trading date (bars' granularity); the
            # intraday acceptance instant is kept only to resolve the bmo/amc session.
            report_ts = _trading_date_ms(accept_ts)
            hour = _session_from_ts(accept_ts)
        else:
            # No acceptance time: the filing DATE is already the trading date
            # (midnight UTC); the session can't be resolved (-> '').
            fd = filed[i] if i < len(filed) else None
            report_ts = _acceptance_to_ts(f"{fd}T00:00:00Z") if fd else None
            hour = ""
        if report_ts is None:
            continue
        out.append({"report_ts": report_ts, "hour": hour})
    return out


def _shard_names(filings_files) -> list[str]:
    """History-shard filenames from ``filings.files[]`` (older 8-Ks paginate here)."""
    names: list[str] = []
    for f in filings_files or []:
        name = (f or {}).get("name")
        if name:
            names.append(name)
    return names


def _resolve_cik(ticker: str, user_agent: str) -> str | None:
    """Ticker -> zero-padded 10-digit CIK via SEC's company_tickers map. None if absent."""
    smap = universe.sec_ticker_map(user_agent)
    meta = smap.get((ticker or "").upper()) or {}
    return meta.get("cik")


def announcement_dates(ticker: str, user_agent: str | None = None,
                       cik: str | None = None) -> list[dict]:
    """Historical earnings-announcement dates for ``ticker`` from EDGAR 8-K/Item 2.02.

    Returns ``[{"report_ts": epoch_ms(UTC), "hour": "bmo"|"amc"|"",
    "year": int, "quarter": int}, ...]`` NEWEST-FIRST, de-duped to one row per
    fiscal (year, quarter) — earnings 8-Ks are 1/quarter for clean filers; when a
    quarter has extras (preliminary + corrective, or a 2.02 co-filed with 9.01) we
    keep the earliest announcement of that quarter (the moment the market learned).

    ``cik`` may be passed pre-resolved (the collector already stores it on
    ``stock_universe``); otherwise it is looked up from the SEC ticker map. Fail-
    soft: [] on any failure, missing CIK, or a foreign private issuer that files
    6-K (no items[] taxonomy) rather than 8-K."""
    # Resolve the User-Agent: SEC 403s without a descriptive one.
    if not user_agent:
        try:
            from ...config import load_config
            user_agent = load_config().sec_user_agent
        except Exception:  # noqa: BLE001 - never let config break a source read
            user_agent = "btc-signal research contact@example.com"
    cik10 = cik or _resolve_cik(ticker, user_agent)
    if not cik10:
        return []
    cik10 = f"{int(cik10):010d}"   # tolerate an unpadded CIK from the universe table
    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}

    _pace()
    data = get_json(SUBMISSIONS_URL.format(cik10=cik10), headers=headers)
    if not isinstance(data, dict):
        return []
    filings = data.get("filings") or {}
    rows: list[dict] = []
    recent = filings.get("recent")
    if isinstance(recent, dict):
        rows.extend(_rows_from_arrays(recent))
    # Merge every history shard: a high-volume filer's earnings 8-Ks have already
    # rolled out of recent[] into these, so skipping them silently loses quarters.
    for name in _shard_names(filings.get("files")):
        _pace()
        shard = get_json(SHARD_URL.format(name=name), headers=headers)
        if isinstance(shard, dict):
            rows.extend(_rows_from_arrays(shard))

    if not rows:
        return []
    # Attach the provisional (year, quarter) key and de-dup to one per quarter,
    # keeping the EARLIEST announcement of each fiscal quarter.
    best: dict[tuple[int, int], dict] = {}
    for r in rows:
        dt = datetime.fromtimestamp(r["report_ts"] / 1000, tz=timezone.utc)
        year, quarter = _preceding_quarter(dt)
        r = {**r, "year": year, "quarter": quarter}
        key = (year, quarter)
        cur = best.get(key)
        if cur is None or r["report_ts"] < cur["report_ts"]:
            best[key] = r
    return sorted(best.values(), key=lambda r: r["report_ts"], reverse=True)

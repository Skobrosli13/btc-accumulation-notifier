"""Daily OHLCV adapter — keyless-first with keyed upgrades.

Truly keyless free daily data is scarce in 2026 (Stooq now gates its CSV behind a
JS proof-of-work wall; that adapter fails soft when the wall is up). The keyless
default is therefore the **Yahoo chart endpoint** — free, split-adjusted, but
*fragile* (undocumented, occasionally rate-limits/blocks), so it is best-effort,
never load-bearing. The robust path is a free **Alpaca** key (IEX feed, 200
req/min) or **Tiingo** key (fine for a fixed ≤500-symbol universe).
Venue order: alpaca → tiingo → yahoo → massive → stooq.

Every function returns bars OLDEST->NEWEST as ``[(ts_ms, open, high, low, close,
volume), ...]`` (``ts_ms`` = UTC-midnight epoch ms of the bar date) or ``None`` on
failure. ``daily_bars`` also returns which venue served the bars; translate that
venue through ``VENUE_BASIS`` to know the adjustment basis of the series.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .._http import get_json, get_text

log = logging.getLogger(__name__)

STOOQ_URL = "https://stooq.com/q/d/l/"
ALPACA_BARS = "https://data.alpaca.markets/v2/stocks/{sym}/bars"
TIINGO_EOD = "https://api.tiingo.com/tiingo/daily/{sym}/prices"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
# Browser-ish UA so Yahoo/Stooq don't reject the default requests UA outright.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

Bar = tuple  # (ts_ms, o, h, l, c, v)

# Adjustment basis per venue. Everything the chain serves is SPLIT-ONLY ("split")
# except Stooq, whose CSV is total-return ("split_div": splits + dividends) and
# carries no columns to unwind it — tolerable for the last-resort venue only.
# Features must never be compared across bases; callers translate the venue name
# ``daily_bars`` returns through this map.
VENUE_BASIS = {"alpaca": "split", "tiingo": "split", "yahoo": "split",
               "massive": "split", "stooq": "split_div"}

# Calendar days requested per trading bar wanted (weekends/holidays ≈ 1.45x, with
# slack so a ``limit``-bar request always spans enough wall-clock history).
_CAL_DAYS_PER_BAR = 1.6
# A venue answering with fewer bars than this against a >=60-bar request is
# treated as failed (the "latest bar only" degenerate response) so the fallback
# chain isn't masked by a truthy 1-element list.
_MIN_BARS_FULL = 60

_PAGE_CAP = 10  # hard cap on Alpaca pagination loops


def _ts_to_midnight_ms(epoch_s: int) -> int:
    d = datetime.fromtimestamp(int(epoch_s), tz=timezone.utc)
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _date_to_ms(datestr: str) -> int | None:
    try:
        d = datetime.strptime(datestr[:10], "%Y-%m-%d")
        return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _start_date_for(limit: int) -> str:
    """YYYY-MM-DD far enough back that ``limit`` trading bars fit in the window."""
    start = datetime.now(timezone.utc) - timedelta(days=int(limit * _CAL_DAYS_PER_BAR) + 5)
    return start.strftime("%Y-%m-%d")


def _yahoo_symbol(ticker: str) -> str:
    # Yahoo uses a dash for class shares (BRK.B -> BRK-B).
    return ticker.replace(".", "-")


_YAHOO_RANGES = ((366, "1y"), (732, "2y"), (1830, "5y"), (3660, "10y"))


def _yahoo_range_for(limit: int) -> str:
    """Smallest Yahoo chart ``range`` token that covers ``limit`` trading bars."""
    need = int(limit * _CAL_DAYS_PER_BAR)
    for days, rng in _YAHOO_RANGES:
        if need <= days:
            return rng
    return "max"


def yahoo_daily(ticker: str, limit: int = 400, rng: str | None = None) -> list[Bar] | None:
    """Keyless (fragile) split-adjusted daily bars from Yahoo's chart endpoint.

    The chart quote arrays are already split-adjusted (but NOT dividend-adjusted),
    which is exactly the split-only basis the other venues serve — the adjclose
    (total-return) ratio is deliberately not applied, so prices stay on the same
    basis as the raw volume. ``rng`` derives from ``limit`` when omitted. None on
    any failure — treat as best-effort, not a dependency."""
    data = get_json(YAHOO_CHART.format(sym=_yahoo_symbol(ticker)),
                    params={"range": rng or _yahoo_range_for(limit), "interval": "1d",
                            "events": "split"},
                    headers={"User-Agent": _UA})
    try:
        res = (data or {}).get("chart", {}).get("result")
        r0 = res[0]
        ts = r0["timestamp"]
        q = r0["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        return None
    o_, h_, l_, c_, v_ = q.get("open"), q.get("high"), q.get("low"), q.get("close"), q.get("volume")
    out: list[Bar] = []
    for i in range(len(ts)):
        try:
            c = c_[i]
            if c is None or o_[i] is None or h_[i] is None or l_[i] is None:
                continue
            out.append((_ts_to_midnight_ms(ts[i]), o_[i], h_[i], l_[i], c, float(v_[i] or 0)))
        except (TypeError, IndexError):
            continue
    if not out:
        return None
    out.sort(key=lambda r: r[0])
    return out[-limit:]


def _stooq_symbol(ticker: str) -> str:
    # Stooq uses lowercase + '.us' suffix; dotted class shares use a dash.
    return ticker.lower().replace(".", "-") + ".us"


def stooq_daily(ticker: str, limit: int = 400) -> list[Bar] | None:
    """Free keyless daily bars from Stooq (last-resort; often behind a JS PoW wall,
    in which case the body isn't CSV and this returns None). None on failure/empty.

    NOTE: Stooq's series is total-return (split+dividend adjusted) — a different
    basis from the split-only venues (see ``VENUE_BASIS``)."""
    txt = get_text(STOOQ_URL, params={"s": _stooq_symbol(ticker), "i": "d"},
                   headers={"User-Agent": _UA})
    if not txt or "," not in txt:
        return None
    lines = txt.strip().splitlines()
    if len(lines) < 2 or not lines[0].lower().startswith("date"):
        return None
    out: list[Bar] = []
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 6:
            continue
        ts = _date_to_ms(parts[0])
        if ts is None:
            continue
        try:
            o, h, l, c = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
            v = float(parts[5]) if parts[5] not in ("", "N/D") else 0.0
        except ValueError:
            continue
        out.append((ts, o, h, l, c, v))
    if not out:
        return None
    out.sort(key=lambda r: r[0])
    return out[-limit:]


def alpaca_daily(ticker: str, key: str, secret: str, limit: int = 400) -> list[Bar] | None:
    """Alpaca daily bars (feed=iex to stay strictly within the free tier).

    Requests an explicit ``start`` — Alpaca defaults ``start`` to the beginning of
    the CURRENT day, which silently returns a single bar regardless of ``limit`` —
    and follows ``next_page_token`` pagination. None on failure."""
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    start = _start_date_for(limit)
    out: list[Bar] = []
    page_token: str | None = None
    for _ in range(_PAGE_CAP):
        params = {"timeframe": "1Day", "limit": min(limit, 10_000), "feed": "iex",
                  "adjustment": "split", "start": start}
        if page_token:
            params["page_token"] = page_token
        data = get_json(ALPACA_BARS.format(sym=ticker), params=params, headers=headers)
        if data is None:
            break
        for b in data.get("bars") or []:
            ts = _date_to_ms(b.get("t", ""))
            if ts is None:
                continue
            try:
                out.append((ts, float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]),
                            float(b.get("v", 0))))
            except (KeyError, TypeError, ValueError):
                continue
        page_token = data.get("next_page_token")
        if not page_token:
            break
    if not out:
        return None
    out.sort(key=lambda r: r[0])
    return out[-limit:]


def tiingo_daily(ticker: str, token: str, limit: int = 400) -> list[Bar] | None:
    """Split-adjusted daily bars from Tiingo.

    Requests an explicit ``startDate`` — without one Tiingo returns ONLY the latest
    record — and rebuilds a SPLIT-ONLY series from the raw columns + ``splitFactor``:
    Tiingo's adj* columns are total-return (dividends folded in), which would put
    this venue on a different basis from Alpaca/Yahoo/Massive. None on failure."""
    data = get_json(
        TIINGO_EOD.format(sym=ticker),
        params={"token": token, "format": "json", "resampleFreq": "daily",
                "startDate": _start_date_for(limit)},
    )
    if not isinstance(data, list) or not data:
        return None
    rows: list[tuple] = []
    for b in data:
        ts = _date_to_ms(b.get("date", ""))
        if ts is None:
            continue
        try:
            rows.append((ts, float(b["open"]), float(b["high"]), float(b["low"]),
                         float(b["close"]), float(b.get("volume", 0) or 0),
                         float(b.get("splitFactor", 1.0) or 1.0)))
        except (KeyError, TypeError, ValueError):
            continue
    if not rows:
        return None
    rows.sort(key=lambda r: r[0])
    # Split-only adjustment: a bar's splitFactor takes effect ON that date, so all
    # EARLIER bars divide prices (and multiply volume) by it. Walk newest->oldest
    # accumulating the factor.
    out_rev: list[Bar] = []
    factor = 1.0
    for ts, o, h, l, c, v, sf in reversed(rows):
        out_rev.append((ts, o / factor, h / factor, l / factor, c / factor, v * factor))
        if sf and sf != 1.0:
            factor *= sf
    out = list(reversed(out_rev))
    return out[-limit:]


def _massive_daily(ticker: str, key: str | None, limit: int = 400) -> list[Bar] | None:
    """Massive per-ticker daily aggregates as a fallback (UTC-midnight ts). None on
    failure. Polygon-shaped ``adjusted=true`` is split-only — same basis as the rest."""
    from . import massive
    end = datetime.now(timezone.utc)
    bars = massive.daily_bars(ticker, key, _start_date_for(limit), end.strftime("%Y-%m-%d"))
    if not bars:
        return None
    out = [(_ts_to_midnight_ms(int(t / 1000)), o, h, l, c, v) for (t, o, h, l, c, v) in bars]
    return out[-limit:]


def daily_bars(ticker: str, cfg, limit: int = 400,
               venue: str | None = None) -> tuple[list[Bar], str] | None:
    """Best available daily bars for one ticker + the venue that served them.

    Venue order is fixed by key presence: alpaca → tiingo → yahoo → massive →
    stooq (keyed venues only when configured; ``cfg.stock_price_source`` is not
    consulted). ``venue`` pins the fetch to that single venue — used to reprice a
    position on the venue that priced its entry, so an adjustment-basis or rebase
    seam between venues can't silently shift stored levels. A venue answering a
    >=60-bar request with fewer than 60 bars is treated as failed (degenerate
    "latest bar only" response) so the chain falls through instead of masking it.
    Returns ``(bars, source)`` or ``None`` if every attempted venue failed for
    this symbol (the caller just skips the name)."""
    attempts: list[tuple[str, callable]] = []
    if cfg.alpaca_active:
        attempts.append(("alpaca", lambda: alpaca_daily(ticker, cfg.alpaca_api_key,
                                                         cfg.alpaca_secret_key, limit)))
    if cfg.tiingo_api_key:
        attempts.append(("tiingo", lambda: tiingo_daily(ticker, cfg.tiingo_api_key, limit)))
    attempts.append(("yahoo", lambda: yahoo_daily(ticker, limit)))
    # Massive per-ticker as a keyed FALLBACK (5/min free limit -> only for the handful
    # of names Yahoo drops, never the bulk feed).
    if getattr(cfg, "massive_active", False):
        attempts.append(("massive", lambda: _massive_daily(ticker, cfg.massive_api_key, limit)))
    attempts.append(("stooq", lambda: stooq_daily(ticker, limit)))
    if venue is not None:
        attempts = [(s, fn) for s, fn in attempts if s == venue]
    min_bars = _MIN_BARS_FULL if limit >= _MIN_BARS_FULL else 1
    for source, fn in attempts:
        try:
            bars = fn()
        except Exception as exc:  # noqa: BLE001 - fail-soft
            log.warning("%s daily_bars(%s) failed: %s", source, ticker, exc)
            bars = None
        if bars and len(bars) >= min_bars:
            return bars, source
        if bars:
            log.warning("%s daily_bars(%s) returned only %d bars (<%d); trying next venue",
                        source, ticker, len(bars), min_bars)
    return None

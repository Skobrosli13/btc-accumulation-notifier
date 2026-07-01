"""Daily OHLCV adapter — keyless-first with keyed upgrades.

Truly keyless free daily data is scarce in 2026 (Stooq now gates its CSV behind a
JS proof-of-work wall; that adapter fails soft when the wall is up). The keyless
default is therefore the **Yahoo chart endpoint** — free, split-adjusted (via the
adjclose ratio), but *fragile* (undocumented, occasionally rate-limits/blocks), so
it is best-effort, never load-bearing. The robust path is a free **Alpaca** key
(IEX feed, 200 req/min) or **Tiingo** key (fine for a fixed ≤500-symbol universe).
Venue order: alpaca → tiingo → yahoo → stooq.

Every function returns bars OLDEST->NEWEST as ``[(ts_ms, open, high, low, close,
volume), ...]`` (``ts_ms`` = UTC-midnight epoch ms of the bar date) or ``None`` on
failure. ``daily_bars`` also returns which venue served the bars.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

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


def _ts_to_midnight_ms(epoch_s: int) -> int:
    d = datetime.fromtimestamp(int(epoch_s), tz=timezone.utc)
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _date_to_ms(datestr: str) -> int | None:
    try:
        d = datetime.strptime(datestr[:10], "%Y-%m-%d")
        return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _yahoo_symbol(ticker: str) -> str:
    # Yahoo uses a dash for class shares (BRK.B -> BRK-B).
    return ticker.replace(".", "-")


def yahoo_daily(ticker: str, limit: int = 400, rng: str = "2y") -> list[Bar] | None:
    """Keyless (fragile) split-adjusted daily bars from Yahoo's chart endpoint.

    OHLC are scaled by the adjclose/close ratio so splits are handled; volume is
    left raw (used only for the liquidity filter). None on any failure — treat as
    best-effort, not a dependency."""
    data = get_json(YAHOO_CHART.format(sym=_yahoo_symbol(ticker)),
                    params={"range": rng, "interval": "1d",
                            "events": "split", "includeAdjustedClose": "true"},
                    headers={"User-Agent": _UA})
    try:
        res = (data or {}).get("chart", {}).get("result")
        r0 = res[0]
        ts = r0["timestamp"]
        q = r0["indicators"]["quote"][0]
        adj = (r0["indicators"].get("adjclose") or [{}])[0].get("adjclose")
    except (KeyError, IndexError, TypeError):
        return None
    o_, h_, l_, c_, v_ = q.get("open"), q.get("high"), q.get("low"), q.get("close"), q.get("volume")
    out: list[Bar] = []
    for i in range(len(ts)):
        try:
            c = c_[i]
            if c is None or o_[i] is None or h_[i] is None or l_[i] is None:
                continue
            ratio = (adj[i] / c) if (adj and adj[i] is not None and c) else 1.0
            out.append((_ts_to_midnight_ms(ts[i]), o_[i] * ratio, h_[i] * ratio,
                        l_[i] * ratio, c * ratio, float(v_[i] or 0)))
        except (TypeError, IndexError, ZeroDivisionError):
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
    in which case the body isn't CSV and this returns None). None on failure/empty."""
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
    """Alpaca daily bars (historical SIP is free for EOD; feed=iex is always free).
    Uses feed=iex to stay strictly within the free tier. None on failure."""
    data = get_json(
        ALPACA_BARS.format(sym=ticker),
        params={"timeframe": "1Day", "limit": min(limit, 1000), "feed": "iex", "adjustment": "split"},
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
    )
    bars = (data or {}).get("bars")
    if not bars:
        return None
    out: list[Bar] = []
    for b in bars:
        ts = _date_to_ms(b.get("t", ""))
        if ts is None:
            continue
        try:
            out.append((ts, float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]),
                        float(b.get("v", 0))))
        except (KeyError, TypeError, ValueError):
            continue
    if not out:
        return None
    out.sort(key=lambda r: r[0])
    return out[-limit:]


def tiingo_daily(ticker: str, token: str, limit: int = 400) -> list[Bar] | None:
    data = get_json(
        TIINGO_EOD.format(sym=ticker),
        params={"token": token, "format": "json", "resampleFreq": "daily"},
    )
    if not isinstance(data, list) or not data:
        return None
    out: list[Bar] = []
    for b in data:
        ts = _date_to_ms(b.get("date", ""))
        if ts is None:
            continue
        try:
            # adjusted fields keep splits/divs consistent with Stooq's adjusted series
            out.append((ts, float(b["adjOpen"]), float(b["adjHigh"]), float(b["adjLow"]),
                        float(b["adjClose"]), float(b.get("adjVolume", 0))))
        except (KeyError, TypeError, ValueError):
            continue
    if not out:
        return None
    out.sort(key=lambda r: r[0])
    return out[-limit:]


def _massive_daily(ticker: str, key: str | None, limit: int = 400) -> list[Bar] | None:
    """Massive per-ticker daily aggregates as a fallback (UTC-midnight ts). None on failure."""
    from datetime import timedelta
    from . import massive
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(limit * 1.5) + 5)
    bars = massive.daily_bars(ticker, key, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    if not bars:
        return None
    out = [(_ts_to_midnight_ms(int(t / 1000)), o, h, l, c, v) for (t, o, h, l, c, v) in bars]
    return out[-limit:]


def daily_bars(ticker: str, cfg, limit: int = 400) -> tuple[list[Bar], str] | None:
    """Best available daily bars for one ticker + the venue that served them.

    Order follows ``cfg.stock_price_source`` (alpaca → tiingo → stooq), always
    falling back to keyless Stooq. Returns ``(bars, source)`` or ``None`` if every
    venue failed for this symbol (the caller just skips the name)."""
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
    for source, fn in attempts:
        try:
            bars = fn()
        except Exception as exc:  # noqa: BLE001 - fail-soft
            log.warning("%s daily_bars(%s) failed: %s", source, ticker, exc)
            bars = None
        if bars:
            return bars, source
    return None

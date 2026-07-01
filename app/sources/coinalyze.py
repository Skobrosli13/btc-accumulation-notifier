"""Coinalyze adapter — free derivatives order-flow (SINGLE-VENUE by default).

Coinalyze *can* aggregate open interest, funding, liquidations and candle-CVD
across futures venues, but the default ``COINALYZE_SYMBOL`` (``BTCUSDT_PERP.A``)
is ONE venue: the **Binance** USDT perp. Every CVD/OI/liquidation number this
module returns is therefore that single market's, not market-wide — the draw is
that Binance is HTTP-451 from US/AWS, so this is our only window onto the
deepest perp venue. (Switching to a Coinalyze aggregate symbol code would
genuinely aggregate, but also invalidates the committed single-venue order-flow
backtest framing.) Free API key, 40 req/min, auth via the ``api_key`` header.
Activated by ``COINALYZE_API_KEY`` presence; every function degrades to
``None``/``[]`` so the collector falls back to the OKX path when this is dark.

Endpoint contract (verified against api.coinalyze.net/v1/doc + the reference
client). Base ``https://api.coinalyze.net/v1`` ::

    GET /open-interest?symbols=            -> [{"symbol","value","update"}]
    GET /funding-rate?symbols=             -> [{"symbol","value","update"}]
    GET /ohlcv-history?symbols&interval&from&to
        -> [{"symbol","history":[{t,o,h,l,c,v,bv,tx,btx}]}]   (bv = taker BUY volume)
    GET /open-interest-history?...         -> [{"symbol","history":[{t,o,h,l,c}]}]
    GET /liquidation-history?...&convert_to_usd=true
        -> [{"symbol","history":[{t,l,s}]}]                   (l/s = long/short liquidated)

History ``t`` is epoch **seconds**; ``from``/``to`` are epoch **seconds** too.
CVD is not a first-class endpoint — we derive per-bar delta from the OHLCV taker
split: ``delta = 2*bv - v`` (sell volume = v - bv).
"""
from __future__ import annotations

import logging
import time

from ._http import get_json

log = logging.getLogger(__name__)

BASE = "https://api.coinalyze.net/v1"

# Short-term timeframe -> Coinalyze interval code, and that interval's length in
# hours (used to size the history lookback window).
INTERVAL_MAP = {"15m": "15min", "30m": "30min", "1h": "1hour", "2h": "2hour",
                "4h": "4hour", "6h": "6hour", "12h": "12hour", "1d": "daily"}
INTERVAL_HOURS = {"15min": 0.25, "30min": 0.5, "1hour": 1, "2hour": 2, "4hour": 4,
                  "6hour": 6, "12hour": 12, "daily": 24}


def _hdr(api_key: str) -> dict:
    return {"api_key": api_key}


def _current_value(data) -> float | None:
    """Pull ``value`` from a current-OI / current-funding response (list of one
    row per symbol). Single-symbol callers take the first row."""
    if not isinstance(data, list) or not data:
        return None
    try:
        return float(data[0]["value"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _history_rows(path: str, symbol: str, interval: str, hours: float,
                  api_key: str, convert_to_usd: bool = False) -> list[dict]:
    """Raw ``history`` array for a single symbol, oldest-first. [] on any failure."""
    to = int(time.time())
    frm = to - int(hours * 3600)
    params = {"symbols": symbol, "interval": interval, "from": frm, "to": to}
    if convert_to_usd:
        params["convert_to_usd"] = "true"
    data = get_json(f"{BASE}/{path}", params=params, headers=_hdr(api_key))
    if not isinstance(data, list) or not data:
        return []
    hist = data[0].get("history") if isinstance(data[0], dict) else None
    if not isinstance(hist, list):
        return []
    # Coinalyze returns oldest->newest already, but don't trust ordering.
    return sorted((r for r in hist if isinstance(r, dict)),
                  key=lambda r: r.get("t", 0))


# --- Current values (drop-in alternatives to the OKX funding/OI) -------------

def open_interest(symbol: str, api_key: str) -> float | None:
    """Current open interest for ``symbol`` (contracts/coin units; the default
    symbol is the single-venue Binance perp, not a cross-venue aggregate)."""
    return _current_value(get_json(f"{BASE}/open-interest",
                                   params={"symbols": symbol}, headers=_hdr(api_key)))


def funding_latest(symbol: str, api_key: str) -> float | None:
    """Current funding rate fraction for ``symbol`` (Binance perp = per-8h)."""
    return _current_value(get_json(f"{BASE}/funding-rate",
                                   params={"symbols": symbol}, headers=_hdr(api_key)))


# --- History series (feed app/flow.py) ---------------------------------------

def ohlcv_history(symbol: str, interval: str, hours: float, api_key: str) -> list[dict]:
    """OHLCV bars with the taker buy-volume split, oldest-first.

    Each row: {ts(ms), open, high, low, close, volume, buyvol}. ``buyvol`` is the
    taker BUY volume (``bv``) used to derive CVD downstream. [] on failure.
    """
    out: list[dict] = []
    for r in _history_rows("ohlcv-history", symbol, interval, hours, api_key):
        try:
            out.append({
                "ts": int(r["t"]) * 1000,
                "open": float(r["o"]), "high": float(r["h"]),
                "low": float(r["l"]), "close": float(r["c"]),
                "volume": float(r["v"]), "buyvol": float(r["bv"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def oi_history(symbol: str, interval: str, hours: float, api_key: str) -> list[dict]:
    """Open-interest OHLC history -> [{ts(ms), oi(close)}], oldest-first. [] on failure."""
    out: list[dict] = []
    for r in _history_rows("open-interest-history", symbol, interval, hours, api_key):
        try:
            out.append({"ts": int(r["t"]) * 1000, "oi": float(r["c"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def liquidations_history(symbol: str, interval: str, hours: float,
                         api_key: str) -> list[dict]:
    """Liquidation history in USD -> [{ts(ms), long, short}], oldest-first.

    ``long``/``short`` are the USD notional of LONG / SHORT positions liquidated
    in that bar (``convert_to_usd=true``). [] on failure.
    """
    out: list[dict] = []
    for r in _history_rows("liquidation-history", symbol, interval, hours, api_key,
                           convert_to_usd=True):
        try:
            out.append({"ts": int(r["t"]) * 1000,
                        "long": float(r["l"]), "short": float(r["s"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out

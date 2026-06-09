"""Deep OKX history pagination for the short-term backtests (network).

``app/sources/exchange.klines_history`` uses the ``/market/candles`` endpoint,
which OKX caps at ~1440 candles of lookback (recent data only). The
``/market/history-candles`` endpoint paginates years back via ``after`` (the
oldest ts of the previous page), which is what we need to span multiple
bull/bear regimes for the 4h track. We keep this in scripts/ (not app/) because
it is a backtest-only concern; it returns the SAME normalized frame contract as
``exchange.klines`` (open_time/open/high/low/close/volume/confirmed, oldest-first,
``df.attrs['source']``).

If history-candles is unreachable, callers fall back to ``exchange.klines_history``
so the backtest still runs on whatever recent history is available.
"""
from __future__ import annotations

import pandas as pd

from app.sources import exchange
from app.sources._http import get_json

_OKX_BASE = "https://www.okx.com"
_OKX_BAR = {"4h": "4H", "1d": "1Dutc", "1w": "1Wutc"}


def _okx_history_candles(timeframe: str, total: int, symbol: str) -> pd.DataFrame | None:
    """Paginate /market/history-candles backward via ``after`` to assemble ~total."""
    bar = _OKX_BAR.get(timeframe)
    if bar is None:
        return None
    frames: list[list] = []
    after: str | None = None
    fetched = 0
    max_pages = max(1, total // 100 + 2)
    for _ in range(max_pages):
        params = {"instId": symbol, "bar": bar, "limit": 100}
        if after:
            params["after"] = after
        data = get_json(f"{_OKX_BASE}/api/v5/market/history-candles", params=params)
        if not data or data.get("code") != "0" or not data.get("data"):
            break
        chunk = data["data"]  # newest-first: [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]
        for c in chunk:
            frames.append([int(c[0]), c[1], c[2], c[3], c[4], c[5], c[8] == "1"])
        fetched += len(chunk)
        after = chunk[-1][0]  # oldest ts this page -> next page is older
        if len(chunk) < 100 or fetched >= total:
            break
    if not frames:
        return None
    df = pd.DataFrame(frames, columns=exchange._COLS)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True)
    df["confirmed"] = df["confirmed"].astype(bool)
    df = (df.drop_duplicates("open_time").sort_values("open_time")
          .reset_index(drop=True).tail(total).reset_index(drop=True))
    df.attrs["source"] = "okx"
    return df


def deep_klines(timeframe: str, total: int, symbol: str = "BTC-USDT") -> pd.DataFrame:
    """Deep history via history-candles; falls back to exchange.klines_history."""
    df = _okx_history_candles(timeframe, total, symbol)
    if df is not None and not df.empty:
        return df
    return exchange.klines_history(timeframe, total, symbol)


def daily_regime_series(symbol: str = "BTC-USDT", total: int = 2600) -> pd.Series:
    """Deep daily closes as a Series indexed by open_time (UTC), for the 200DMA
    regime. ``st_validation._regime_at`` slices this at each evaluation time so the
    bull/bear tag has no look-ahead. Needs >=200 daily closes to ever leave 'unknown';
    the default ~7y span covers the full multi-year 4h history (with the 200d lead) so
    early-period events get a real bull/bear tag rather than 'unknown'."""
    df = exchange.closed_only(deep_klines("1d", total, symbol))
    s = df.set_index("open_time")["close"].astype(float)
    s.name = "close"
    return s

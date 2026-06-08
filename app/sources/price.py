"""Price structure from free Binance klines (CoinGecko fallback).

This is the one MANDATORY source: 200-week MA, 200-day MA / Mayer Multiple, and
the recent drawdown all come from here, and the price/200WMA gate is part of the
DEEP_VALUE tier. If both Binance and CoinGecko fail, this raises — the run cannot
produce a meaningful signal without price.
"""
from __future__ import annotations

import logging

import pandas as pd
import requests

log = logging.getLogger(__name__)

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
COINGECKO_MARKET_CHART = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"

_KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume",
               "close_time", "qav", "trades", "tbav", "tqav", "ignore"]


def _klines(interval: str, limit: int = 1000, symbol: str = "BTCUSDT") -> pd.DataFrame:
    r = requests.get(BINANCE_KLINES,
                     params={"symbol": symbol, "interval": interval, "limit": limit},
                     timeout=20)
    r.raise_for_status()
    df = pd.DataFrame(r.json(), columns=_KLINE_COLS)
    df["close"] = df["close"].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df


def _coingecko_daily(days: str = "365") -> pd.DataFrame:
    """Fallback: CoinGecko daily closes -> a DataFrame with a 'close' column.

    The free/demo tier caps the window (days='max' -> 401) and treats
    'interval=daily' as enterprise-only, so we request the largest free window
    (365d) and let CoinGecko auto-pick daily granularity. This is enough for the
    200-day MA / Mayer but NOT for a true 200-week MA — see price_structure.
    """
    r = requests.get(COINGECKO_MARKET_CHART,
                     params={"vs_currency": "usd", "days": days},
                     timeout=20)
    r.raise_for_status()
    prices = r.json().get("prices", [])
    df = pd.DataFrame(prices, columns=["ts", "close"])
    df["open_time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df["close"] = df["close"].astype(float)
    return df[["open_time", "close"]]


def get_frames(symbol: str = "BTCUSDT") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (daily, weekly) close DataFrames. Tries Binance, falls back to CoinGecko.

    Both frames have at least 'open_time' and 'close' columns, oldest first.
    """
    try:
        weekly = _klines("1w", 1000, symbol)
        daily = _klines("1d", 1000, symbol)
        return daily, weekly
    except Exception as exc:  # noqa: BLE001
        log.warning("Binance klines failed (%s); falling back to CoinGecko", exc)
        daily = _coingecko_daily()
        weekly = (
            daily.set_index("open_time")["close"]
            .resample("1W").last()
            .dropna()
            .reset_index()
        )
        return daily, weekly


def price_structure(symbol: str = "BTCUSDT") -> dict:
    """Compute price-structure readings from daily + weekly closes.

    A moving average is reported only when enough history is present; otherwise it
    (and the ratio built on it) is None so the indicator degrades gracefully
    rather than reporting a bogus short-window mean as a "200-week MA". On the
    CoinGecko free-tier fallback (365d) the 200-week MA is therefore unavailable
    and the price category leans on the Mayer Multiple alone.
    """
    daily, weekly = get_frames(symbol)
    price = float(daily["close"].iloc[-1])

    wma200 = float(weekly["close"].tail(200).mean()) if len(weekly) >= 200 else None
    dma200 = float(daily["close"].tail(200).mean()) if len(daily) >= 200 else None

    drop = None
    if len(daily) >= 3:
        drop = float((daily["close"].iloc[-1] / daily["close"].iloc[-3] - 1) * -100)

    return {
        "price": price,
        "wma200": wma200,
        "dma200": dma200,
        "price_to_wma200": (price / wma200) if wma200 else None,
        "mayer_multiple": (price / dma200) if dma200 else None,
        "drop_24_48h_pct": drop,
    }

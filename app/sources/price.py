"""Price structure from free exchange klines (OKX/Kraken via exchange.py),
with CoinGecko as the last-resort price fallback.

This is the one MANDATORY source: 200-week MA, 200-day MA / Mayer Multiple, and
the recent drawdown all come from here, and the price/200WMA gate is part of the
DEEP_VALUE tier. If the exchange adapter AND CoinGecko both fail, this raises —
the run cannot produce a meaningful signal without price.
"""
from __future__ import annotations

import logging

import pandas as pd
import requests

from . import exchange

log = logging.getLogger(__name__)

COINGECKO_MARKET_CHART = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"


def _coingecko_daily(days: str = "365") -> pd.DataFrame:
    """Last-resort price fallback: CoinGecko daily closes.

    The free/demo tier caps the window (days='max' -> 401) and treats
    'interval=daily' as enterprise-only, so we request the largest free window
    (365d). Enough for the 200-day MA / Mayer but NOT a true 200-week MA — see
    price_structure's graceful-None handling.
    """
    r = requests.get(COINGECKO_MARKET_CHART,
                     params={"vs_currency": "usd", "days": days}, timeout=20)
    r.raise_for_status()
    prices = r.json().get("prices", [])
    df = pd.DataFrame(prices, columns=["ts", "close"])
    df["open_time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df["close"] = df["close"].astype(float)
    return df[["open_time", "close"]]


def get_frames(symbol: str = "BTC-USDT", prefer: str = "okx") -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """Return (daily, weekly, source). Tries the exchange adapter, then CoinGecko.

    Both frames have at least 'open_time' and 'close' columns, oldest first.
    ``source`` is "exchange" or "coingecko" for the dashboard health panel.
    """
    try:
        daily = exchange.klines("1d", limit=300, symbol=symbol, prefer=prefer)
        weekly = exchange.klines("1w", limit=300, symbol=symbol, prefer=prefer)
        return daily, weekly, "exchange"
    except Exception as exc:  # noqa: BLE001
        log.warning("exchange klines failed (%s); falling back to CoinGecko", exc)
        daily = _coingecko_daily()
        weekly = (
            daily.set_index("open_time")["close"]
            .resample("1W").last().dropna().reset_index()
        )
        return daily, weekly, "coingecko"


def get_intraday_frames(symbol: str = "BTC-USDT", timeframes=("4h", "1d"),
                        prefer: str = "okx") -> dict[str, pd.DataFrame]:
    """OHLCV frames per short-term timeframe for the collector. Missing TFs are
    omitted (caller degrades gracefully); raises only if NONE could be fetched."""
    out: dict[str, pd.DataFrame] = {}
    for tf in timeframes:
        try:
            out[tf] = exchange.klines(tf, limit=300, symbol=symbol, prefer=prefer)
        except Exception as exc:  # noqa: BLE001
            log.warning("intraday klines(%s) failed: %s", tf, exc)
    if not out:
        raise RuntimeError("no intraday timeframes could be fetched")
    return out


def price_structure(symbol: str = "BTC-USDT", prefer: str = "okx") -> dict:
    """Compute price-structure readings from daily + weekly closes.

    A moving average is reported only when enough history is present; otherwise it
    (and the ratio built on it) is None so the indicator degrades gracefully
    rather than reporting a bogus short-window mean as a "200-week MA". On the
    CoinGecko fallback (365d) the 200-week MA is therefore unavailable and the
    price category leans on the Mayer Multiple alone.
    """
    daily, weekly, source = get_frames(symbol, prefer=prefer)
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
        "source": source,
    }

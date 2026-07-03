"""Pure technical-indicator primitives (ema / rsi / macd / bollinger / atr).

Extracted verbatim from ``shortterm.py`` so the BTC swing engine and the
equities scoring engine share ONE implementation instead of the equities side
reaching back into ``shortterm``. No I/O, no config, no TA-Lib — plain pandas
Series in, pandas Series out, oldest->newest. Behaviour is byte-for-byte what
``shortterm`` used before the move (Phase-0 §0.2, no behaviour change).

Every function returns a full Series aligned to the input; insufficient history
surfaces as NaN at the window seams rather than raising, so callers degrade
gracefully.
"""
from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    out = 100 - 100 / (1 + rs)
    # Edge cases at the window seams:
    out = out.mask((avg_loss == 0) & (avg_gain > 0), 100.0)   # only gains -> 100
    out = out.mask((avg_gain == 0) & (avg_loss > 0), 0.0)     # only losses -> 0
    out = out.mask((avg_gain == 0) & (avg_loss == 0), 50.0)   # flat -> neutral 50
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def bollinger(close: pd.Series, period: int = 20, mult: float = 2.0):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = mid + mult * std
    lower = mid - mult * std
    pctb = (close - lower) / (upper - lower)
    return mid, upper, lower, pctb


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

"""Hand-computed fixtures for the shared indicator primitives (core.indicators).

These pin the exact math so the Phase-0 extraction from shortterm.py is a
no-behaviour-change move, and so the equities side that now shares them can't be
broken silently. Values below are worked out by hand in the comments.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.core import indicators as ind


def test_ema_span1_is_identity():
    # alpha = 2/(span+1) = 1 for span=1 -> ema == the series itself.
    s = pd.Series([1.0, 2.0, 3.0])
    assert list(ind.ema(s, 1)) == [1.0, 2.0, 3.0]


def test_ema_hand_value():
    # span=2 -> alpha=2/3. ema0=0; ema1 = (2/3)*3 + (1/3)*0 = 2.0
    assert ind.ema(pd.Series([0.0, 3.0]), 2).iloc[-1] == pytest.approx(2.0)


def test_ema_constant_series_is_flat():
    assert list(ind.ema(pd.Series([5.0] * 4), 3)) == [5.0, 5.0, 5.0, 5.0]


def test_rsi_all_gains_is_100():
    # strictly increasing -> avg_loss==0, avg_gain>0 -> masked to 100
    assert ind.rsi(pd.Series([1.0, 2, 3, 4, 5]), period=2).iloc[-1] == 100.0


def test_rsi_all_losses_is_0():
    assert ind.rsi(pd.Series([5.0, 4, 3, 2, 1]), period=2).iloc[-1] == 0.0


def test_rsi_flat_is_50():
    # no gains and no losses -> masked to neutral 50
    assert ind.rsi(pd.Series([5.0, 5, 5, 5]), period=2).iloc[-1] == 50.0


def test_macd_constant_series_is_zero():
    # ema_fast == ema_slow on a flat series -> macd line, signal, hist all 0
    line, signal, hist = ind.macd(pd.Series([100.0] * 40))
    assert hist.iloc[-1] == pytest.approx(0.0)
    assert line.iloc[-1] == pytest.approx(0.0)
    assert signal.iloc[-1] == pytest.approx(0.0)


def test_bollinger_hand_values():
    # series [0,4], period=2, mult=2. mean=2, var=((0-2)^2+(4-2)^2)/2=4, std=2.
    # upper=2+4=6, lower=2-4=-2, pctb=(4-(-2))/(6-(-2))=6/8=0.75
    mid, upper, lower, pctb = ind.bollinger(pd.Series([0.0, 4.0]), period=2, mult=2.0)
    assert mid.iloc[-1] == pytest.approx(2.0)
    assert upper.iloc[-1] == pytest.approx(6.0)
    assert lower.iloc[-1] == pytest.approx(-2.0)
    assert pctb.iloc[-1] == pytest.approx(0.75)


def test_atr_period1_equals_true_range():
    # period=1 -> alpha=1 -> atr == TR of the last bar.
    # bar1: TR = max(high-low=3, |12-9|=3, |9-9|=0) = 3
    high = pd.Series([10.0, 12.0])
    low = pd.Series([8.0, 9.0])
    close = pd.Series([9.0, 11.0])
    assert ind.atr(high, low, close, period=1).iloc[-1] == pytest.approx(3.0)


def test_shortterm_reexports_are_the_same_objects():
    # backward-compat: shortterm.<prim> must still resolve to the moved funcs.
    from app import shortterm
    assert shortterm.ema is ind.ema
    assert shortterm.rsi is ind.rsi
    assert shortterm.macd is ind.macd
    assert shortterm.bollinger is ind.bollinger
    assert shortterm.atr is ind.atr

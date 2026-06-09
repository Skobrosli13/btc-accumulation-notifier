"""Short-term indicator + trigger + composite tests (known-value where possible)."""
from __future__ import annotations

import pandas as pd
import pytest

from app import shortterm as st
from tests.factories import make_config


def _df(closes, volumes=None):
    """Build a closed-candle OHLCV frame from a list of closes."""
    n = len(closes)
    volumes = volumes if volumes is not None else [1.0] * n
    times = pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC")
    return pd.DataFrame({
        "open_time": times,
        "open": closes,
        "high": [c * 1.001 for c in closes],
        "low": [c * 0.999 for c in closes],
        "close": [float(c) for c in closes],
        "volume": [float(v) for v in volumes],
        "confirmed": [True] * n,
    })


# --- indicator primitives ----------------------------------------------------

def test_ema_constant_is_constant():
    s = pd.Series([100.0] * 10)
    assert st.ema(s, 9).iloc[-1] == pytest.approx(100.0)


def test_rsi_all_up_is_100_all_down_is_0_flat_is_50():
    up = pd.Series([float(i) for i in range(1, 40)])
    down = pd.Series([float(i) for i in range(40, 1, -1)])
    flat = pd.Series([100.0] * 40)
    assert st.rsi(up).iloc[-1] == pytest.approx(100.0)
    assert st.rsi(down).iloc[-1] == pytest.approx(0.0)
    assert st.rsi(flat).iloc[-1] == pytest.approx(50.0)


def test_macd_flat_is_zero():
    _m, _s, hist = st.macd(pd.Series([100.0] * 40))
    assert hist.iloc[-1] == pytest.approx(0.0)


# --- st_state thresholds (pure) ---------------------------------------------

def test_st_state_thresholds():
    cfg = make_config()
    assert st.st_state(70, cfg) == "STRONG_BUY"
    assert st.st_state(40, cfg) == "BUY"
    assert st.st_state(0, cfg) == "NEUTRAL"
    assert st.st_state(-40, cfg) == "SELL"
    assert st.st_state(-70, cfg) == "STRONG_SELL"


# --- triggers ----------------------------------------------------------------

def _keys(triggers):
    return {t.key for t in triggers}


def test_flat_market_no_triggers_and_neutral():
    cfg = make_config()
    df = _df([100.0] * 40)
    # no triggers on perfectly flat data
    assert st.detect_triggers(df, cfg) == []
    score, _ = st.st_composite(df, cfg)
    assert score == pytest.approx(0.0, abs=1.0)
    assert st.st_state(score, cfg) == "NEUTRAL"


def test_ema_bull_cross_fires_on_uptick():
    cfg = make_config()
    df = _df([100.0] * 30 + [101.0])      # flat then a single up-tick -> fast EMA crosses up
    keys = _keys(st.detect_triggers(df, cfg))
    assert "ema_cross_bull" in keys


def test_ema_bear_cross_fires_on_downtick():
    cfg = make_config()
    df = _df([100.0] * 30 + [99.0])
    keys = _keys(st.detect_triggers(df, cfg))
    assert "ema_cross_bear" in keys


def test_funding_spike_triggers_two_sided():
    cfg = make_config()
    df = _df([100.0] * 40)
    assert "funding_spike_bull" in _keys(st.detect_triggers(df, cfg, funding=-0.001))
    assert "funding_spike_bear" in _keys(st.detect_triggers(df, cfg, funding=+0.001))
    # within band -> no funding trigger
    assert not {"funding_spike_bull", "funding_spike_bear"} & _keys(
        st.detect_triggers(df, cfg, funding=0.0001))


def test_volume_flush_down_is_buy():
    cfg = make_config()
    df = _df([100.0] * 30 + [99.0], volumes=[1.0] * 30 + [5.0])
    trigs = st.detect_triggers(df, cfg)
    keys = _keys(trigs)
    assert "vol_flush_down" in keys
    assert next(t for t in trigs if t.key == "vol_flush_down").direction == "BUY"


def test_composite_signed_direction():
    cfg = make_config()
    # strong sustained uptrend -> positive (BUY-side) score
    up = _df([100 + i for i in range(40)])
    score_up, _ = st.st_composite(up, cfg)
    assert score_up > 0
    # negative funding pushes the score further positive (bullish)
    score_fund, _ = st.st_composite(_df([100.0] * 40), cfg, funding=-0.002)
    assert score_fund > 0


def test_evaluate_shape():
    cfg = make_config()
    out = st.evaluate(_df([100.0] * 30 + [101.0]), cfg, funding=-0.001)
    assert set(out) >= {"ts", "price", "score", "state", "components", "indicators", "triggers"}
    assert out["price"] == pytest.approx(101.0)
    assert isinstance(out["triggers"], list)


def test_current_regime_and_alignment():
    bull = pd.Series([100.0] * 199 + [200.0])   # last >= 200-MA
    bear = pd.Series([200.0] * 199 + [100.0])
    assert st.current_regime(bull) == "bull"
    assert st.current_regime(bear) == "bear"
    assert st.current_regime(pd.Series([1.0] * 50)) == "unknown"   # <200 points
    assert st.regime_aligned("BUY", "bull") is True
    assert st.regime_aligned("BUY", "bear") is False
    assert st.regime_aligned("SELL", "bear") is True
    assert st.regime_aligned("BUY", "unknown") is None

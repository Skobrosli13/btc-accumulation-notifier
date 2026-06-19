"""Order-flow signal tests (pure): CVD math, divergence, participant, flush, triggers."""
from __future__ import annotations

import pandas as pd

from app import flow
from tests.factories import make_config


# --- CVD construction --------------------------------------------------------

def test_build_cvd_delta_and_cumsum():
    rows = [
        {"ts": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1000, "buyvol": 700},
        {"ts": 2, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1000, "buyvol": 300},
        {"ts": 3, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1000, "buyvol": 500},
    ]
    df = flow.build_cvd(rows)
    # delta = 2*bv - v  ->  +400, -400, 0   ;   cvd = cumulative -> 400, 0, 0
    assert list(df["delta"]) == [400.0, -400.0, 0.0]
    assert list(df["cvd"]) == [400.0, 0.0, 0.0]


def test_build_cvd_empty():
    assert flow.build_cvd([]).empty


# --- CVD / price divergence --------------------------------------------------

def _div_df(lows, highs, cvds):
    return pd.DataFrame({"low": lows, "high": highs, "cvd": cvds,
                         "close": highs})  # close unused by divergence


def test_cvd_bullish_divergence():
    # last bar prints a lower low than the window trough (96) but CVD is higher.
    df = _div_df(lows=[100, 98, 96, 97, 99, 95],
                 highs=[101, 99, 97, 98, 100, 96],
                 cvds=[10, 5, 0, 3, 6, 8])
    assert flow.cvd_divergence(df, lookback=14) == "bullish"


def test_cvd_bearish_divergence():
    # rising lows (no lower low -> bullish path skipped); last high tops the peak
    # (104) while CVD is lower than at that peak.
    df = _div_df(lows=[90, 91, 92, 93, 94, 95],
                 highs=[100, 102, 104, 103, 101, 106],
                 cvds=[10, 8, 12, 9, 7, 5])
    assert flow.cvd_divergence(df, lookback=14) == "bearish"


def test_cvd_no_divergence_and_short_history():
    flat = _div_df(lows=[100, 100, 100, 100], highs=[101, 101, 101, 101],
                   cvds=[1, 1, 1, 1])
    assert flow.cvd_divergence(flat, lookback=14) is None
    assert flow.cvd_divergence(_div_df([1, 2], [1, 2], [1, 2]), 14) is None
    assert flow.cvd_divergence(pd.DataFrame(), 14) is None


# --- OI participant quadrants ------------------------------------------------

def test_participant_quadrants():
    assert flow.participant(1.0, 2.0, 10)["state"] == "new_longs"
    assert flow.participant(1.0, -2.0, 10)["state"] == "short_covering"
    assert flow.participant(-1.0, 2.0, 10)["state"] == "new_shorts"
    assert flow.participant(-1.0, -2.0, 10)["state"] == "long_liquidation"


def test_participant_significance_and_none():
    assert flow.participant(1.0, 2.0, 10)["significant"] is False    # |2| < 10
    assert flow.participant(1.0, 12.0, 10)["significant"] is True    # |12| >= 10
    assert flow.participant(None, 1.0, 10) is None


def test_participant_from_series():
    closes = [100.0, 101.0]   # +1%
    ois = [100.0, 115.0]      # +15% -> significant new longs
    p = flow.participant_from_series(closes, ois, 10)
    assert p["state"] == "new_longs" and p["significant"] is True
    assert flow.participant_from_series([100.0], [100.0], 10) is None


# --- Liquidation flush -------------------------------------------------------

def test_liquidation_long_flush():
    rows = [{"long": 100.0, "short": 100.0}] * 4 + [{"long": 1000.0, "short": 50.0}]
    assert flow.liquidation_flush(rows, mult=3.0) == ("long", 1000.0)


def test_liquidation_short_flush():
    rows = [{"long": 100.0, "short": 100.0}] * 4 + [{"long": 50.0, "short": 900.0}]
    assert flow.liquidation_flush(rows, mult=3.0) == ("short", 900.0)


def test_liquidation_no_flush_or_short_history():
    calm = [{"long": 100.0, "short": 100.0}] * 5
    assert flow.liquidation_flush(calm, mult=3.0) is None
    assert flow.liquidation_flush([{"long": 1.0, "short": 1.0}], 3.0) is None


# --- Trigger assembly --------------------------------------------------------

def test_detect_flow_triggers_full_house():
    cfg = make_config()
    cvd_df = _div_df(lows=[100, 98, 96, 97, 99, 95],
                     highs=[101, 99, 97, 98, 100, 96],
                     cvds=[10, 5, 0, 3, 6, 8])                     # -> bullish
    part = flow.participant(1.0, 12.0, cfg.st_oi_surge_pct)        # -> new_longs (significant)
    liq = ("long", 1000.0)
    trigs = {t.key: t.direction for t in
             flow.detect_flow_triggers(cvd_df, part, liq, cfg)}
    assert trigs == {"cvd_bull_divergence": "BUY", "oi_new_longs": "BUY",
                     "liq_long_flush": "BUY"}


def test_detect_flow_triggers_insignificant_participant_is_quiet():
    cfg = make_config()
    part = flow.participant(1.0, 1.0, cfg.st_oi_surge_pct)  # |1| < 10 -> not significant
    assert flow.detect_flow_triggers(pd.DataFrame(), part, None, cfg) == []

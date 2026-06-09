"""Percentile/calibration scoring: interpolation, direction, fallback, look-ahead."""
from __future__ import annotations

import pytest

from app import scoring

# value at each quantile, ascending
_PROBS = [0.0, 0.25, 0.5, 0.75, 1.0]
_BP = [0.0, 1.0, 2.0, 4.0, 8.0]


def test_percentile_score_interpolates_and_clamps():
    f = scoring.percentile_score
    # below/at the bottom breakpoint -> percentile 0
    assert f(-5, _BP, _PROBS, "higher_bullish") == pytest.approx(0.0)
    # at/above the top -> percentile 1
    assert f(99, _BP, _PROBS, "higher_bullish") == pytest.approx(1.0)
    # midpoint of the 2.0->4.0 bucket (probs .5->.75) -> p=.625
    assert f(3.0, _BP, _PROBS, "higher_bullish") == pytest.approx(0.625)


def test_percentile_score_direction_flips():
    f = scoring.percentile_score
    # a low value is bullish for lower_bullish (high score) and bearish for higher
    assert f(0.5, _BP, _PROBS, "lower_bullish") == pytest.approx(1.0 - 0.125)
    assert f(0.5, _BP, _PROBS, "higher_bullish") == pytest.approx(0.125)


def test_percentile_score_degenerate_inputs():
    assert scoring.percentile_score(1.0, [], [], "higher_bullish") is None
    assert scoring.percentile_score(1.0, [1, 2], [0.0], "higher_bullish") is None  # len mismatch


def test_rank_score_no_lookahead():
    # Scoring a day against its expanding [start..t] slice must ignore future data.
    history_to_t = [10, 8, 9, 7]           # values up to and including day t
    future = [100, 200]                     # later days that would change the rank
    v = 9.0
    past_only = scoring.rank_score(history_to_t, v, "higher_bullish")
    with_future = scoring.rank_score(history_to_t + future, v, "higher_bullish")
    # 3/4 of past <= 9 -> 0.75 ; including the future the rank drops to 3/6 = 0.5
    assert past_only == pytest.approx(0.75)
    assert with_future == pytest.approx(0.5)
    assert past_only != with_future        # proves the slice excludes look-ahead


def test_score_indicators_uses_calibration_when_present():
    scoring.set_calibration({
        "probs": _PROBS,
        "indicators": {"mvrv_z": {"direction": "lower_bullish", "breakpoints": _BP}},
    })
    # mvrv_z=3.0 -> percentile .625 -> lower_bullish score .375 (NOT the linear value)
    out = scoring.score_indicators({"mvrv_z": 3.0})
    assert out["mvrv_z"] == pytest.approx(1.0 - 0.625)


def test_score_indicators_falls_back_to_linear_without_calibration():
    scoring.set_calibration({})                      # no calibration -> linear thresholds
    out = scoring.score_indicators({"mvrv_z": 0.0})  # extreme=0.0 -> linear score 1.0
    assert out["mvrv_z"] == pytest.approx(1.0)
    out = scoring.score_indicators({"mvrv_z": 2.0})  # neutral=2.0 -> linear score 0.0
    assert out["mvrv_z"] == pytest.approx(0.0)


def test_direction_map_matches_thresholds():
    assert scoring.DIRECTION["mvrv_z"] == "lower_bullish"     # neutral 2 > extreme 0
    assert scoring.DIRECTION["m2_yoy"] == "higher_bullish"    # neutral 2 < extreme 10
    assert scoring.DIRECTION["hy_spread"] == "higher_bullish"
    assert scoring.DIRECTION["funding"] == "lower_bullish"

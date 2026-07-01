"""Percentile/calibration scoring: interpolation, direction, fallback, look-ahead —
plus the track-record machinery in scripts/calibrate: date-joined forward returns,
non-overlapping episode spacing, cold-start seeding, and the emitted edge fields."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app import scoring
from scripts import calibrate
from tests.factories import make_config

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


# --- track record: date-joined forward returns --------------------------------

def test_fwd_idx_uses_dates_not_row_offsets():
    dates = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03",
                            "2024-04-05", "2024-04-06"]).to_numpy()  # 3-month hole
    # 90d from 2024-01-01 -> target 2024-03-31; the 2024-04-05 row is 5d late,
    # inside the tolerance -> row 3 (a row offset would have run off the end).
    assert calibrate._fwd_idx(dates, 0, 90) == 3
    # 30d from 2024-01-03 -> target 2024-02-02; nearest row overshoots by >7d (the
    # hole) -> None, never a silently stretched horizon.
    assert calibrate._fwd_idx(dates, 2, 30) is None
    # Past the end of the sample -> None.
    assert calibrate._fwd_idx(dates, 4, 90) is None


def test_fwd_idx_weekly_rows_keep_calendar_horizons():
    # On a WEEKLY frame a 90d horizon must land ~13 rows ahead, not 90 rows.
    dates = pd.date_range("2024-01-01", periods=60, freq="7D").to_numpy()
    j = calibrate._fwd_idx(dates, 0, 90)
    assert j == 13   # first weekly row >= 90 days later (91d), within tolerance


def test_spaced_episodes_enforce_horizon_separation():
    dates = pd.date_range("2024-01-01", periods=400, freq="D").to_numpy()
    eps = [0, 30, 100, 130, 250]
    assert calibrate._spaced(eps, dates, 90) == [0, 100, 250]
    assert calibrate._spaced(eps, dates, 30) == [0, 30, 100, 130, 250]


# --- track record: cold-start seeding ------------------------------------------

def test_seed_history_prefers_native_and_falls_back_to_panel():
    panel = pd.DataFrame({
        "date": pd.date_range("2018-01-01", periods=10, freq="D"),
        "price_to_wma200": [np.nan] * 5 + [1.0] * 5,   # scoring starts at row 5
        "mayer": [float(i) for i in range(10)],
    })
    native = {"m2_yoy": pd.DataFrame({
        "date": pd.to_datetime(["1960-01-01", "2017-12-01", "2018-06-01"]),
        "m2_yoy": [1.0, 2.0, 3.0]})}
    seeds = calibrate._seed_history(panel, ["m2_yoy", "mayer"], native=native)
    # Native full history strictly before the panel start (2018-06-01 is after).
    assert seeds["m2_yoy"] == [1.0, 2.0]
    # No native frame -> the panel's own pre-start rows.
    assert seeds["mayer"] == [0.0, 1.0, 2.0, 3.0, 4.0]


def _synthetic_panel():
    """600 daily rows; five 5-day 'deep value' dips (each deeper than the last, so
    expanding lower_bullish ranks saturate) separated by 105-day neutral stretches."""
    n = 600
    vals = []
    for i in range(n):
        block, pos = divmod(i, 110)
        vals.append(0.5 - 0.02 * min(block, 4) if pos < 5 and block < 5 else 2.0)
    return pd.DataFrame({
        "date": pd.date_range("2020-01-01", periods=n, freq="D"),
        "close": [100.0 + i * 0.1 for i in range(n)],       # steadily rising
        "price_to_wma200": vals,
        "mayer": vals,
    })


def test_track_record_seeding_recovers_day_one_episodes():
    cfg = make_config()
    panel = _synthetic_panel()
    seeds = {"price_to_wma200": [1.5] * 50, "mayer": [1.5] * 50}
    seeded = calibrate._track_record(panel, cfg, ["price_to_wma200", "mayer"], seeds=seeds)
    unseeded = calibrate._track_record(panel, cfg, ["price_to_wma200", "mayer"])
    # Unseeded, the day-0 dip ranks against n=1 (score 0) and is MISSED; seeding
    # with pre-panel history recovers it — the cold-start bias the fix removes.
    assert seeded["signal_episodes"] == 5
    assert unseeded["signal_episodes"] == 4
    assert seeded["seeded"] == {"price_to_wma200": 50, "mayer": 50}


def test_track_record_emits_effective_n_ci_and_edge():
    cfg = make_config()
    panel = _synthetic_panel()
    seeds = {"price_to_wma200": [1.5] * 50, "mayer": [1.5] * 50}
    tr = calibrate._track_record(panel, cfg, ["price_to_wma200", "mayer"], seeds=seeds)
    h90 = tr["horizons"]["90d"]
    # Episodes are 110 days apart -> all 5 survive the >=90d spacing; every one
    # has a full forward window inside 600 days.
    assert h90["episodes_effective"] == 5
    assert h90["ci"] is not None                     # >=3 non-overlapping outcomes
    assert isinstance(h90["edge"], bool)
    # Rising closes: signal and base both win everywhere -> no edge over base.
    assert h90["edge"] is False
    # 365d: spacing keeps only episodes 0 and 440; 440+365 > 600 -> ONE outcome.
    h365 = tr["horizons"]["365d"]
    assert h365["episodes_effective"] == 1
    assert h365["ci"] is None                        # too few to bootstrap
    assert h365["edge"] is False                     # no CI -> never an edge claim
    # The overlapping-daily rate is still served but labeled descriptive-only.
    assert any("descriptive only" in c for c in tr["caveats"])

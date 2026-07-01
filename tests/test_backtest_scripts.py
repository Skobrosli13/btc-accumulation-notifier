"""Pure-logic tests for the offline backtest/eval scripts (no network).

Covers the honesty fixes:
  * backtest.py: the "200-week MA" needs the FULL 1400-day window (no fabricated
    short-history MA mislabeled as the live indicator).
  * backtest_tops.py: the cached frame is validated for column completeness so a
    rate-capped (degraded) build can't silently pin itself.
  * backtest_flow.py: forming-bar drop mirrors collect_once._closed; the
    Bonferroni z used by the promotion check.
  * eval_netactivity.py: the z-score matches the LIVE definition (trailing window
    EXCLUDING the current point, population std), and bucket days collapse to
    non-overlapping episodes.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts import backtest, backtest_flow, backtest_tops
from scripts import eval_netactivity as ena


# --- backtest.py: honest 200WMA ------------------------------------------------

def test_wma200_requires_full_1400_day_window():
    daily = pd.DataFrame({"date": pd.date_range("2020-01-01", periods=1500, freq="D"),
                          "close": np.linspace(100.0, 200.0, 1500)})
    df = backtest._price_structure_series(daily)
    assert df["wma200"].iloc[:1399].isna().all()      # never a short-window fake
    assert not pd.isna(df["wma200"].iloc[1399])       # full window -> real value
    assert not pd.isna(df["price_to_wma200"].iloc[1499])


def test_wma200_short_history_is_all_nan():
    # ~600 days (the 2015-bottom situation with 2013+ data) -> NO 200WMA at all,
    # matching the live path's None instead of an ~85-week MA labeled as 200w.
    daily = pd.DataFrame({"date": pd.date_range("2020-01-01", periods=600, freq="D"),
                          "close": np.linspace(100.0, 200.0, 600)})
    df = backtest._price_structure_series(daily)
    assert df["wma200"].isna().all()
    assert df["price_to_wma200"].isna().all()


# --- backtest_tops.py: cache completeness ---------------------------------------

def _tops_frame(cols: list[str], n: int = 5) -> pd.DataFrame:
    data = {"date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "close": [100.0] * n}
    for c in cols:
        data[c] = [1.0] * n
    return pd.DataFrame(data)


def test_tops_cache_missing_detects_absent_and_all_nan_columns():
    full = _tops_frame(backtest_tops._EXPECTED_COLS)
    assert backtest_tops._cache_missing(full) == []
    # The committed degraded cache: realized_ratio never made it in.
    degraded = _tops_frame([c for c in backtest_tops._EXPECTED_COLS
                            if c != "realized_ratio"])
    assert backtest_tops._cache_missing(degraded) == ["realized_ratio"]
    # Present but all-NaN is just as degraded.
    nanned = _tops_frame(backtest_tops._EXPECTED_COLS)
    nanned["sopr"] = np.nan
    assert backtest_tops._cache_missing(nanned) == ["sopr"]


def test_tops_expected_cols_exclude_funding_only():
    # funding has no free deep history (documented exclusion); everything else in
    # TOP_THRESHOLDS must be validated.
    from app import scoring
    assert set(backtest_tops._EXPECTED_COLS) == set(scoring.TOP_THRESHOLDS) - {"funding"}


# --- backtest_flow.py: closed bars + multiplicity --------------------------------

def test_drop_forming_mirrors_collect_once_closed():
    rows = [{"ts": 1}, {"ts": 2}, {"ts": 3}]
    assert backtest_flow._drop_forming(rows) == [{"ts": 1}, {"ts": 2}]
    single = [{"ts": 1}]
    assert backtest_flow._drop_forming(single) == single    # never empties the list


def test_bonferroni_z_widens_with_cells():
    assert backtest_flow._bonferroni_z(1) == pytest.approx(1.96)
    z18 = backtest_flow._bonferroni_z(18)
    assert z18 > 2.9                       # 0.05/18 two-sided ~ 2.99
    assert backtest_flow._bonferroni_z(100) > z18


def test_flow_collapse_keeps_first_of_each_run():
    assert backtest_flow._collapse([10, 11, 12, 30], gap=3) == [10, 30]
    assert backtest_flow._collapse([10, 14, 18], gap=3) == [10, 14, 18]


# --- eval_netactivity.py: live-aligned z + episode spacing -----------------------

def test_zscore_excludes_current_point_and_uses_population_std():
    vals = [10.0, 12.0] * 30 + [20.0]
    s = pd.Series(vals, index=pd.date_range("2024-01-01", periods=61, freq="D"))
    z = ena._zscore(s, window=60, minp=20)
    base = vals[:60]                       # trailing window EXCLUDES the 20.0
    expected = (20.0 - np.mean(base)) / np.std(base)   # ddof=0 (pstdev)
    assert z.iloc[-1] == pytest.approx(expected)
    # An include-current, sample-std z would read materially lower — guard the gap.
    incl = (20.0 - np.mean(vals[1:])) / np.std(vals[1:], ddof=1)
    assert abs(z.iloc[-1] - incl) > 0.5


def test_zscore_flat_baseline_yields_nan_like_live_none():
    vals = [10.0] * 40 + [20.0]
    s = pd.Series(vals, index=pd.date_range("2024-01-01", periods=41, freq="D"))
    z = ena._zscore(s, window=30, minp=20)
    assert pd.isna(z.iloc[-1])             # live returns z=None on a flat baseline


def test_episode_dates_enforce_gap():
    dates = list(pd.to_datetime(["2024-01-01", "2024-01-05", "2024-02-15",
                                 "2024-02-20", "2024-06-01"]))
    kept = ena._episode_dates(dates, 30)
    assert kept == [dates[0], dates[2], dates[4]]


def test_bucket_stats_counts_episodes_not_days():
    # 10 consecutive bucket days with a 30d horizon = ONE episode, not ten.
    idx = pd.date_range("2024-01-01", periods=10, freq="D")
    sub = pd.DataFrame({"fwd30": [0.05] * 10}, index=idx)
    s = ena._bucket_stats(sub, 30)
    assert s["episodes"] == 1
    assert s["hit"] == 1.0
    assert 0.0 <= s["lo"] < s["hi"] <= 1.0

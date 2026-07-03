"""Hand-computed fixtures for the SRW-SUE extractor (M1 §4.5).

Pins the point-in-time discipline (earliest-filed dedup, YTD-fact exclusion, Q4
derivation) and the standardization math, so the surprise magnitude can never
regress into look-ahead or a fabricated value.
"""
from __future__ import annotations

import math
import statistics

from app.data.equities.edgar import xbrl_eps


def _fact(fy, fp, start, end, val, filed):
    return {"fy": fy, "fp": fp, "start": start, "end": end, "val": val, "filed": filed}


def test_parse_dedupes_to_earliest_filed_and_derives_q4():
    payload = {"units": {"USD/shares": [
        # FY2022 quarters (true ~quarter durations)
        _fact(2022, "Q1", "2022-01-01", "2022-03-31", 1.0, "2022-04-15"),
        # a RESTATEMENT of Q1 filed later -> must be dropped (look-ahead)
        _fact(2022, "Q1", "2022-01-01", "2022-03-31", 1.1, "2022-11-01"),
        _fact(2022, "Q2", "2022-04-01", "2022-06-30", 1.0, "2022-07-15"),
        # a 6-month YTD fact ALSO tagged Q2 -> must be excluded by the duration filter
        _fact(2022, "Q2", "2022-01-01", "2022-06-30", 2.0, "2022-07-15"),
        _fact(2022, "Q3", "2022-07-01", "2022-09-30", 1.0, "2022-10-15"),
        # annual (10-K) fact -> Q4 = FY - (Q1+Q2+Q3)
        _fact(2022, "FY", "2022-01-01", "2022-12-31", 5.0, "2023-02-15"),
    ]}}
    eps = xbrl_eps.parse_companyconcept(payload)
    assert eps[(2022, 1)] == 1.0        # earliest-filed value kept, not the restatement
    assert eps[(2022, 2)] == 1.0        # YTD 6-month fact ignored
    assert eps[(2022, 3)] == 1.0
    assert eps[(2022, 4)] == 2.0        # 5.0 - (1+1+1)


def test_seasonal_sue_uses_preceding_window_and_guards():
    # Y0 baseline 0, so a year's seasonal diff is just later-EPS minus prior-year.
    #   Y1 EPS = [1,1,1,1]          -> Y1 diffs = [1,1,1,1]
    #   Y2 EPS = [2,2,9,5]          -> Y2 diffs = [1,1,8,4]
    # Chronological diff series = [1,1,1,1, 1,1,8, 4].
    # SUE(2,4): scaled by stdev of the 7 PRECEDING diffs [1,1,1,1,1,1,8].
    eps = {}
    for q in (1, 2, 3, 4):
        eps[(0, q)] = 0.0
        eps[(1, q)] = 1.0
    eps[(2, 1)], eps[(2, 2)], eps[(2, 3)], eps[(2, 4)] = 2.0, 2.0, 9.0, 5.0
    sue = xbrl_eps.seasonal_sue(eps)
    expected = 4.0 / statistics.stdev([1, 1, 1, 1, 1, 1, 8])   # diff(2,4)=4
    assert math.isclose(sue[(2, 4)], expected, rel_tol=1e-9)
    # (2,3): its 6 preceding diffs are all 1.0 -> zero variance -> undefined.
    assert (2, 3) not in sue
    # (2,2): only 5 preceding diffs (< min_diffs) -> skipped.
    assert (2, 2) not in sue
    assert (1, 1) not in sue


def test_seasonal_sue_winsorizes():
    # A big current surprise against a tiny-variance preceding window blows past
    # the winsor bound and clips to +/-10 (only possible because sigma excludes
    # the current quarter).
    eps = {}
    for q in (1, 2, 3, 4):
        eps[(0, q)] = 0.0
        eps[(1, q)] = 1.0
    # Y2 diffs = [1,1,0, 100]; preceding window for (2,4) = [1,1,1,1,1,1,0].
    eps[(2, 1)], eps[(2, 2)], eps[(2, 3)], eps[(2, 4)] = 2.0, 2.0, 1.0, 101.0
    sue = xbrl_eps.seasonal_sue(eps)
    assert sue[(2, 4)] == 10.0        # winsorized


def test_split_adjust_callable_is_applied():
    # Without adjustment the two years' Q1 differ only by a 2x split; with a
    # factor that halves the pre-split year, the seasonal diff shrinks.
    eps = {(y, q): v for y in (0, 1, 2) for q, v in
           zip((1, 2, 3, 4), (1.0, 1.0, 1.0, 1.0))}
    # make one later value large enough to yield a defined SUE, then check adjust
    # changes the diff sign/scale via a 0.5 factor on year 0.
    diffs_plain = xbrl_eps._seasonal_diffs(eps)
    diffs_adj = xbrl_eps._seasonal_diffs(eps, adjust=lambda fy, q: 0.5 if fy == 0 else 1.0)
    assert diffs_plain[(1, 1)] == 0.0
    assert diffs_adj[(1, 1)] == 1.0 - 0.5 * 1.0   # 0.5

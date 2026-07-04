"""Hand-computed fixtures for the harness statistical core (§5.4).

Every value below is worked out by hand in the comments — these pin the math
the gates run on, so a silent regression in the honesty machinery is impossible.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from app.harness import stats


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)


# Six events across three months: Jan [1,1], Feb [2], Mar [3,3,3].
_VALS = [1.0, 1.0, 2.0, 3.0, 3.0, 3.0]
_TS = [_ms("2026-01-05"), _ms("2026-01-20"), _ms("2026-02-10"),
       _ms("2026-03-03"), _ms("2026-03-14"), _ms("2026-03-30")]


def test_month_key_utc():
    assert stats.month_key(_ms("2026-03-15")) == "2026-03"


def test_monthly_means_groups_by_calendar_month():
    assert stats.monthly_means(_VALS, _TS) == [1.0, 2.0, 3.0]


def test_clustered_t_hand_value():
    # monthly means [1,2,3]: mean 2, sample var ((1)^2+0+(1)^2)/2 = 1,
    # se = sqrt(1/3), t = 2*sqrt(3).
    r = stats.clustered_t(_VALS, _TS)
    assert r["n_months"] == 3
    assert r["monthly_mean"] == pytest.approx(2.0)
    assert r["t"] == pytest.approx(2 * math.sqrt(3))
    # event-weighted mean for display: (1+1+2+3+3+3)/6
    assert r["mean"] == pytest.approx(13 / 6)


def test_clustered_t_degenerate_guards():
    # one month -> no t (not a fabricated infinity)
    one = stats.clustered_t([1.0, 2.0], [_ms("2026-01-05"), _ms("2026-01-25")])
    assert one["t"] is None and one["n_months"] == 1
    # two months with identical means -> zero variance -> no t
    flat = stats.clustered_t([1.0, 1.0], [_ms("2026-01-05"), _ms("2026-02-05")])
    assert flat["t"] is None
    assert stats.clustered_t([], [])["t"] is None


def test_clustered_delta_t_hand_value():
    # arm a: monthly means [1,2,3] (mean 2, var 1, n 3)
    # arm b: Jan [0], Feb [1] -> monthly means [0,1] (mean .5, var .5, n 2)
    # delta = 1.5; se = sqrt(1/3 + 0.5/2) = sqrt(7/12); t = 1.5/sqrt(7/12)
    b_vals = [0.0, 1.0]
    b_ts = [_ms("2026-01-08"), _ms("2026-02-18")]
    r = stats.clustered_delta_t(_VALS, _TS, b_vals, b_ts)
    assert r["delta"] == pytest.approx(1.5)
    assert r["t"] == pytest.approx(1.5 / math.sqrt(7 / 12))
    assert (r["n_months_a"], r["n_months_b"]) == (3, 2)


def test_spaced_subset_greedy_non_overlap():
    # gaps vs last KEPT: keep 0; 10-0<15 drop; 20-0>=15 keep; 35-20>=15 keep.
    assert stats.spaced_subset([0, 10, 20, 35], 15) == [0, 2, 3]
    assert stats.spaced_subset([], 15) == []


def test_winsorize_clips_at_percentiles():
    vals = [float(i) for i in range(101)]          # 0..100
    w = stats.winsorize(vals, lo=0.05, hi=0.95)    # pct(.05)=5.0, pct(.95)=95.0
    assert min(w) == 5.0 and max(w) == 95.0
    assert w[50] == 50.0                            # interior untouched
    assert stats.winsorize([1.0, 2.0]) == [1.0, 2.0]  # n<3 passthrough


def test_bootstrap_ci_deterministic_bounds():
    assert stats.bootstrap_ci([1, 1, 1, 1]) == [1.0, 1.0]
    ci = stats.bootstrap_ci([0, 1] * 10, seed=7)
    assert ci is not None and 0.0 <= ci[0] < ci[1] <= 1.0
    assert stats.bootstrap_ci([1, 1]) is None       # below 3 samples


def test_wilson_ci_matches_published_value():
    # 8/10 at 95%: the standard published Wilson interval (0.4902, 0.9433).
    lo, hi = stats.wilson_ci(8, 10)
    assert lo == pytest.approx(0.4902, abs=1e-4)
    assert hi == pytest.approx(0.9433, abs=1e-4)
    assert stats.wilson_ci(0, 0) is None

"""SUE coverage report — the pure summary stat."""
from __future__ import annotations

from scripts.sue_coverage import summarize


def test_summarize_coverage():
    # 3 names, expecting 40 quarters each (10y); one fully covered, one partial, one none.
    s = summarize({"AAA": 40, "BBB": 20, "CCC": 0}, expected_per_name=40)
    assert s["names"] == 3
    assert s["quarters_with_sue"] == 60 and s["expected_quarters"] == 120
    assert abs(s["coverage"] - 0.5) < 1e-9
    assert s["median_quarters_per_name"] == 20
    assert s["names_with_zero"] == 1


def test_summarize_empty():
    s = summarize({}, expected_per_name=40)
    assert s["coverage"] == 0.0 and s["names"] == 0

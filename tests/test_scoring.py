"""Scoring + graceful-degradation tests."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app import alerting, scoring
from app.config import Config

from tests.factories import make_config


# --- linear_score ------------------------------------------------------------

def test_linear_score_lower_is_bullish():
    # neutral > extreme => lower value scores higher
    assert scoring.linear_score(2.0, neutral=2.0, extreme=0.0) == pytest.approx(0.0)
    assert scoring.linear_score(0.0, neutral=2.0, extreme=0.0) == pytest.approx(1.0)
    assert scoring.linear_score(1.0, neutral=2.0, extreme=0.0) == pytest.approx(0.5)


def test_linear_score_higher_is_bullish():
    # neutral < extreme => higher value scores higher
    assert scoring.linear_score(0.0, neutral=0.0, extreme=8.0) == pytest.approx(0.0)
    assert scoring.linear_score(8.0, neutral=0.0, extreme=8.0) == pytest.approx(1.0)
    assert scoring.linear_score(4.0, neutral=0.0, extreme=8.0) == pytest.approx(0.5)


def test_m2_yoy_recalibrated_band():
    # Locks the recalibrated threshold (neutral=2, extreme=10): contraction scores
    # ~0, typical ~5-6% lands mid-band, strong easing maxes out.
    s = scoring.score_indicators
    assert s({"m2_yoy": 0.0})["m2_yoy"] < 0.2
    assert 0.3 < s({"m2_yoy": 6.0})["m2_yoy"] < 0.7
    assert s({"m2_yoy": 10.0})["m2_yoy"] == pytest.approx(1.0)


def test_linear_score_clamps_and_degenerate():
    assert scoring.linear_score(-5.0, neutral=2.0, extreme=0.0) == 1.0  # beyond extreme
    assert scoring.linear_score(99.0, neutral=2.0, extreme=0.0) == 0.0  # beyond neutral
    assert scoring.linear_score(5.0, neutral=1.0, extreme=1.0) == 0.0   # neutral == extreme


# --- category_score ----------------------------------------------------------

def test_category_score_mean_of_available():
    assert scoring.category_score({"a": 0.2, "b": 0.8}) == pytest.approx(0.5)
    assert scoring.category_score({"a": 0.4, "b": None}) == pytest.approx(0.4)


def test_category_score_all_none_is_none():
    assert scoring.category_score({"a": None, "b": None}) is None
    assert scoring.category_score({}) is None


# --- composite + renormalization ---------------------------------------------

def test_composite_renormalizes_when_category_missing():
    # Only price+sentiment available; weights renormalize over those two.
    cats = {"onchain": None, "price": 1.0, "macro": None, "sentiment": 0.0, "derivs": None}
    weights = {"onchain": 0.35, "price": 0.20, "macro": 0.20, "sentiment": 0.10, "derivs": 0.15}
    score, active = scoring.composite(cats, weights, cycle_multiplier=1.0)
    # renormalized: price weight 0.20/0.30 = 2/3, sentiment 0.10/0.30 = 1/3
    assert score == pytest.approx(100 * (2 / 3) * 1.0)
    assert active == ["price", "sentiment"]


def test_composite_all_categories_full():
    cats = {"onchain": 1.0, "price": 1.0, "macro": 1.0, "sentiment": 1.0, "derivs": 1.0}
    weights = {"onchain": 0.35, "price": 0.20, "macro": 0.20, "sentiment": 0.10, "derivs": 0.15}
    score, active = scoring.composite(cats, weights, cycle_multiplier=1.0)
    assert score == pytest.approx(100.0)
    assert len(active) == 5


def test_composite_empty_returns_zero():
    score, active = scoring.composite({"price": None}, {"price": 0.2}, 1.0)
    assert score == 0.0 and active == []


def test_composite_clamped_by_multiplier():
    cats = {"price": 1.0}
    score, _ = scoring.composite(cats, {"price": 0.2}, cycle_multiplier=1.1)
    assert score == 100.0  # 110 clamped to 100


# --- tiers -------------------------------------------------------------------

def test_tier_thresholds():
    assert scoring.tier(10, 100, 200, 40, 60, 80) == "NEUTRAL"
    assert scoring.tier(45, 100, 200, 40, 60, 80) == "WATCH"
    assert scoring.tier(65, 100, 200, 40, 60, 80) == "ACCUMULATE"


def test_deep_value_requires_price_below_wma200():
    # score high enough, but price ABOVE 200wma => not deep value
    assert scoring.tier(85, 250, 200, 40, 60, 80) == "ACCUMULATE"
    # price at/below 200wma => deep value
    assert scoring.tier(85, 150, 200, 40, 60, 80) == "DEEP_VALUE"
    # no wma available => cannot be deep value
    assert scoring.tier(85, 150, None, 40, 60, 80) == "ACCUMULATE"


# --- score_indicators / category_scores --------------------------------------

def test_score_indicators_skips_missing():
    readings = {"mvrv_z": 0.0, "fng": 10.0}  # others absent
    sub = scoring.score_indicators(readings)
    assert sub["mvrv_z"] == pytest.approx(1.0)
    assert sub["fng"] == pytest.approx(1.0)
    assert sub["mayer"] is None  # not provided


def test_category_scores_aggregate():
    readings = {"price_to_wma200": 0.85, "mayer": 0.5}  # both deep => 1.0 each
    sub = scoring.score_indicators(readings)
    cats = scoring.category_scores(sub)
    assert cats["price"] == pytest.approx(1.0)
    assert cats["onchain"] is None


# --- cycle multiplier --------------------------------------------------------

def test_cycle_multiplier_bounds_and_peak():
    ath = date(2025, 10, 6)
    at_window = ath + timedelta(days=370)
    assert scoring.cycle_multiplier(at_window, ath, 370) == pytest.approx(1.1)
    far = ath + timedelta(days=370 + 1000)
    assert scoring.cycle_multiplier(far, ath, 370) == pytest.approx(0.9)
    # always within [0.9, 1.1]
    for d in range(0, 1500, 50):
        m = scoring.cycle_multiplier(ath + timedelta(days=d), ath, 370)
        assert 0.9 <= m <= 1.1


def test_cycle_multiplier_swing_and_killswitch():
    ath = date(2025, 10, 6)
    at = ath + timedelta(days=370)
    far = ath + timedelta(days=370 + 2000)
    # softer swing 0.05 -> band [0.95, 1.05]
    assert scoring.cycle_multiplier(at, ath, 370, swing=0.05) == pytest.approx(1.05)
    assert scoring.cycle_multiplier(far, ath, 370, swing=0.05) == pytest.approx(0.95)
    # kill-switch: swing=0 -> always exactly 1.0 (timing off)
    assert scoring.cycle_multiplier(at, ath, 370, swing=0.0) == pytest.approx(1.0)
    assert scoring.cycle_multiplier(far, ath, 370, swing=0.0) == pytest.approx(1.0)


# --- indicators_in_zone ------------------------------------------------------

def test_indicators_in_zone_uses_labels():
    sub = {"mvrv_z": 0.9, "fng": 0.1, "mayer": None}
    labels = scoring.indicators_in_zone(sub)
    assert "MVRV Z-Score" in labels
    assert "Fear & Greed" not in labels


# --- flash + decide_alerts ---------------------------------------------------

def _cfg(**over) -> Config:
    return make_config(**over)


def test_flash_fires_on_free_proxy():
    cfg = _cfg()
    readings = {"fng": 8, "drop_24_48h_pct": 12, "funding": -0.0005}
    assert alerting.evaluate_flash(readings, cfg) is True


def test_flash_needs_all_conditions():
    cfg = _cfg()
    # F&G too high
    assert alerting.evaluate_flash({"fng": 20, "drop_24_48h_pct": 12, "funding": -0.0005}, cfg) is False
    # drop too small
    assert alerting.evaluate_flash({"fng": 8, "drop_24_48h_pct": 3, "funding": -0.0005}, cfg) is False
    # no capitulation signal
    assert alerting.evaluate_flash({"fng": 8, "drop_24_48h_pct": 12, "funding": 0.0001}, cfg) is False
    # missing data => conservative no-fire
    assert alerting.evaluate_flash({"fng": None, "drop_24_48h_pct": 12}, cfg) is False


def test_decide_alerts_tier_change_only():
    now = datetime(2026, 6, 7, tzinfo=timezone.utc)
    # change into an alert tier => fire
    d = alerting.decide_alerts("WATCH", "NEUTRAL", False, None, 3, now)
    assert d["tier_alert"] is True
    # no change => no fire
    d = alerting.decide_alerts("WATCH", "WATCH", False, None, 3, now)
    assert d["tier_alert"] is False
    # change down to NEUTRAL => no fire (NEUTRAL not an alert tier)
    d = alerting.decide_alerts("NEUTRAL", "WATCH", False, None, 3, now)
    assert d["tier_alert"] is False


def test_decide_alerts_flash_debounce():
    now = datetime(2026, 6, 7, tzinfo=timezone.utc)
    # never fired => fire
    assert alerting.decide_alerts("NEUTRAL", "NEUTRAL", True, None, 3, now)["flash_alert"] is True
    # fired 1 day ago, debounce 3 => suppressed
    recent = now - timedelta(days=1)
    assert alerting.decide_alerts("NEUTRAL", "NEUTRAL", True, recent, 3, now)["flash_alert"] is False
    # fired 4 days ago => fires again
    old = now - timedelta(days=4)
    assert alerting.decide_alerts("NEUTRAL", "NEUTRAL", True, old, 3, now)["flash_alert"] is True

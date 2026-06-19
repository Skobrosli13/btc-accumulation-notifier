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


def test_reserve_risk_band():
    # lower_bullish: deep value (p10 ~0.0012) -> 1.0, median (~0.0025) -> 0.0.
    s = scoring.score_indicators
    assert s({"reserve_risk": 0.0012})["reserve_risk"] == pytest.approx(1.0)
    assert s({"reserve_risk": 0.0025})["reserve_risk"] == pytest.approx(0.0)
    assert s({"reserve_risk": (0.0012 + 0.0025) / 2})["reserve_risk"] == pytest.approx(0.5)


def test_net_liq_and_nfci_bands():
    s = scoring.score_indicators
    assert s({"net_liq_yoy": 10.0})["net_liq_yoy"] == pytest.approx(1.0)
    assert s({"net_liq_yoy": 0.0})["net_liq_yoy"] == pytest.approx(0.0)
    # NFCI higher_bullish: tight/stress (+0.6) -> 1.0; loose (<0) clamps to 0.
    assert s({"nfci": 0.6})["nfci"] == pytest.approx(1.0)
    assert s({"nfci": 0.0})["nfci"] == pytest.approx(0.0)
    assert s({"nfci": -0.5})["nfci"] == pytest.approx(0.0)


def test_hash_ribbon_identity_passthrough():
    # The adapter emits a cooked 0..1; the threshold band is identity.
    s = scoring.score_indicators
    assert s({"hash_ribbon": 1.0})["hash_ribbon"] == pytest.approx(1.0)
    assert s({"hash_ribbon": 0.3})["hash_ribbon"] == pytest.approx(0.3)
    assert s({"hash_ribbon": 0.0})["hash_ribbon"] == pytest.approx(0.0)


def test_macro_liquidity_and_stress_groups_collapse():
    # m2_yoy+net_liq_yoy collapse to ONE liquidity term; hy_spread+nfci to ONE
    # stress term — correlated macro inputs must not double-count.
    sub = scoring.score_indicators({"m2_yoy": 10.0, "net_liq_yoy": 0.0, "hy_spread": 3.5})
    # liquidity term = mean(1.0, 0.0) = 0.5 ; stress term = mean(hy=0.0) = 0.0
    cats = scoring.category_scores(sub)
    assert cats["macro"] == pytest.approx(0.25)


def test_cohort_sopr_and_mvrv_bands():
    s = scoring.score_indicators
    # LTH-SOPR lower_bullish: neutral 1.0 / extreme 0.6 (LTH capitulation)
    assert s({"lth_sopr": 0.6})["lth_sopr"] == pytest.approx(1.0)
    assert s({"lth_sopr": 1.0})["lth_sopr"] == pytest.approx(0.0)
    # STH-SOPR neutral 1.0 / extreme 0.93
    assert s({"sth_sopr": 0.93})["sth_sopr"] == pytest.approx(1.0)
    assert s({"sth_sopr": 1.0})["sth_sopr"] == pytest.approx(0.0)
    # LTH-MVRV neutral 2.4 / extreme 1.0
    assert s({"lth_mvrv": 1.0})["lth_mvrv"] == pytest.approx(1.0)
    assert s({"lth_mvrv": 2.4})["lth_mvrv"] == pytest.approx(0.0)


def test_lth_mvrv_joins_valuation_group():
    # mvrv_z deep (1.0) + lth_mvrv neutral (0.0) collapse to ONE valuation term (0.5);
    # with a standalone puell (1.0) -> onchain = mean(0.5, 1.0) = 0.75. If lth_mvrv
    # were its own term it would be mean(1.0, 0.0, 1.0) = 0.667.
    sub = scoring.score_indicators({"mvrv_z": 0.0, "lth_mvrv": 2.4, "puell": 0.3})
    assert scoring.category_scores(sub)["onchain"] == pytest.approx(0.75)


def test_sth_sopr_groups_with_aggregate_sopr():
    # sopr (1.0) + sth_sopr (0.0) collapse to ONE term (0.5); standalone puell (1.0)
    # -> onchain = mean(0.5, 1.0) = 0.75 (not 0.667 from three separate terms).
    sub = scoring.score_indicators({"sopr": 0.95, "sth_sopr": 1.0, "puell": 0.3})
    assert scoring.category_scores(sub)["onchain"] == pytest.approx(0.75)


def test_ssr_band():
    # lower_bullish: max dry powder (SSR 3) -> 1.0; little dry powder (SSR 6) -> 0.0.
    s = scoring.score_indicators
    assert s({"ssr": 3.0})["ssr"] == pytest.approx(1.0)
    assert s({"ssr": 6.0})["ssr"] == pytest.approx(0.0)
    assert s({"ssr": 4.5})["ssr"] == pytest.approx(0.5)


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


def test_tier_hysteresis_dead_band():
    # margin 2: a composite just over a floor doesn't upgrade until it clears floor+margin
    assert scoring.tier_hysteresis(61, 100, 200, "WATCH", 40, 60, 80, margin=2) == "WATCH"   # 61 < 60+2
    assert scoring.tier_hysteresis(63, 100, 200, "WATCH", 40, 60, 80, margin=2) == "ACCUMULATE"  # >= 62
    # holds the higher tier until it falls margin below the floor (no whipsaw)
    assert scoring.tier_hysteresis(59, 100, 200, "ACCUMULATE", 40, 60, 80, margin=2) == "ACCUMULATE"  # 59 > 60-2
    assert scoring.tier_hysteresis(57, 100, 200, "ACCUMULATE", 40, 60, 80, margin=2) == "WATCH"        # <= 58
    # margin 0 reproduces plain tier
    assert scoring.tier_hysteresis(61, 100, 200, "WATCH", 40, 60, 80, margin=0) == "ACCUMULATE"


def test_category_agreement():
    assert scoring.category_agreement({"a": 0.8, "b": 0.7}) == {
        "active": 2, "spread": 0.1, "agreement": 0.9, "confidence": "high"}
    low = scoring.category_agreement({"a": 0.9, "b": 0.1, "c": None})
    assert low["confidence"] == "low" and low["active"] == 2
    assert scoring.category_agreement({"a": 0.5, "b": None}) is None   # <2 active


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


# --- froth (sell-side overheat) ------------------------------------------------

def test_froth_subscores_neutral_and_extreme():
    # at/below neutral -> 0; at/above extreme -> 1; midpoint -> 0.5
    # (anchors from the backtest-revised TOP_THRESHOLDS)
    subs = scoring.froth_subscores({"mvrv_z": 2.0, "mayer": 1.55, "fng": 72.5})
    assert subs["mvrv_z"] == pytest.approx(0.0)
    assert subs["mayer"] == pytest.approx(1.0)
    assert subs["fng"] == pytest.approx(0.5)
    assert subs["nupl"] is None  # not provided


def test_froth_monotonic_in_raw_value():
    cold = scoring.froth_score({"mvrv_z": 1.0})["score"]
    warm = scoring.froth_score({"mvrv_z": 2.6})["score"]
    hot = scoring.froth_score({"mvrv_z": 3.5})["score"]
    assert cold < warm < hot
    assert cold == pytest.approx(0.0) and hot == pytest.approx(100.0)


def test_froth_collapses_correlated_families():
    # mvrv_z/nupl/realized_ratio are one family: maxing all three must score the
    # same as maxing one (no triple-counting), with fng pulling the mean down.
    one = scoring.froth_score({"mvrv_z": 9.0, "fng": 60.0})
    three = scoring.froth_score({"mvrv_z": 9.0, "nupl": 0.9, "realized_ratio": 4.0, "fng": 60.0})
    assert one["score"] == pytest.approx(three["score"]) == pytest.approx(50.0)
    assert one["active"] == three["active"] == 2


def test_froth_none_tolerant_and_empty():
    out = scoring.froth_score({})
    assert out["score"] is None and out["active"] == 0 and out["in_zone"] == []
    # missing indicators renormalize away rather than dragging the mean down
    assert scoring.froth_score({"fng": 90.0})["score"] == pytest.approx(100.0)


def test_froth_in_zone_uses_labels():
    out = scoring.froth_score({"mayer": 2.4, "fng": 60.0})
    assert "Mayer Multiple" in out["in_zone"]
    assert "Fear & Greed" not in out["in_zone"]


def test_zone_boundary_inverts_linear_mapping():
    # lower_bullish (mvrv_z: neutral 2, extreme 0): score 0.6 at raw 0.8.
    b = scoring.zone_boundary_raw("mvrv_z")
    assert b == pytest.approx(0.8)
    assert scoring.score_indicators({"mvrv_z": b})["mvrv_z"] == pytest.approx(0.6)
    # top side (mayer: 1.2 -> 1.55, higher = frothy): score 0.6 at 1.2 + 0.6*0.35.
    tb = scoring.top_zone_boundary_raw("mayer")
    assert tb == pytest.approx(1.41)
    assert scoring.froth_subscores({"mayer": tb})["mayer"] == pytest.approx(0.6)
    assert scoring.zone_boundary_raw("nope") is None
    assert scoring.top_zone_boundary_raw("m2_yoy") is None  # not a froth indicator


def test_zone_boundary_inverts_calibrated_mapping():
    # With calibration active the boundary must come from the SAME percentile
    # mapping the scorer uses, not the linear fallback.
    scoring.set_calibration({
        "probs": [0.0, 1.0],
        "indicators": {"price_to_wma200": {
            "direction": "lower_bullish", "breakpoints": [1.0, 3.0]}},
    })
    try:
        b = scoring.zone_boundary_raw("price_to_wma200")
        # lower_bullish: score 0.6 <=> quantile 0.4 <=> 1.0 + 0.4*(3.0-1.0)
        assert b == pytest.approx(1.8)
        assert scoring.score_indicators({"price_to_wma200": b})["price_to_wma200"] == pytest.approx(0.6)
    finally:
        scoring.set_calibration({})


def test_nan_readings_score_as_missing():
    # NaN survives `is None` checks and clamps into a full 1.0 sub-score via
    # min/max (a false maximal signal) — it must be treated as missing on BOTH
    # the buy side and the froth side.
    nan = float("nan")
    assert scoring.score_indicators({"mvrv_z": nan})["mvrv_z"] is None
    out = scoring.froth_score({"mvrv_z": nan})
    assert out["score"] is None and out["in_zone"] == [] and out["active"] == 0
    # mixed: NaN renormalizes away, finite values still score
    mixed = scoring.froth_score({"mvrv_z": nan, "fng": 90.0})
    assert mixed["score"] == pytest.approx(100.0) and mixed["active"] == 1
    # non-numeric garbage is also missing, not a crash
    assert scoring.score_indicators({"mvrv_z": "n/a"})["mvrv_z"] is None


def test_froth_band_hysteresis():
    fb = scoring.froth_band
    assert fb(None) is None
    assert fb(10.0) == "COOL"
    assert fb(60.0) == "FROTHY"
    assert fb(80.0) == "OVERHEATED"
    # upgrade requires clearing the new floor by the margin
    assert fb(51.0, "WARMING") == "WARMING"   # inside the dead-band -> holds
    assert fb(53.5, "WARMING") == "FROTHY"
    # downgrade requires falling the margin below the previous floor
    assert fb(48.0, "FROTHY") == "FROTHY"     # holds
    assert fb(46.9, "FROTHY") == "WARMING"
    # a two-band jump clears in one step when it beats the top floor + margin
    assert fb(80.0, "COOL") == "OVERHEATED"
    # a multi-band jump landing INSIDE the top band's dead-band steps to the
    # highest intermediate band it cleared — it must not fall back to prev
    # (a 76 score holding a COOL label would suppress the overheat alert).
    assert fb(76.0, "COOL") == "FROTHY"
    assert fb(51.0, "COOL") == "WARMING"
    assert fb(76.0, "WARMING") == "FROTHY"


def test_next_froth_cursor_debounce():
    nc = alerting.next_froth_cursor
    # advances upward freely; alert+ok advances too
    assert nc("WARMING", "COOL", False, True) == "WARMING"
    assert nc("FROTHY", "WARMING", True, True) == "FROTHY"
    # failed send holds (retry next run)
    assert nc("FROTHY", "WARMING", True, False) == "WARMING"
    # downgrade holds unless a full cool-down to COOL — the oscillation debounce:
    # 55 -> 47 -> 53 must NOT re-email; 55 -> 20 -> 53 must.
    assert nc("WARMING", "FROTHY", False, True) == "FROTHY"
    assert alerting.decide_froth_alert("FROTHY", "FROTHY") is False  # 47->53 re-entry: quiet
    assert nc("COOL", "FROTHY", False, True) == "COOL"
    assert alerting.decide_froth_alert("FROTHY", "COOL") is True     # full round trip: re-arms
    # None band holds the cursor
    assert nc(None, "FROTHY", False, True) == "FROTHY"


def test_decide_froth_alert_crossings():
    d = alerting.decide_froth_alert
    assert d("FROTHY", None) is True             # first ever crossing
    assert d("FROTHY", "WARMING") is True        # upward crossing
    assert d("FROTHY", "FROTHY") is False        # no repeat at the same band
    assert d("OVERHEATED", "FROTHY") is True     # escalation fires again
    assert d("FROTHY", "OVERHEATED") is False    # de-escalation is silent
    assert d("WARMING", "COOL") is False         # below the alert bands
    assert d(None, None) is False
    # cursor falls back quietly when the band cools, so a re-entry re-alerts
    assert d("FROTHY", "WARMING") is True


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

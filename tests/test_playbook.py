"""Playbook helpers: conviction, laddering, what-to-do, ATR trade levels, diff."""
from __future__ import annotations

import pytest

from app import alerting, playbook, shortterm


def test_conviction_within_band():
    assert playbook.conviction(60, "ACCUMULATE", 40, 60, 80) == pytest.approx(0.0)
    assert playbook.conviction(70, "ACCUMULATE", 40, 60, 80) == pytest.approx(0.5)
    assert playbook.conviction(80, "ACCUMULATE", 40, 60, 80) == pytest.approx(1.0)


def test_laddering_none_below_accumulate():
    for tier in ("NEUTRAL", "WATCH"):
        assert playbook.laddering_plan(composite=50, tier=tier, conviction_=0.5,
            price=60000, wma200=50000, realized_price=40000, atr_daily=1000) is None


def test_laddering_deploy_now_scales_and_sums():
    lo = playbook.laddering_plan(composite=60, tier="ACCUMULATE", conviction_=0.0,
        price=60000, wma200=50000, realized_price=40000, atr_daily=1000)
    hi = playbook.laddering_plan(composite=80, tier="ACCUMULATE", conviction_=1.0,
        price=60000, wma200=50000, realized_price=40000, atr_daily=1000)
    assert lo["deploy_now_pct"] == 25 and hi["deploy_now_pct"] == 50
    assert hi["deploy_now_pct"] > lo["deploy_now_pct"]
    assert sum(t["pct"] for t in lo["tranches"]) == pytest.approx(100, abs=2)
    assert all(t["price"] < 60000 for t in lo["tranches"][1:])     # ladder buys lower
    assert "not financial advice" in lo["disclaimer"].lower()


def test_laddering_deep_value_heavier():
    dv = playbook.laddering_plan(composite=90, tier="DEEP_VALUE", conviction_=0.5,
        price=60000, wma200=50000, realized_price=40000, atr_daily=1000)
    assert dv["deploy_now_pct"] == 75   # base 50 + 0.5*50


def test_what_to_do_matrix():
    oversold = playbook.what_to_do_now(long_tier="ACCUMULATE", long_conviction=0.5,
        st_state="STRONG_SELL", st_triggers=[])
    assert "higher-conviction" in oversold["stance"]
    hot = playbook.what_to_do_now(long_tier="ACCUMULATE", long_conviction=0.5,
        st_state="STRONG_BUY", st_triggers=[])
    assert "hot" in hot["stance"]
    watch = playbook.what_to_do_now(long_tier="WATCH", long_conviction=0.2,
        st_state="NEUTRAL", st_triggers=[])
    assert "prepare" in watch["stance"]
    neutral = playbook.what_to_do_now(long_tier="NEUTRAL", long_conviction=0.0,
        st_state="NEUTRAL", st_triggers=[])
    assert "no accumulation" in neutral["stance"]
    assert all("not financial advice" in w["disclaimer"].lower()
               for w in (oversold, hot, watch, neutral))


def test_trade_levels_buy_sell_and_none():
    buy = shortterm.trade_levels("BUY", 100.0, 2.0, k_stop=1.5, k_target=2.5)
    assert buy["stop"] == pytest.approx(97.0) and buy["target"] == pytest.approx(105.0)
    assert buy["rr"] == pytest.approx(2.5 / 1.5, abs=0.01)
    sell = shortterm.trade_levels("SELL", 100.0, 2.0)
    assert sell["stop"] == pytest.approx(103.0) and sell["target"] == pytest.approx(95.0)
    assert shortterm.trade_levels("BUY", 100.0, None) is None
    assert shortterm.trade_levels("BUY", None, 2.0) is None


def test_diff_since():
    prev = {"composite": 45.0, "tier": "WATCH", "subscores": {"fng": 0.9, "mvrv_z": 0.5},
            "run_ts": "2026-06-01T00:00:00+00:00"}
    cur = {"composite": 62.0, "tier": "ACCUMULATE", "subscores": {"fng": 0.9, "mvrv_z": 0.7}}
    d = alerting.diff_since(prev, cur)
    assert d["composite_delta"] == pytest.approx(17.0)
    assert d["tier_from"] == "WATCH" and d["tier_to"] == "ACCUMULATE"
    assert "MVRV Z-Score" in d["newly_in_zone"]   # 0.5 -> 0.7 crosses IN_ZONE_THRESHOLD
    assert alerting.diff_since(None, cur) is None

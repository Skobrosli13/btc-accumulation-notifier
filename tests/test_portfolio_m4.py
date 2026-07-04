"""M4 portfolio modules: sizing, limits, discipline, paper ledger."""
from __future__ import annotations

import math
import sqlite3

import pytest

from app.harness import schema
from app.portfolio import discipline, limits, paper, sizing


# --- sizing ---------------------------------------------------------------------

def test_vol_parity_hand_value():
    # target 15%, asset vol 60%, 4 concurrent: 0.15/(0.6*2) = 0.125
    assert sizing.vol_parity_weight(0.60, 4) == pytest.approx(0.125)
    assert sizing.vol_parity_weight(0.0, 4) == 0.0
    assert sizing.vol_parity_weight(0.60, 0) == 0.0


def test_quarter_kelly_hand_value():
    # mu 0.01/trade, sigma^2 0.04 -> kelly 0.25, quartered 0.0625
    assert sizing.quarter_kelly(0.01, 0.04) == pytest.approx(0.0625)
    assert sizing.quarter_kelly(-0.01, 0.04) == 0.0     # no edge -> no size
    assert sizing.quarter_kelly(0.01, 0.0) == 0.0


def test_position_size_takes_min_and_caps():
    # legs: parity 0.125, kelly 0.0625, cap 0.07 -> 0.0625
    s = sizing.position_size(asset_vol_annual=0.60, n_concurrent=4,
                             expectancy=0.01, variance=0.04)
    assert s == pytest.approx(0.0625)
    # huge kelly/parity -> the 7% cap binds; BTC caps at 15%
    big = dict(asset_vol_annual=0.10, n_concurrent=1, expectancy=0.05, variance=0.01)
    assert sizing.position_size(**big) == pytest.approx(0.07)
    assert sizing.position_size(**big, is_btc=True) == pytest.approx(0.15)


# --- limits ---------------------------------------------------------------------

def _book(n, sector="Tech"):
    return [{"ticker": f"P{i}", "sector": sector} for i in range(n)]


def test_limits_concurrent_sector_corr_btc():
    ok = limits.check_candidate(_book(3), {"ticker": "X", "sector": "Energy"},
                                mean_corr_60d=0.2)
    assert ok == []
    assert any("max_concurrent" in v for v in
               limits.check_candidate(_book(12), {"ticker": "X"}, mean_corr_60d=0.1))
    assert any("max_per_sector" in v for v in
               limits.check_candidate(_book(3), {"ticker": "X", "sector": "Tech"},
                                      mean_corr_60d=0.1))
    assert any("mean_corr" in v for v in
               limits.check_candidate(_book(2), {"ticker": "X", "sector": "E"},
                                      mean_corr_60d=0.75))
    # unpriced correlation on a non-empty book is a violation, not a pass
    assert any("correlation_unpriced" in v for v in
               limits.check_candidate(_book(2), {"ticker": "X", "sector": "E"}))
    # empty book: no corr requirement
    assert limits.check_candidate([], {"ticker": "X", "sector": "E"}) == []
    btc_book = [{"ticker": "BTC", "is_btc": True}]
    assert any("btc_single" in v for v in
               limits.check_candidate(btc_book, {"ticker": "BTC2", "is_btc": True},
                                      mean_corr_60d=0.0))


def test_drawdown_ladder():
    assert limits.drawdown_action(0.05) == "none"
    assert limits.drawdown_action(0.10) == "halve_gross"
    assert limits.drawdown_action(0.149) == "halve_gross"
    assert limits.drawdown_action(0.15) == "flat"


# --- discipline -------------------------------------------------------------------

def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    schema.init_harness_db(c)
    return c


def test_decision_requires_reason_on_deviation():
    c = _conn()
    discipline.record_decision(c, event_id=1, system_action="BUY",
                               user_action="BUY")                    # follow: ok
    with pytest.raises(ValueError):
        discipline.record_decision(c, event_id=2, system_action="BUY",
                                   user_action="SKIP")               # no reason
    discipline.record_decision(c, event_id=2, system_action="BUY",
                               user_action="SKIP", reason_code="liquidity")
    rows = c.execute("SELECT * FROM decisions ORDER BY event_id").fetchall()
    assert len(rows) == 2 and rows[1]["reason_code"] == "liquidity"
    c.close()


def test_r_gap_hand_value():
    pairs = [{"system_r": 1.0, "executed_r": 1.0},     # followed
             {"system_r": 2.0, "executed_r": 0.0}]     # skipped a 2R winner
    g = discipline.r_gap(pairs)
    assert g["n"] == 2 and g["deviated"] == 1
    assert g["gap_r"] == pytest.approx(-1.0)           # deviation cost 1R/trade
    assert discipline.r_gap([])["gap_r"] is None


# --- paper -----------------------------------------------------------------------

def test_btc_paper_fill_crosses_the_spread():
    assert paper.btc_fill_price(100_000.0, "BUY") == pytest.approx(100_050.0)
    assert paper.btc_fill_price(100_000.0, "SELL") == pytest.approx(99_950.0)
    # measured spread wider than the 5bps floor is used
    assert paper.btc_fill_price(100_000.0, "BUY", half_spread=0.001) == \
        pytest.approx(100_100.0)
    c = _conn()
    row = paper.record_btc_fill(c, event_id=None, side="buy", qty=0.1,
                                mid=100_000.0)
    assert row["fill_px"] == pytest.approx(100_050.0)
    assert row["slippage_bps"] == pytest.approx(5.0)
    assert c.execute("SELECT count(*) FROM fills").fetchone()[0] == 1
    c.close()

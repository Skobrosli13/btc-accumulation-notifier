"""portfolio_bt + BTC policy signals — hand-computed fixtures."""
from __future__ import annotations

import pytest

from app.harness import portfolio_bt as pbt
from app.policies import btc as pol


def test_max_drawdown_hand_value():
    # peak 1.2 -> trough 0.9 = 25%; later 1.5 -> 1.2 = 20%. Max = 25%.
    assert pbt.max_drawdown([1.0, 1.2, 0.9, 1.5, 1.2]) == pytest.approx(0.25)
    assert pbt.max_drawdown([1.0, 1.1, 1.2]) == 0.0
    assert pbt.max_drawdown([]) == 0.0


def test_equity_curve_causal_with_switch_cost():
    closes = [100.0, 110.0, 99.0]
    eq = pbt.equity_curve(closes, [1.0, 1.0, 0.0], switch_cost_bps=10.0)
    # t0: switch 0->1 costs 10bps, then +10%:  1 * .999 * 1.10 = 1.0989
    assert eq[1] == pytest.approx(0.999 * 1.10)
    # t1: no switch, -10%: 1.0989 * 0.9
    assert eq[2] == pytest.approx(0.999 * 1.10 * 0.9)
    # never exposed -> flat at 1.0 regardless of prices
    flat = pbt.equity_curve(closes, [0.0, 0.0, 0.0])
    assert flat == [1.0, 1.0, 1.0]


def test_dca_simulate_plain_vs_tilted_dip_buying():
    closes = [100.0, 50.0, 200.0]
    plain = pbt.dca_simulate(closes, [0, 1], budget=100.0)
    # 1 unit @100 + 2 units @50 = 3 units -> 600 final on 200 in
    assert plain["units"] == pytest.approx(3.0)
    assert plain["final_value"] == pytest.approx(600.0)
    assert plain["total_return"] == pytest.approx(2.0)

    # Tilt banks half at t0 and deploys the bank into the dip at t1:
    # 0.5 unit @100 (bank 50) + 150/50 = 3 units @50 -> 3.5 units -> 700 final.
    tilt = pbt.dca_simulate(closes, [0, 1], budget=100.0, scales=[0.5, 2.0])
    assert tilt["units"] == pytest.approx(3.5)
    assert tilt["contributed"] == plain["contributed"] == 200.0   # identical capital
    assert tilt["final_value"] == pytest.approx(700.0)

    legs = pbt.policy_vs_baseline(tilt, plain)
    assert legs["return_ok"] is True


def test_dca_spend_capped_by_cash_and_smax():
    closes = [100.0, 100.0]
    # scale 5 capped at s_max=2, and spend capped by available cash (100):
    out = pbt.dca_simulate(closes, [0], budget=100.0, scales=[5.0], s_max=2.0)
    assert out["units"] == pytest.approx(1.0)      # only 100 of cash existed
    assert out["cash"] == pytest.approx(0.0)


def test_rebalance_backtest_active_and_curves():
    # port +10%, +0%, +20%; bench +5%, +5%, +5%
    bt = pbt.rebalance_backtest([0.10, 0.0, 0.20], [0.05, 0.05, 0.05])
    assert bt["n_periods"] == 3
    assert bt["active"] == pytest.approx([0.05, -0.05, 0.15])
    assert bt["port_total"] == pytest.approx(1.10 * 1.0 * 1.20 - 1.0)
    assert bt["bench_total"] == pytest.approx(1.05 ** 3 - 1.0)
    # pairwise drop: a None in either series removes that period from both
    bt2 = pbt.rebalance_backtest([0.1, None, 0.2], [0.05, 0.05, 0.05])
    assert bt2["n_periods"] == 2 and bt2["active"] == pytest.approx([0.05, 0.15])
    assert pbt.rebalance_backtest([], [])["n_periods"] == 0


def test_trend_exposure_hysteresis():
    closes = [10.0, 10.0, 10.0, 11.0, 10.4, 9.0]
    exp = pol.trend_exposure(closes, period=3, band=0.02)
    # cold start (no MA) -> flat; cross above MA*1.02 -> long; INSIDE the band
    # -> hold; drop below MA*0.98 -> flat.
    assert exp == [0.0, 0.0, 0.0, 1.0, 1.0, 0.0]


def test_trend_exposure_no_lookahead_shape():
    closes = [10.0] * 250 + [20.0]                 # jump on the last bar
    exp = pol.trend_exposure(closes)
    assert exp[-2] == 0.0                          # nothing before the jump
    assert exp[-1] == 1.0                          # reacts AT the jump close only


def test_accum_scales_mapping():
    assert pol.accum_scales(["NEUTRAL", "WATCH", "ACCUMULATE", "DEEP_VALUE", None,
                             "garbage"]) == [0.75, 1.0, 1.5, 2.0, 1.0, 1.0]

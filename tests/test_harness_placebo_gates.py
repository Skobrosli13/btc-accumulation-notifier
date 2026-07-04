"""Placebo suite + gates — incl. the M2 acceptance check: placebo CLEAN on dummy
emitters through BOTH evaluators (if shuffled noise looks significant, the
machinery is broken)."""
from __future__ import annotations

import random

import pytest

from app.harness import car, gates, placebo, ts_study

DAY = 86_400_000


# --- suite mechanics -----------------------------------------------------------

def test_suite_clean_and_dirty_thresholds():
    # bare floats are judged at infinite dof (crit 1.96) — the strictest bar
    clean = placebo.suite(lambda rnd: rnd.gauss(0, 0.5), n=50, seed=1)
    assert clean["n_valid"] == 50 and clean["clean"] is True
    assert clean["exceed_frac"] <= 0.15
    dirty = placebo.suite(lambda rnd: rnd.gauss(3.0, 0.1), n=50, seed=1)
    assert dirty["clean"] is False and dirty["exceed_frac"] > 0.9
    # mostly-undefined t proves nothing -> not clean
    sparse = placebo.suite(lambda rnd: None, n=50, seed=1)
    assert sparse["clean"] is False and sparse["n_valid"] == 0


def test_t_crit_95_table_and_interpolation():
    assert placebo.t_crit_95(5) == pytest.approx(2.571)
    assert placebo.t_crit_95(1000) == pytest.approx(1.96)
    # interpolated between dof 10 (2.228) and 12 (2.179)
    assert 2.179 < placebo.t_crit_95(11) < 2.228
    # a correct null at 5 months (dof 4) must NOT be judged against 2.0:
    # its own 95% critical value is 2.776.
    assert placebo.t_crit_95(4) == pytest.approx(2.776)


def test_shuffle_dates_preserves_per_ticker_counts():
    events = [{"ticker": "A", "event_ts": 1}, {"ticker": "A", "event_ts": 2},
              {"ticker": "B", "event_ts": 3}]
    sh = placebo.shuffle_dates_per_ticker(events, random.Random(0))
    assert sorted(e["event_ts"] for e in sh) == [1, 2, 3]     # same date multiset
    assert [e["ticker"] for e in sh] == ["A", "A", "B"]        # tickers untouched


def test_redraw_within_regimes_preserves_counts():
    ts_ms = [i * DAY for i in range(100)]
    pools = {"bull": list(range(0, 50)), "bear": list(range(50, 100))}
    out = placebo.redraw_within_regimes(["bull", "bull", "bear"], pools,
                                        ts_ms, random.Random(0))
    assert len(out) == 3
    assert sum(1 for t in out if t < 50 * DAY) == 2            # 2 bull draws
    assert sum(1 for t in out if t >= 50 * DAY) == 1           # 1 bear draw


# --- M2 acceptance: placebo clean on dummy emitters (both evaluators) -----------

def _noise_bars(seed: int, n_days: int = 260) -> list[dict]:
    rnd = random.Random(seed)
    out, close = [], 100.0
    for k in range(n_days):
        opn = close
        close = opn * (1.0 + rnd.gauss(0, 0.02))
        out.append({"ts": k * DAY, "open": opn, "high": max(opn, close),
                    "low": min(opn, close), "close": close, "volume": 1.0})
    return out


def test_placebo_clean_on_dummy_car_emitter():
    tickers = [f"T{j}" for j in range(15)]
    bars = {tk: _noise_bars(j) for j, tk in enumerate(tickers)}
    rnd0 = random.Random(99)
    # dummy emitter: 12 information-free events on random names/dates
    events = [{"ticker": rnd0.choice(tickers), "event_ts": rnd0.randrange(30, 180) * DAY,
               "direction": "LONG", "tier": "small", "sector": "Tech",
               "days_since_earnings": 5} for _ in range(12)]
    cands = [[{"ticker": tk, "tier": "small", "sector": "Tech", "days_since_earnings": 5}
              for tk in tickers] for _ in events]

    def eval_t(rnd):
        sh = placebo.shuffle_dates_per_ticker(events, rnd)
        h5 = car.evaluate(sh, bars, cands, horizons=(5,))["horizons"][5]
        return (h5["t_clustered"], h5["n_months"])

    result = placebo.suite(eval_t, n=50, seed=7)
    assert result["clean"] is True, f"CAR placebo dirty: {result}"


def test_placebo_clean_on_dummy_ts_emitter():
    rnd0 = random.Random(5)
    rets = [rnd0.gauss(0, 0.01) for _ in range(799)]
    closes, ts_ms = [100.0], [0]
    for k, r in enumerate(rets):
        closes.append(closes[-1] * (1 + r))
        ts_ms.append((k + 1) * DAY)
    pool = list(range(210, 760))       # bars with regime + forward coverage

    def eval_t(rnd):
        ev = sorted(ts_ms[i] for i in rnd.sample(pool, 8))
        a = ts_study.evaluate(closes, ts_ms, ev, h_days=10, n_resamples=1,
                              seed=1)["all"]
        return (a["t_clustered"], a["n_months"])

    result = placebo.suite(eval_t, n=50, seed=11)
    assert result["clean"] is True, f"ts placebo dirty: {result}"


# --- gates ----------------------------------------------------------------------

_OK = dict(t_clustered=3.5, n_months=14, n_events=120, exp_after_tax=0.004,
           sign_consistent=True, placebo_clean=True)


def test_alpha_gate_promotes_and_hard_kills():
    assert gates.alpha_verdict(**_OK)["status"] == "PROMOTED"
    for bad in (dict(exp_after_tax=-0.001), dict(placebo_clean=False),
                dict(sign_consistent=False)):
        v = gates.alpha_verdict(**{**_OK, **bad})
        assert v["status"] == "KILLED" and v["reasons"]


def test_alpha_gate_soft_miss_extends_once():
    soft = {**_OK, "t_clustered": 2.4}
    assert gates.alpha_verdict(**soft)["status"] == "EXTEND"
    assert gates.alpha_verdict(**soft, already_extended=True)["status"] == "KILLED"
    # BTC ts-study event floor
    btc = {**_OK, "n_events": 45}
    assert gates.alpha_verdict(**btc)["status"] == "EXTEND"          # default floor 100
    assert gates.alpha_verdict(**btc, min_events=gates.ALPHA_MIN_EVENTS_BTC_TS
                               )["status"] == "PROMOTED"             # ts floor 40


def test_policy_gate_requires_both_legs():
    ok = gates.policy_verdict(overlay_return=0.5, baseline_return=0.4,
                              overlay_maxdd=0.30, baseline_maxdd=0.45)
    assert ok["status"] == "PROMOTED"
    worse_dd = gates.policy_verdict(overlay_return=0.5, baseline_return=0.4,
                                    overlay_maxdd=0.50, baseline_maxdd=0.45)
    assert worse_dd["status"] == "WATCHLIST"
    fwd_fail = gates.policy_verdict(overlay_return=0.5, baseline_return=0.4,
                                    overlay_maxdd=0.30, baseline_maxdd=0.45,
                                    forward_overlay_return=0.01,
                                    forward_baseline_return=0.05,
                                    forward_overlay_maxdd=0.10,
                                    forward_baseline_maxdd=0.20)
    assert fwd_fail["status"] == "WATCHLIST"


def test_policy_gate_forward_dd_checked_independently():
    """Forward drawdowns WITHOUT forward returns must still be checked (the
    nested version silently skipped this leg), and a missing forward baseline is
    never fabricated as 0.0."""
    v = gates.policy_verdict(overlay_return=0.5, baseline_return=0.4,
                             overlay_maxdd=0.30, baseline_maxdd=0.45,
                             forward_overlay_maxdd=0.50, forward_baseline_maxdd=0.10)
    assert v["status"] == "WATCHLIST"                 # 5x-worse forward DD caught
    # forward return supplied WITHOUT a baseline: no fabricated comparison
    v2 = gates.policy_verdict(overlay_return=0.5, baseline_return=0.4,
                              overlay_maxdd=0.30, baseline_maxdd=0.45,
                              forward_overlay_return=-0.01)
    assert v2["status"] == "PROMOTED" and v2["reasons"] == []


def test_premium_gate():
    ok = gates.premium_verdict(net_annualized_carry=0.07, tbill_rate=0.04,
                               forced_liquidations=0, min_margin_ratio=2.5)
    assert ok["status"] == "PROMOTED"
    thin = gates.premium_verdict(net_annualized_carry=0.05, tbill_rate=0.04,
                                 forced_liquidations=0, min_margin_ratio=2.5)
    assert thin["status"] == "KILLED"                  # < tbill + 2pp
    liq = gates.premium_verdict(net_annualized_carry=0.09, tbill_rate=0.04,
                                forced_liquidations=1, min_margin_ratio=2.5)
    assert liq["status"] == "KILLED"


def test_lt_factor_gate_needs_both_benchmarks():
    ok = gates.lt_factor_verdict(t_vs_universe=2.5, t_vs_etf=2.1, n_months=40)
    assert ok["status"] == "PROMOTED"
    one = gates.lt_factor_verdict(t_vs_universe=2.5, t_vs_etf=1.5, n_months=40)
    assert one["status"] == "WATCHLIST"

"""CAR evaluator — the 5-event hand fixture (M2 acceptance) + matching rules.

Bar construction: session k has ts = k*DAY_MS, open = previous close, so a name
with daily ratio g has EXACTLY r = g-1 every session (day 1 is close/open-1 at
the next session's open — the entry convention). Controls are flat (r = 0), so
each event's CAR_h is just h * its own daily return, hand-computable.
"""
from __future__ import annotations

import math
import statistics

import pytest

from app.harness import car

DAY = 86_400_000


def _bars(event_day: int, ratio: float, n_days: int = 40, p0: float = 100.0) -> list[dict]:
    """Flat at p0 through event_day, then multiplies by ``ratio`` each session."""
    out, close = [], p0
    for k in range(n_days):
        opn = close
        if k > event_day:
            close = opn * ratio
        out.append({"ts": k * DAY, "open": opn, "high": max(opn, close),
                    "low": min(opn, close), "close": close, "volume": 1.0})
    return out


def _flat(n_days: int = 40) -> list[dict]:
    return _bars(10, 1.0, n_days)


def _event(ticker, day, direction="LONG", month_day=None):
    return {"ticker": ticker, "permaticker": ticker, "event_ts": day * DAY,
            "direction": direction, "tier": "small", "sector": "Tech",
            "days_since_earnings": 5}


def _candidates(n=12):
    return [{"ticker": f"C{j}", "tier": "small", "sector": "Tech",
             "days_since_earnings": 5} for j in range(n)]


# --- the 5-event hand fixture ---------------------------------------------------
# Jan events E1,E2: +1%/day -> CAR_5 = 0.05 each.
# Feb events E3,E4: +2%/day -> CAR_5 = 0.10 each.
# Mar event  E5 (SHORT): -3%/day -> raw -0.15, signed CAR_5 = +0.15.
# Winsorize [.05,.05,.10,.10,.15] at 1/99 (n=5, linear interp):
#   lo = pct(.01) at pos .04 -> .05 ; hi = pct(.99) at pos 3.96 -> .10*.04+.15*.96 = .148
#   -> [.05,.05,.10,.10,.148]
# Months (31-day spacing puts each pair in its own calendar month):
#   monthly means [.05, .10, .148]; t = mean / (stdev/sqrt(3)).

def _fixture():
    events = [_event("E1", 5), _event("E2", 8),           # Jan (epoch days 5-8)
              _event("E3", 36), _event("E4", 39),          # Feb
              _event("E5", 65, direction="SHORT")]         # Mar
    bars = {"E1": _bars(5, 1.01, 80), "E2": _bars(8, 1.01, 80),
            "E3": _bars(36, 1.02, 80), "E4": _bars(39, 1.02, 80),
            "E5": _bars(65, 0.97, 80)}
    for j in range(12):
        bars[f"C{j}"] = _flat(80)
    cands = [_candidates() for _ in events]
    return events, bars, cands


def test_car_five_event_hand_fixture():
    events, bars, cands = _fixture()
    out = car.evaluate(events, bars, cands, horizons=(5,), k=25)
    h5 = out["horizons"][5]
    assert h5["n_events"] == 5 and h5["n_months"] == 3
    assert h5["win_rate"] == 1.0
    # winsorized CARs, exact: [.05,.05,.10,.10,.148]
    wins = sorted(w for _i, w in out["cars"][5])
    assert wins == pytest.approx([0.05, 0.05, 0.10, 0.10, 0.148])
    assert h5["mean_car"] == pytest.approx(0.448 / 5)
    mm = [0.05, 0.10, 0.148]
    expected_t = (sum(mm) / 3) / (statistics.stdev(mm) / math.sqrt(3))
    assert h5["t_clustered"] == pytest.approx(expected_t)
    # every event priced at h=5
    assert out["coverage"]["n_priced_by_horizon"][5] == 5


def test_car_short_event_sign_flip():
    events, bars, cands = _fixture()
    out = car.evaluate(events, bars, cands, horizons=(5,))
    e5_car = dict(out["cars"][5])[4]
    assert e5_car > 0                      # -3%/day fall scored as a WIN for a SHORT
    # and a LONG event on the same falling path would be negative
    ev_long = [_event("E5", 65, direction="LONG")]
    out2 = car.evaluate(ev_long, bars, [_candidates()], horizons=(5,))
    assert dict(out2["cars"][5])[0] < 0


def test_insufficient_forward_bars_skips_event():
    events, bars, cands = _fixture()
    out = car.evaluate(events, bars, cands, horizons=(63,))   # only ~75 bars total
    # E5 at day 65 has ~14 post-event sessions -> unpriceable at h=63.
    assert out["coverage"]["n_priced_by_horizon"][63] < 5


# --- matching rules --------------------------------------------------------------

def test_match_controls_relaxation_ladder():
    ev = _event("E1", 5)
    # 8 perfect matches (sector+bucket) — BELOW min_cohort=10 — but 12 more that
    # match the bucket only: the sector constraint must drop, giving 20.
    perfect = [{"ticker": f"P{j}", "tier": "small", "sector": "Tech",
                "days_since_earnings": 5} for j in range(8)]
    bucket_only = [{"ticker": f"B{j}", "tier": "small", "sector": "Energy",
                    "days_since_earnings": 7} for j in range(12)]
    got = car.match_controls(ev, perfect + bucket_only, k=25)
    assert len(got) == 20                          # sector dropped, bucket held
    # tier NEVER relaxes: wrong-tier candidates are unusable even when thin.
    wrong_tier = [{"ticker": f"W{j}", "tier": "large", "sector": "Tech",
                   "days_since_earnings": 5} for j in range(30)]
    got2 = car.match_controls(ev, perfect + wrong_tier, k=25)
    assert {c["ticker"] for c in got2} == {f"P{j}" for j in range(8)}


def test_match_controls_excludes_contaminated_and_self():
    ev = _event("E2", 8)
    cands = _candidates(10) + [
        {"ticker": "E1", "tier": "small", "sector": "Tech", "days_since_earnings": 5},
        {"ticker": "E2", "tier": "small", "sector": "Tech", "days_since_earnings": 5},
    ]
    # E1 carries a same-study event 3 days earlier -> contaminated within ±30d.
    got = car.match_controls(ev, cands, study_event_ts_by_ticker={
        "E1": [5 * DAY], "E2": [8 * DAY]})
    names = {c["ticker"] for c in got}
    assert "E1" not in names and "E2" not in names and len(names) == 10


def test_earnings_bucket_edges():
    b = car.earnings_bucket
    assert b(0) == "0-10" and b(10) == "0-10"
    assert b(11) == "11-30" and b(30) == "11-30"
    assert b(31) == "31+" and b(None) == ""


def test_collision_report():
    events = [_event("E1", 5), _event("E2", 40)]
    other = {"insider_cluster": [
        {"permaticker": "E1", "event_ts": 8 * DAY},     # 3d from E1 -> collision
        {"permaticker": "E2", "event_ts": 60 * DAY},    # 20d from E2 -> clear
    ]}
    rep = car.collision_report(events, other)
    assert rep["collided_indices"] == [0]
    assert rep["pct"] == pytest.approx(0.5)
    assert rep["by_study"] == {"insider_cluster": 1}

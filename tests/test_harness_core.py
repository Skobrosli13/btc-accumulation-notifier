"""Harness support modules: schema round-trip, walk-forward segmenting, costs, tax."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from app.harness import costs, schema, tax, walkforward


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    schema.init_harness_db(c)
    return c


# --- schema -------------------------------------------------------------------

def test_events_insert_or_ignore_dedupes():
    c = _conn()
    ev = {"study": "sue_pead", "asset": "EQ", "permaticker": "199059",
          "ticker": "AAPL", "event_ts": 1000, "direction": "LONG",
          "strength": 1.5, "tier": "large", "sector": "Tech",
          "days_since_earnings": 0, "meta": {"sue": 1.5}}
    assert schema.insert_events(c, [ev]) == 1
    assert schema.insert_events(c, [ev]) == 0            # idempotent re-emit
    rows = schema.events_for_study(c, "sue_pead")
    assert len(rows) == 1 and rows[0]["meta"]["sue"] == 1.5
    c.close()


def test_register_study_and_results_round_trip():
    c = _conn()
    schema.register_study(c, name="sue_pead", asset="EQ", evaluator="car",
                          tier="alpha", spec_path="studies/sue_pead.md",
                          registered_at=_ms("2026-07-04"), primary_horizon=21)
    s = schema.get_study(c, "sue_pead")
    assert s["status"] == "REGISTERED" and s["evaluator"] == "car"
    # duplicate registration must raise (re-register = new name, old rows freeze)
    with pytest.raises(sqlite3.IntegrityError):
        schema.register_study(c, name="sue_pead", asset="EQ", evaluator="car",
                              tier="alpha", spec_path="x",
                              registered_at=0, primary_horizon=21)
    schema.record_results(c, [{"study": "sue_pead", "segment": "OOS", "horizon": 21,
                               "n_events": 120, "n_months": 14, "mean_car": 0.011,
                               "t_clustered": 3.2, "win_rate": 0.56,
                               "exp_gross": 0.011, "exp_net": 0.009,
                               "exp_after_tax": 0.0054, "emitter_sha": "abc123",
                               "params_hash": "p1", "computed_at": 1}])
    # recompute supersedes (same study/segment/horizon/tier)
    schema.record_results(c, [{"study": "sue_pead", "segment": "OOS", "horizon": 21,
                               "n_events": 121, "t_clustered": 3.1}])
    rows = schema.results_for_study(c, "sue_pead", "OOS")
    assert len(rows) == 1 and rows[0]["n_events"] == 121
    schema.set_study_status(c, "sue_pead", "RUNNING")
    assert schema.get_study(c, "sue_pead")["status"] == "RUNNING"
    c.close()


def test_schema_rejects_bad_enums():
    c = _conn()
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO studies (name, evaluator) VALUES ('x', 'vibes')")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO events (study, asset, event_ts) VALUES ('s', 'DOGE', 1)")
    c.close()


# --- walk-forward -------------------------------------------------------------

def test_segments_and_embargo():
    reg = _ms("2026-03-01")
    f = lambda iso: walkforward.segment_of(_ms(iso), reg)
    assert f("2020-06-15") == "IS"
    assert f("2023-06-15") == "OOS"
    assert f("2026-06-15") == "LIVE"
    # embargo brackets both boundaries (30 calendar days each side)
    assert f("2021-12-20") is None       # near IS end
    assert f("2022-01-15") is None       # just after IS end
    assert f("2026-02-10") is None       # near registration
    assert f("2026-03-20") is None       # just after registration
    assert f("2022-02-15") == "OOS"      # clear of the embargo


def test_split_events_reports_embargoed():
    reg = _ms("2026-03-01")
    events = [{"event_ts": _ms("2020-06-15")}, {"event_ts": _ms("2022-01-15")},
              {"event_ts": _ms("2023-06-15")}, {"event_ts": _ms("2026-06-15")}]
    out = walkforward.split_events(events, reg)
    assert [len(out[k]) for k in ("IS", "OOS", "LIVE", "EMBARGOED")] == [1, 1, 1, 1]


# --- costs / tax ---------------------------------------------------------------

def test_costs_by_tier_and_pessimistic_default():
    assert costs.round_trip_bps("large") == 10.0
    assert costs.round_trip_bps("btc") == 10.0
    assert costs.round_trip_bps("micro") == 80.0
    assert costs.round_trip_bps(None) == 80.0            # unknown -> worst tier
    assert costs.round_trip_bps("mid", {"mid": 25.0}) == 25.0
    # 1% gross in small tier: 0.01 - 40bps = 0.006
    assert costs.net_return(0.01, "small") == pytest.approx(0.006)


def test_after_tax_rates():
    assert tax.after_tax(0.01) == pytest.approx(0.006)                 # ST 40%
    assert tax.after_tax(0.01, long_term=True) == pytest.approx(0.0076)  # LT 24%
    # §1256 blended: 0.6*0.24 + 0.4*0.40 = 0.304 -> 0.01*0.696
    assert tax.after_tax(0.01, section_1256=True) == pytest.approx(0.00696)
    assert tax.after_tax(-0.01) == pytest.approx(-0.006)               # loss credit


def test_expectancy_triplet_hand_value():
    # two events, +2% and -1% gross, mid tier (20bps RT):
    # nets: 0.018, -0.012 -> mean 0.003; after-tax (ST 40%): 0.0108, -0.0072 -> 0.0018
    out = tax.expectancy_triplet([0.02, -0.01], "mid")
    assert out["exp_gross"] == pytest.approx(0.005)
    assert out["exp_net"] == pytest.approx(0.003)
    assert out["exp_after_tax"] == pytest.approx(0.0018)
    assert tax.expectancy_triplet([], "mid")["exp_gross"] is None

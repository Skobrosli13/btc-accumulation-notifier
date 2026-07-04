"""study CLI end-to-end (M2 acceptance): register -> emit -> run -> placebo -> report
against a temp DB with synthetic candles + a synthetic injected-effect study."""
from __future__ import annotations

import random

import pytest

from app import store
from app.harness import schema
from scripts import study as cli

DAY = 86_400_000


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Temp DB with ~900 synthetic 1d candles + config pointed at it."""
    db = str(tmp_path / "study.db")
    conn = store.connect(db)
    store.init_db(conn)
    schema.init_harness_db(conn)
    rnd = random.Random(3)
    close, rows = 100.0, []
    for k in range(900):
        opn = close
        close = opn * (1 + rnd.gauss(0, 0.01))
        rows.append((k * DAY, opn, max(opn, close), min(opn, close), close, 10.0))
    store.upsert_candles(conn, "1d", rows)
    conn.close()

    from tests.factories import make_config
    cfg = make_config(db_path=db)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    return db


def _seed_events(db, ts_list, study="btc_dummy"):
    conn = store.connect(db)
    schema.init_harness_db(conn)
    schema.insert_events(conn, [
        {"study": study, "asset": "BTC", "ticker": "BTC", "permaticker": "BTC",
         "event_ts": ts, "direction": "LONG"} for ts in ts_list])
    conn.close()


def test_cli_end_to_end_ts(env, tmp_path, capsys):
    spec = tmp_path / "btc_dummy.md"
    spec.write_text("# dummy pre-registration")
    # register (spec required)
    cli.main(["register", "--name", "btc_dummy", "--asset", "BTC",
              "--evaluator", "ts", "--tier", "alpha", "--horizon", "10",
              "--spec", str(spec)])
    # registration without a spec must refuse
    with pytest.raises(SystemExit):
        cli.main(["register", "--name", "x", "--asset", "BTC", "--evaluator",
                  "ts", "--tier", "alpha", "--horizon", "10",
                  "--spec", str(tmp_path / "missing.md")])

    # events across IS (2021-) years... candles start at epoch, so use early ts.
    _seed_events(env, [d * DAY for d in (300, 400, 500, 600, 700)])
    cli.main(["run", "--name", "btc_dummy", "--resamples", "30"])
    cli.main(["placebo", "--name", "btc_dummy", "--shuffles", "12"])
    cli.main(["report"])
    out = capsys.readouterr().out
    assert "btc_dummy" in out and "placebo" in out.lower()

    conn = store.connect(env)
    rows = schema.results_for_study(conn, "btc_dummy")
    segments = {r["segment"] for r in rows}
    assert "IS" in segments            # epoch-1970 events are all deep IS
    assert "PLACEBO" in segments
    r0 = [r for r in rows if r["segment"] == "IS"][0]
    assert r0["emitter_sha"] and r0["params_hash"]        # anti-drift stamps
    assert schema.get_study(conn, "btc_dummy")["status"] == "RUNNING"
    conn.close()


def test_verdict_single_era_population_is_not_a_sign_failure(env, capsys):
    """A study whose whole population is OOS (no IS rows — e.g. clone13f, where
    complete SF3 books only exist 2022+) must not hard-KILL on 'sign
    inconsistency': the two-population split is NOT APPLICABLE (§5.5 'where
    applicable'). With a soft t-miss it EXTENDs instead."""
    conn = store.connect(env)
    schema.init_harness_db(conn)
    schema.register_study(conn, name="oos_only", asset="EQ", evaluator="car",
                          tier="alpha", spec_path="x", registered_at=1,
                          primary_horizon=63)
    schema.record_results(conn, [
        {"study": "oos_only", "segment": "OOS", "horizon": 63, "n_events": 1467,
         "n_months": 15, "mean_car": 0.01, "t_clustered": 1.58,
         "exp_after_tax": 0.0073},
        {"study": "oos_only", "segment": "PLACEBO", "horizon": 63,
         "n_events": 50, "extra": {"clean": True}}])
    conn.close()
    cli.main(["verdict", "--name", "oos_only"])
    out = capsys.readouterr().out
    assert "EXTEND" in out and "sign not consistent" not in out
    conn = store.connect(env)
    assert schema.get_study(conn, "oos_only")["status"] == "EXTEND"
    conn.close()


def test_cli_run_requires_registration_and_events(env, capsys):
    with pytest.raises(SystemExit):
        cli.main(["run", "--name", "ghost"])
    conn = store.connect(env)
    schema.init_harness_db(conn)
    schema.register_study(conn, name="empty", asset="BTC", evaluator="ts",
                          tier="alpha", spec_path="x", registered_at=1,
                          primary_horizon=10)
    conn.close()
    with pytest.raises(SystemExit):
        cli.main(["run", "--name", "empty"])

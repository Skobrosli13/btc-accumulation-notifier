"""Study CLI — register | run | placebo | report (§5.6).

The ONLY writer of study_results (machine-written; never hand-edited). Every
run stamps the git SHA + a params hash so a post-registration change to an
event definition is visible as a different fingerprint — the anti-drift rule:
any such change re-registers as `<study>-v2` and the old rows freeze.

    python -m scripts.study register --name btc_funding_extreme --asset BTC \
        --evaluator ts --tier alpha --horizon 10 --spec studies/btc_funding_extreme.md
    python -m scripts.study run --name btc_funding_extreme
    python -m scripts.study placebo --name btc_funding_extreme
    python -m scripts.study report

Evaluator wiring:
  * ts  — daily candles from the app DB (1d kept forever), events from `events`
  * car — PIT universe snapshots + SEP bars from the Parquet lake
Both paths: walk-forward split by the study's registration instant; per-segment
results (gross/net/after-tax via harness.costs/tax) written per horizon.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store                                        # noqa: E402
from app.config import load_config                           # noqa: E402
from app.harness import (car, gates, placebo, schema, stats,  # noqa: E402
                         tax, ts_study, walkforward)

HORIZONS_CAR = car.HORIZONS          # sessions
HORIZONS_TS = (5, 10, 21, 63)        # days


def _now_ms() -> int:
    return int(time.time() * 1000)


def emitter_sha() -> str:
    """Short git SHA of the working tree (fail-soft: 'unknown')."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10,
                             cwd=str(Path(__file__).resolve().parents[1]))
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def params_hash(params: dict) -> str:
    return hashlib.sha256(
        json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _conn(cfg):
    conn = store.connect(cfg.db_path)
    store.init_db(conn)
    schema.init_harness_db(conn)
    return conn


# --- run: ts evaluator ---------------------------------------------------------

def _btc_daily(conn) -> tuple[list[float], list[int]]:
    rows = store.candles_since(conn, "1d")
    rows = rows[:-1] if len(rows) > 1 else rows      # drop the forming candle
    return [r["close"] for r in rows], [r["ts"] for r in rows]


def _run_ts(conn, study: dict, events: list[dict], *, n_resamples: int) -> list[dict]:
    closes, ts_ms = _btc_daily(conn)
    if len(closes) < 250:
        raise SystemExit("not enough stored 1d candles to evaluate")
    split = walkforward.split_events(events, study["registered_at"])
    sha, now = emitter_sha(), _now_ms()
    rows = []
    for segment in ("IS", "OOS", "LIVE"):
        evs = split[segment]
        if not evs:
            continue
        for h in HORIZONS_TS:
            p = {"h_days": h, "n_resamples": n_resamples,
                 "mean_block": ts_study.MEAN_BLOCK_DAYS}
            out = ts_study.evaluate(
                closes, ts_ms, [e["event_ts"] for e in evs], h_days=h,
                # score each event by its OWN claimed direction (contrarian
                # studies fire both ways; a uniform sign would mis-score them)
                directions=[e.get("direction") or "LONG" for e in evs],
                n_resamples=n_resamples,
                split_ts_ms=ts_study.SPLIT_2024_MS)
            a = out["all"]
            if a["observed_mean"] is None:
                continue
            trip = tax.expectancy_triplet(
                [a["observed_mean"]], "btc") if a["observed_mean"] is not None else {}
            rows.append({"study": study["name"], "segment": segment, "horizon": h,
                         "n_events": a["n_events"], "n_months": a["n_months"],
                         "mean_car": a["observed_mean"], "t_clustered": a["t_clustered"],
                         "win_rate": a["win_rate"],
                         "exp_gross": a["observed_mean"],
                         "exp_net": trip.get("exp_net"),
                         "exp_after_tax": trip.get("exp_after_tax"),
                         "emitter_sha": sha, "params_hash": params_hash(p),
                         "computed_at": now})
    return rows


# --- run: car evaluator ---------------------------------------------------------

def _run_car(conn, study: dict, events: list[dict]) -> list[dict]:
    from datetime import datetime, timezone

    from app.data.equities import prices as eq_prices
    from app.data.equities import universe as eq_universe
    from app.data_lake import Lake

    cfg = load_config()
    lake = Lake(cfg.data_lake_path)
    # One PIT snapshot per distinct event date (candidates for control matching).
    def _date_iso(ts): return datetime.fromtimestamp(
        ts / 1000, tz=timezone.utc).date().isoformat()
    snapshots: dict[str, list[dict]] = {}
    for ev in events:
        d = _date_iso(ev["event_ts"])
        if d not in snapshots:
            snapshots[d] = [r for r in eq_universe.build_from_lake(lake, d)
                            if r["included"]]
    cand_tickers = {r["ticker"] for rows in snapshots.values() for r in rows}
    cand_tickers |= {e["ticker"] for e in events if e.get("ticker")}
    # Full history per name: a most-recent-N window would truncate BEFORE old
    # (IS-segment) events, and the evaluator's adjacency guard would then skip
    # them all. SEP holds ~2.6k sessions/name — 5000 covers everything.
    bars = eq_prices.sep_bars_bulk(lake, sorted(cand_tickers), limit=5000)

    cands_by_event = [snapshots[_date_iso(ev["event_ts"])] for ev in events]
    split = walkforward.split_events(events, study["registered_at"])
    sha, now = emitter_sha(), _now_ms()
    rows = []
    for segment in ("IS", "OOS", "LIVE"):
        evs = split[segment]
        if not evs:
            continue
        idx = [events.index(e) for e in evs]
        out = car.evaluate(evs, bars, [cands_by_event[i] for i in idx])
        p = {"horizons": HORIZONS_CAR, "k": car.K_CONTROLS,
             "min_cohort": car.MIN_COHORT}
        for h, hstats in out["horizons"].items():
            if hstats["mean_car"] is None:
                continue
            cars_h = [c for _i, c in out["cars"][h]]
            # per-event tiers vary; use the modal tier for the cost model
            tiers = [e.get("tier") for e in evs if e.get("tier")]
            tier = max(set(tiers), key=tiers.count) if tiers else None
            trip = tax.expectancy_triplet(cars_h, tier)
            rows.append({"study": study["name"], "segment": segment, "horizon": h,
                         "tier": tier or "", "n_events": hstats["n_events"],
                         "n_months": hstats["n_months"],
                         "mean_car": hstats["mean_car"],
                         "t_clustered": hstats["t_clustered"],
                         "win_rate": hstats["win_rate"],
                         "exp_gross": trip["exp_gross"], "exp_net": trip["exp_net"],
                         "exp_after_tax": trip["exp_after_tax"],
                         "emitter_sha": sha, "params_hash": params_hash(p),
                         "computed_at": now})
    return rows


# --- run: portfolio evaluator (BTC policies) ---------------------------------------

def _btc_daily_lake() -> tuple[list[str], list[float], list[int]]:
    """(dates_iso, closes, ts_ms) from the lake's deep btc_daily archive."""
    from datetime import datetime, timezone

    from app.data_lake import Lake
    lake = Lake(load_config().data_lake_path)
    df = lake.read("btc_daily")
    if df.empty:
        raise SystemExit("lake btc_daily missing — run: python -m scripts.ingest_btc")
    df = df.sort_values("date").reset_index(drop=True)
    dates = [str(d)[:10] for d in df["date"]]
    closes = [float(c) for c in df["close"]]
    ts = [int(datetime.strptime(d, "%Y-%m-%d").replace(
        tzinfo=timezone.utc).timestamp() * 1000) for d in dates]
    return dates, closes, ts


def _policy_row(study: str, segment: str, legs: dict, n_days: int,
                extra: dict | None = None) -> dict:
    return {"study": study, "segment": segment, "horizon": 0,
            "n_events": n_days,
            "mean_car": legs["overlay_return"] - legs["baseline_return"],
            "exp_gross": legs["overlay_return"],
            "emitter_sha": emitter_sha(), "computed_at": _now_ms(),
            "extra": {**legs, **(extra or {})}}


def _run_trend_policy(conn, study: dict) -> list[dict]:
    from app.harness import portfolio_bt as pbt
    from app.policies import btc as pol

    _dates, closes, ts = _btc_daily_lake()
    exposure = pol.trend_exposure(closes)      # causal over the FULL series

    def window(lo_ts: int | None, hi_ts: int) -> tuple[int, int]:
        # default len(ts), NOT 0: an empty window (e.g. LIVE right after
        # registration) must be empty, not silently wrap to the whole series.
        a = next((i for i, t in enumerate(ts) if lo_ts is None or t > lo_ts), len(ts))
        b = next((i for i, t in enumerate(ts) if t > hi_ts), len(ts))
        return a, max(a, b)      # clamp: a degenerate window is empty, never inverted

    reg = study["registered_at"]
    segments = {"IS": window(None, walkforward.IS_END_MS),
                "OOS": window(walkforward.IS_END_MS, reg),
                "BACKTEST": window(None, reg),
                "LIVE": window(reg, ts[-1] + 1)}
    rows, legs_by_segment = [], {}
    for seg, (a, b) in segments.items():
        if b - a < 60:
            continue
        ov = pbt.equity_curve(closes[a:b], exposure[a:b], switch_cost_bps=10.0)
        bh = pbt.equity_curve(closes[a:b], [1.0] * (b - a), switch_cost_bps=10.0)
        legs = pbt.policy_vs_baseline(
            {"total_return": ov[-1] - 1.0, "max_drawdown": pbt.max_drawdown(ov)},
            {"total_return": bh[-1] - 1.0, "max_drawdown": pbt.max_drawdown(bh)})
        legs_by_segment[seg] = legs
        rows.append(_policy_row(study["name"], seg, legs, b - a))

    bt = legs_by_segment.get("BACKTEST")
    live = legs_by_segment.get("LIVE") or {}
    verdict = gates.policy_verdict(
        overlay_return=bt["overlay_return"], baseline_return=bt["baseline_return"],
        overlay_maxdd=bt["overlay_maxdd"], baseline_maxdd=bt["baseline_maxdd"],
        forward_overlay_return=live.get("overlay_return"),
        forward_baseline_return=live.get("baseline_return"),
        forward_overlay_maxdd=live.get("overlay_maxdd"),
        forward_baseline_maxdd=live.get("baseline_maxdd"))
    schema.set_study_status(conn, study["name"], verdict["status"],
                            verdict_at=_now_ms())
    print(f"{study['name']}: {verdict['status']}"
          + (f" — {'; '.join(verdict['reasons'])}" if verdict["reasons"] else ""))
    return rows


def _accum_tier_series(cfg) -> tuple[list[str], list[float], list[str]]:
    """No-look-ahead daily (dates, closes, tiers) via the multi-cycle panel
    (network: Coinbase + FRED + BGeometrics statics). Mirrors
    calibrate._track_record's expanding-percentile loop exactly."""
    import numpy as np

    from app import scoring
    from scripts import backtest_longterm, calibrate

    px, native = backtest_longterm._panel(cfg)
    inds = [k for k in backtest_longterm.BACKBONE + backtest_longterm.ONCHAIN
            if k in px.columns]
    seeds = calibrate._seed_history(px, inds, native=native)
    rows = px.dropna(subset=["price_to_wma200"]).reset_index(drop=True)
    hist = {k: list(seeds.get(k, [])) for k in inds}
    dates, closes, tiers = [], [], []
    for _, r in rows.iterrows():
        sub: dict[str, float] = {}
        for name in inds:
            v = r.get(name)
            if v is None or not np.isfinite(v):
                continue
            hist[name].append(float(v))
            sub[name] = scoring.rank_score(hist[name], float(v),
                                           scoring.DIRECTION[name])
        cats = scoring.category_scores(sub)
        comp, _ = scoring.composite(cats, cfg.weights, 1.0)
        wma = r["close"] / r["price_to_wma200"] if r["price_to_wma200"] else None
        tiers.append(scoring.tier(comp, r["close"], wma, cfg.tier_watch,
                                  cfg.tier_accumulate, cfg.tier_deepvalue))
        dates.append(str(r["date"])[:10])
        closes.append(float(r["close"]))
    return dates, closes, tiers


def _run_accum_policy(conn, study: dict) -> list[dict]:
    from app.harness import portfolio_bt as pbt
    from app.policies import btc as pol

    cfg = load_config()
    try:
        dates, closes, tiers = _accum_tier_series(cfg)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - network panel is genuinely optional
        raise SystemExit(
            f"BLOCKED: accum backtest panel unavailable ({exc}) — the forward "
            "leg accrues from live runs; re-run when the panel sources are up")
    n = len(closes)
    contrib = list(range(0, n, 7))                  # weekly budget
    scales = pol.accum_scales([tiers[i] for i in contrib])
    overlay = pbt.dca_simulate(closes, contrib, budget=1.0, scales=scales)
    plain = pbt.dca_simulate(closes, contrib, budget=1.0)
    legs = pbt.policy_vs_baseline(overlay, plain)
    verdict = gates.policy_verdict(
        overlay_return=legs["overlay_return"], baseline_return=legs["baseline_return"],
        overlay_maxdd=legs["overlay_maxdd"], baseline_maxdd=legs["baseline_maxdd"])
    schema.set_study_status(conn, study["name"], verdict["status"],
                            verdict_at=_now_ms())
    print(f"{study['name']}: {verdict['status']}"
          + (f" — {'; '.join(verdict['reasons'])}" if verdict["reasons"] else ""))
    return [_policy_row(study["name"], "BACKTEST", legs, n,
                        extra={"window": f"{dates[0]}..{dates[-1]}",
                               "contributions": len(contrib),
                               "tier_counts": {t: tiers.count(t)
                                               for t in set(tiers)}})]


_POLICY_RUNNERS = {"btc_trend_policy": _run_trend_policy,
                   "btc_accum_policy": _run_accum_policy}


def _run_portfolio(conn, study: dict) -> list[dict]:
    base = study["name"].split("-v")[0]             # <study>-v2 reuses the runner
    fn = _POLICY_RUNNERS.get(base)
    if fn is None:
        raise SystemExit(f"no portfolio runner wired for {study['name']}")
    return fn(conn, study)


# --- commands --------------------------------------------------------------------

def cmd_register(args) -> None:
    cfg = load_config()
    spec = Path(args.spec)
    if not spec.is_file():
        raise SystemExit(f"pre-registration spec not found: {spec} — write it first "
                         "(studies/_TEMPLATE.md); registration without a spec is "
                         "exactly the drift this system exists to prevent")
    conn = _conn(cfg)
    try:
        schema.register_study(conn, name=args.name, asset=args.asset,
                              evaluator=args.evaluator, tier=args.tier,
                              spec_path=str(spec), registered_at=_now_ms(),
                              primary_horizon=args.horizon)
    finally:
        conn.close()
    print(f"registered {args.name} ({args.asset}/{args.evaluator}/{args.tier}, "
          f"primary horizon {args.horizon})")


def cmd_run(args) -> None:
    cfg = load_config()
    conn = _conn(cfg)
    try:
        study = schema.get_study(conn, args.name)
        if not study:
            raise SystemExit(f"unknown study {args.name} — register first")
        if study["evaluator"] == "portfolio":
            # Policies are continuous overlays — no events table; the runner
            # also sets the POLICY verdict itself. A re-run recomputes the WHOLE
            # window set, so clear prior non-placebo rows first (a segment that
            # disappears — e.g. a mis-windowed LIVE — must not linger as stale).
            rows = _run_portfolio(conn, study)
            conn.execute("DELETE FROM study_results WHERE study=? AND segment != 'PLACEBO'",
                         (args.name,))
            conn.commit()
            schema.record_results(conn, rows)
            print(f"{args.name}: wrote {len(rows)} result rows (sha {emitter_sha()})")
            return
        events = schema.events_for_study(conn, args.name)
        if not events:
            raise SystemExit(f"no events for {args.name} — emit events first")
        if study["evaluator"] == "ts":
            rows = _run_ts(conn, study, events, n_resamples=args.resamples)
        elif study["evaluator"] == "car":
            rows = _run_car(conn, study, events)
        else:
            raise SystemExit(f"evaluator {study['evaluator']} not implemented yet")
        schema.record_results(conn, rows)
        schema.mark_running(conn, args.name)   # never clobbers an existing verdict
    finally:
        conn.close()
    print(f"{args.name}: wrote {len(rows)} result rows "
          f"(sha {emitter_sha()})")


def cmd_placebo(args) -> None:
    cfg = load_config()
    conn = _conn(cfg)
    try:
        study = schema.get_study(conn, args.name)
        if not study:
            raise SystemExit(f"unknown study {args.name}")
        events = schema.events_for_study(conn, args.name)
        if not events:
            raise SystemExit("no events")
        h = study["primary_horizon"]
        if study["evaluator"] == "ts":
            closes, ts_ms = _btc_daily(conn)
            pool = list(range(210, max(211, len(closes) - h - 1)))
            regimes = [ts_study._regime_at(closes, i) for i in pool]
            by_regime: dict[str, list[int]] = {}
            for i, r in zip(pool, regimes):
                by_regime.setdefault(r, []).append(i)
            ev_idx = ts_study._event_indices(ts_ms, sorted(e["event_ts"] for e in events))
            ev_regimes = [ts_study._regime_at(closes, i) for i in ev_idx]

            def eval_t(rnd):
                ev = placebo.redraw_within_regimes(ev_regimes, by_regime, ts_ms, rnd)
                a = ts_study.evaluate(closes, ts_ms, ev, h_days=h,
                                      n_resamples=1, seed=1)["all"]
                return (a["t_clustered"], a["n_months"])
        else:
            raise SystemExit("car placebo wiring lands with the first car study (M3)")
        result = placebo.suite(eval_t, n=args.shuffles)
        p = {"shuffles": args.shuffles, "horizon": h,
             "max_exceed_frac": placebo.EXCEEDANCE_MAX_FRAC}
        schema.record_results(conn, [{
            # Column reuse, documented: PLACEBO rows store the suite summary —
            # t_clustered = 95th pct |t|, win_rate = exceedance fraction,
            # n_events = valid shuffles; full detail in extra_json.
            "study": args.name, "segment": "PLACEBO", "horizon": h,
            "n_events": result["n_valid"], "t_clustered": result["p95_abs_t"],
            "win_rate": result["exceed_frac"],
            "emitter_sha": emitter_sha(), "params_hash": params_hash(p),
            "computed_at": _now_ms(),
            "extra": {"clean": result["clean"], "p95_lt_2": result["p95_lt_2"],
                      "exceedances": result["exceedances"]}}])
    finally:
        conn.close()
    print(f"{args.name} placebo: clean={result['clean']} "
          f"p95|t|={result['p95_abs_t']} exceed={result['exceed_frac']}")


def cmd_report(_args) -> None:
    cfg = load_config()
    conn = _conn(cfg)
    try:
        studies = [dict(r) for r in conn.execute(
            "SELECT * FROM studies ORDER BY registered_at").fetchall()]
        print(f"{'study':<24}{'tier':<9}{'eval':<6}{'status':<11}"
              f"{'seg':<5}{'h':>4}{'n':>6}{'t':>7}{'after-tax':>11}")
        for s in studies:
            rows = schema.results_for_study(conn, s["name"])
            if not rows:
                print(f"{s['name']:<24}{s['tier']:<9}{s['evaluator']:<6}"
                      f"{s['status']:<11}(no results)")
            for r in rows:
                t = f"{r['t_clustered']:.2f}" if r["t_clustered"] is not None else "-"
                at = (f"{r['exp_after_tax']:+.4f}"
                      if r["exp_after_tax"] is not None else "-")
                print(f"{s['name']:<24}{s['tier']:<9}{s['evaluator']:<6}"
                      f"{s['status']:<11}{r['segment']:<5}{r['horizon']:>4}"
                      f"{r['n_events'] or 0:>6}{t:>7}{at:>11}")
    finally:
        conn.close()


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="EDGE-LAB study harness CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    reg = sub.add_parser("register")
    reg.add_argument("--name", required=True)
    reg.add_argument("--asset", required=True, choices=["EQ", "BTC"])
    reg.add_argument("--evaluator", required=True, choices=["car", "ts", "portfolio"])
    reg.add_argument("--tier", required=True, choices=["alpha", "policy", "premium"])
    reg.add_argument("--horizon", required=True, type=int)
    reg.add_argument("--spec", required=True)
    reg.set_defaults(fn=cmd_register)

    run = sub.add_parser("run")
    run.add_argument("--name", required=True)
    run.add_argument("--resamples", type=int, default=ts_study.N_RESAMPLES)
    run.set_defaults(fn=cmd_run)

    pl = sub.add_parser("placebo")
    pl.add_argument("--name", required=True)
    pl.add_argument("--shuffles", type=int, default=placebo.N_SHUFFLES)
    pl.set_defaults(fn=cmd_placebo)

    rep = sub.add_parser("report")
    rep.set_defaults(fn=cmd_report)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()

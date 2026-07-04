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

def _sep_min_ms(lake) -> tuple[str, int]:
    from datetime import datetime, timezone
    sep_min = str(lake.query(
        f"SELECT min(date) AS d FROM {lake.sql_table('sep')}")["d"][0])[:10]
    return sep_min, int(datetime.strptime(sep_min, "%Y-%m-%d")
                        .replace(tzinfo=timezone.utc).timestamp() * 1000)


def _month_start(ts_ms: int) -> str:
    from datetime import datetime, timezone
    d = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date()
    return f"{d.year:04d}-{d.month:02d}-01"


def _monthly_snapshots(lake, events: list[dict]) -> dict[str, list[dict]]:
    from app.data.equities import universe as eq_universe
    months = sorted({_month_start(e["event_ts"]) for e in events})
    return {m: [r for r in eq_universe.build_from_lake(lake, m) if r["included"]]
            for m in months}


def _price_events(lake, events: list[dict], controls: list[list[dict]],
                  horizons: tuple[int, ...],
                  seg_of: list[str | None] | None = None) -> tuple[dict, dict, dict]:
    """Year-chunked pricing shared by run + placebo. Returns
    (rows_by[(seg,h)] -> [(idx, car)], cov_by, unhedged_by); seg defaults ''."""
    from datetime import datetime, timezone

    from app.data.equities import prices as eq_prices

    rows_by: dict = {}
    cov_by: dict = {}
    unhedged_by: dict = {}
    by_year: dict[int, list[int]] = {}
    for i, e in enumerate(events):
        y = datetime.fromtimestamp(e["event_ts"] / 1000, tz=timezone.utc).year
        by_year.setdefault(y, []).append(i)
    for y in sorted(by_year):
        idxs = by_year[y]
        tks = {events[i]["ticker"] for i in idxs}
        for i in idxs:
            tks |= {c["ticker"] for c in controls[i]}
        bars = eq_prices.sep_bars_bulk(lake, sorted(tks), limit=5000,
                                       start_date=f"{y - 1}-12-15",
                                       end_date=f"{y + 1}-04-30")
        for i in idxs:
            seg = seg_of[i] if seg_of else ""
            if seg is None:                        # embargoed
                continue
            for h in horizons:
                res = car.event_car(events[i], bars, controls[i], h)
                if res is None:
                    continue
                c_val, diag = res
                key = (seg, h)
                rows_by.setdefault(key, []).append((i, c_val))
                cov_by.setdefault(key, []).append(diag["mean_controls"])
                if diag["zero_control_sessions"]:
                    unhedged_by[key] = unhedged_by.get(key, 0) + 1
    return rows_by, cov_by, unhedged_by


def _run_car(conn, study: dict, events: list[dict]) -> list[dict]:
    """Chunked CAR run: MONTHLY PIT snapshots for control matching (membership
    drifts slowly; a snapshot as of the event month's 1st is PIT-safe) and
    YEAR-chunked bar fetching (whole-universe full histories would be ~10GB of
    bar dicts; a year window keeps memory flat). Aggregation goes through
    car.aggregate — the same path evaluate() uses."""
    from datetime import datetime, timezone

    from app.data.equities import prices as eq_prices
    from app.data.equities import universe as eq_universe
    from app.data_lake import Lake

    cfg = load_config()
    lake = Lake(cfg.data_lake_path)

    # Pre-filter events before price coverage starts (SEP begins 2016-01):
    # unpriceable, and their snapshot/bar fetches would be pure waste.
    sep_min, sep_min_ms = _sep_min_ms(lake)
    usable = [e for e in events if int(e["event_ts"]) >= sep_min_ms]
    print(f"{study['name']}: {len(events)} events, {len(usable)} within price "
          f"coverage (SEP starts {sep_min})")

    snapshots = _monthly_snapshots(lake, usable)
    print(f"{study['name']}: {len(snapshots)} monthly control snapshots built")

    study_ts: dict[str, list[int]] = {}
    for e in usable:
        study_ts.setdefault(e.get("ticker"), []).append(int(e["event_ts"]))
    controls = [car.match_controls(e, snapshots[_month_start(e["event_ts"])],
                                   study_event_ts_by_ticker=study_ts)
                for e in usable]

    seg_of = [walkforward.segment_of(int(e["event_ts"]), study["registered_at"])
              for e in usable]
    rows_by, cov_by, unhedged_by = _price_events(
        lake, usable, controls, HORIZONS_CAR, seg_of)

    sha, now = emitter_sha(), _now_ms()
    p = {"horizons": HORIZONS_CAR, "k": car.K_CONTROLS,
         "min_cohort": car.MIN_COHORT, "monthly_snapshots": True}
    out_rows = []
    for segment in ("IS", "OOS", "LIVE"):
        for h in HORIZONS_CAR:
            rows = rows_by.get((segment, h))
            if not rows:
                continue
            agg = car.aggregate(rows, usable)["stats"]
            if agg["mean_car"] is None:
                continue
            cars_h = [c for _i, c in rows]
            tiers = [usable[i].get("tier") for i, _c in rows if usable[i].get("tier")]
            tier = max(set(tiers), key=tiers.count) if tiers else None
            trip = tax.expectancy_triplet(cars_h, tier)
            covs = cov_by.get((segment, h), [])
            out_rows.append({
                "study": study["name"], "segment": segment, "horizon": h,
                "tier": tier or "", "n_events": agg["n_events"],
                "n_months": agg["n_months"], "mean_car": agg["mean_car"],
                "t_clustered": agg["t_clustered"], "win_rate": agg["win_rate"],
                "exp_gross": trip["exp_gross"], "exp_net": trip["exp_net"],
                "exp_after_tax": trip["exp_after_tax"],
                "emitter_sha": sha, "params_hash": params_hash(p),
                "computed_at": now,
                "extra": {"mean_controls": (sum(covs) / len(covs)) if covs else None,
                          "n_events_with_unhedged_sessions":
                              unhedged_by.get((segment, h), 0)}})
    return out_rows


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


def _monthly_price_panel(lake, table: str, tickers=None):
    """Month-end adjusted-close panel: DataFrame (index='YYYY-MM', cols=ticker).
    max_by(closeadj, date) = the last adjusted close of each calendar month."""
    filt, params = "", []
    if tickers:
        ph = ",".join("?" for _ in tickers)
        filt, params = f"WHERE ticker IN ({ph})", list(tickers)
    df = lake.query(
        f"SELECT ticker, strftime(CAST(date AS DATE), '%Y-%m') AS ym, "
        f"max_by(closeadj, date) AS close "
        f"FROM {lake.sql_table(table)} {filt} GROUP BY ticker, ym", params)
    return df.pivot(index="ym", columns="ticker", values="close").sort_index()


def _month_end_dates(lake) -> dict:
    df = lake.query(
        f"SELECT strftime(CAST(date AS DATE), '%Y-%m') AS ym, max(date) AS d "
        f"FROM {lake.sql_table('sep')} GROUP BY 1")
    return {r["ym"]: str(r["d"])[:10] for _, r in df.iterrows()}


_LT_FACTOR_COLS = ("ticker", "datekey", "pe", "evebitda", "fcf", "marketcap",
                   "gp", "assets", "roic", "netmargin", "ncfo", "netinc",
                   "ncfdiv", "ncfcommon", "de", "currentratio", "opinc")
_LT_TOP_N = 30
_LT_RT_COST = 0.0020        # 20bps blended round-trip on the fraction of book turned
_LT_TIERS = ("small", "mid", "large")


def _run_lt_factor(conn, study: dict) -> list[dict]:
    """Monthly-rebalance QVM backtest vs the equal-weight PIT universe AND a
    50/50 VTV+QUAL ETF blend; verdict = gates.lt_factor_verdict (§5.5)."""
    from datetime import datetime, timezone

    import numpy as np

    from app.data.equities import universe as eq_universe
    from app.data_lake import Lake
    from app.harness import portfolio_bt as pbt
    from app.lt import factor_screener as fscr

    cfg = load_config()
    lake = Lake(cfg.data_lake_path)
    prices = _monthly_price_panel(lake, "sep")
    etf = _monthly_price_panel(lake, "sfp", tickers=("VTV", "QUAL"))
    med = _month_end_dates(lake)
    art = lake.query(
        f"SELECT {', '.join(_LT_FACTOR_COLS)} FROM {lake.sql_table('sf1')} "
        f"WHERE dimension = 'ART'").sort_values("datekey")

    months = [m for m in prices.index if m in etf.index]
    # need 13 months of price history for 12-1 momentum, and a next month for the
    # forward return -> rebalance range [13, len-2].
    rebal = months[13:-1]
    print(f"lt_factor: {len(rebal)} monthly rebalances {rebal[0]}..{rebal[-1]}")

    def etf_ret(i):
        try:
            r1 = etf.loc[months[i + 1], "VTV"] / etf.loc[months[i], "VTV"] - 1
            r2 = etf.loc[months[i + 1], "QUAL"] / etf.loc[months[i], "QUAL"] - 1
            return 0.5 * r1 + 0.5 * r2
        except (KeyError, ZeroDivisionError):
            return None

    port_ret, uni_ret, e_ret, month_ts = [], [], [], []
    prev_sel: set = set()
    for gi, ym in enumerate(rebal):
        i = months.index(ym)
        as_of = med[ym]
        universe = {r["ticker"] for r in eq_universe.build_from_lake(lake, as_of)
                    if r["included"] and r["tier"] in _LT_TIERS}
        if not universe:
            port_ret.append(None); uni_ret.append(None); e_ret.append(None)
            month_ts.append(None); continue
        # PIT fundamentals: latest ART datekey <= as_of, per ticker
        fund = art[art["datekey"] <= as_of].groupby("ticker").tail(1)
        fund = fund[fund["ticker"].isin(universe)].copy()
        # 12-1 momentum (skip most recent month): price[M-1]/price[M-13]-1
        p_prev, p_13 = prices.loc[months[i - 1]], prices.loc[months[i - 13]]
        mom = (p_prev / p_13 - 1.0)
        fund["mom_12_1"] = fund["ticker"].map(mom)
        sel = fscr.select(fund, top_n=_LT_TOP_N)
        sel_tickers = list(sel["ticker"])

        fwd = (prices.loc[months[i + 1]] / prices.loc[months[i]] - 1.0)
        sel_fwd = [fwd.get(t) for t in sel_tickers]
        sel_fwd = [x for x in sel_fwd if x is not None and np.isfinite(x)]
        uni_fwd = [fwd.get(t) for t in universe]
        uni_fwd = [x for x in uni_fwd if x is not None and np.isfinite(x)]
        if not sel_fwd or not uni_fwd:
            port_ret.append(None); uni_ret.append(None); e_ret.append(None)
            month_ts.append(None); prev_sel = set(sel_tickers); continue

        # turnover cost on the portfolio leg (the strategy's real disadvantage)
        cur = set(sel_tickers)
        turnover = (len(cur - prev_sel) / len(cur)) if cur else 0.0
        prev_sel = cur

        port_ret.append(sum(sel_fwd) / len(sel_fwd) - turnover * _LT_RT_COST)
        uni_ret.append(sum(uni_fwd) / len(uni_fwd))
        e_ret.append(etf_ret(i))
        month_ts.append(int(datetime.strptime(as_of, "%Y-%m-%d")
                            .replace(tzinfo=timezone.utc).timestamp() * 1000))

    # segment by IS <= 2021-12-31 / OOS after
    def seg(ts): return "IS" if ts <= walkforward.IS_END_MS else "OOS"
    rows_active_u: dict = {"IS": [], "OOS": []}
    rows_active_e: dict = {"IS": [], "OOS": []}
    ts_by: dict = {"IS": [], "OOS": []}
    for p, u, e, ts in zip(port_ret, uni_ret, e_ret, month_ts):
        if ts is None or p is None or u is None:
            continue
        s = seg(ts)
        rows_active_u[s].append(p - u)
        ts_by[s].append(ts)
        if e is not None:
            rows_active_e[s].append((p - e, ts))

    sha, now = emitter_sha(), _now_ms()
    p_hash = params_hash({"top_n": _LT_TOP_N, "rt_cost": _LT_RT_COST,
                          "tiers": _LT_TIERS})
    out_rows, legs = [], {}
    for segment in ("IS", "OOS"):
        au, ts_u = rows_active_u[segment], ts_by[segment]
        ae = rows_active_e[segment]
        if len(au) < 2:
            continue
        t_u = stats.clustered_t(au, ts_u)["t"]
        t_e = (stats.clustered_t([a for a, _ in ae], [t for _, t in ae])["t"]
               if len(ae) >= 2 else None)
        legs[segment] = {"t_vs_universe": t_u, "t_vs_etf": t_e,
                         "n_months": len(au)}
        out_rows.append({
            "study": study["name"], "segment": segment, "horizon": 0,
            "n_events": len(au), "n_months": len(au),
            "mean_car": sum(au) / len(au),
            "t_clustered": min([x for x in (t_u, t_e) if x is not None], default=None),
            "emitter_sha": sha, "params_hash": p_hash, "computed_at": now,
            "extra": {"t_vs_universe": t_u, "t_vs_etf": t_e,
                      "active_mean_vs_universe": sum(au) / len(au),
                      "active_mean_vs_etf": (sum(a for a, _ in ae) / len(ae)
                                             if ae else None)}})

    # BACKTEST display row: compounded curves over the full window
    bt_u = pbt.rebalance_backtest(port_ret, uni_ret)
    bt_e = pbt.rebalance_backtest(port_ret, e_ret)
    out_rows.append({
        "study": study["name"], "segment": "BACKTEST", "horizon": 0,
        "n_events": bt_u["n_periods"], "mean_car": bt_u["active_total"],
        "emitter_sha": sha, "params_hash": p_hash, "computed_at": now,
        "extra": {"port_total": bt_u["port_total"],
                  "universe_total": bt_u["bench_total"],
                  "etf_total": bt_e["bench_total"],
                  "port_maxdd": bt_u["port_maxdd"],
                  "active_total_vs_universe": bt_u["active_total"],
                  "active_total_vs_etf": bt_e["active_total"]}})

    oos = legs.get("OOS", {})
    v = gates.lt_factor_verdict(t_vs_universe=oos.get("t_vs_universe"),
                                t_vs_etf=oos.get("t_vs_etf"),
                                n_months=oos.get("n_months", 0))
    schema.set_study_status(conn, study["name"], v["status"], verdict_at=_now_ms())
    print(f"{study['name']}: {v['status']}"
          + (f" — {'; '.join(v['reasons'])}" if v["reasons"] else "")
          + f"  [OOS t_vs_universe={oos.get('t_vs_universe')} "
            f"t_vs_etf={oos.get('t_vs_etf')} n_months={oos.get('n_months')}]")
    return out_rows


_POLICY_RUNNERS = {"btc_trend_policy": _run_trend_policy,
                   "btc_accum_policy": _run_accum_policy,
                   "lt_factor": _run_lt_factor}


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
        elif study["evaluator"] == "car":
            import random as _random

            from app.data_lake import Lake
            lake = Lake(cfg.data_lake_path)
            _min_date, sep_min_ms = _sep_min_ms(lake)
            usable = [e for e in events if int(e["event_ts"]) >= sep_min_ms]
            # A fixed-seed SUBSAMPLE keeps 50 shuffles tractable (the suite
            # detects machinery bias, not the study's effect — the null t's
            # distribution is what matters, and 1,000 events span the same
            # months). Deterministic, documented in the PLACEBO row.
            sub = (_random.Random(1234).sample(usable, 1000)
                   if len(usable) > 1000 else usable)
            snapshots = _monthly_snapshots(lake, sub)   # date multiset is shuffle-invariant

            def eval_t(rnd):
                sh = placebo.shuffle_dates_per_ticker(sub, rnd)
                s_ts: dict[str, list[int]] = {}
                for e in sh:
                    s_ts.setdefault(e.get("ticker"), []).append(int(e["event_ts"]))
                ctls = [car.match_controls(
                    e, snapshots[_month_start(e["event_ts"])],
                    study_event_ts_by_ticker=s_ts) for e in sh]
                rows_by, _cov, _unh = _price_events(lake, sh, ctls, (h,))
                rows = rows_by.get(("", h), [])
                if not rows:
                    return None
                st = car.aggregate(rows, sh)["stats"]
                return (st["t_clustered"], st["n_months"])
        else:
            raise SystemExit(f"no placebo path for evaluator {study['evaluator']}")
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


def cmd_verdict(args) -> None:
    """Apply the ALPHA gate (§5.5) to a study's recorded OOS(+LIVE) results at
    its primary horizon. POLICY studies verdict inside their runner; this is
    the event-study path. Sign consistency = same mean_car sign in IS and OOS
    (the recorded pre/post-structural-break proxy; documented)."""
    import json as _json

    cfg = load_config()
    conn = _conn(cfg)
    try:
        study = schema.get_study(conn, args.name)
        if not study:
            raise SystemExit(f"unknown study {args.name}")
        if study["tier"] != "alpha":
            raise SystemExit("verdict is the ALPHA path; policies verdict in their runner")
        h = study["primary_horizon"]
        rows = schema.results_for_study(conn, args.name)
        oos = [r for r in rows if r["segment"] == "OOS" and r["horizon"] == h]
        live = [r for r in rows if r["segment"] == "LIVE" and r["horizon"] == h]
        is_rows = [r for r in rows if r["segment"] == "IS" and r["horizon"] == h]
        pl = next((r for r in rows if r["segment"] == "PLACEBO"), None)
        if not oos:
            raise SystemExit("no OOS results at the primary horizon — run first")
        # OOS(+LIVE) population: n sums; t taken from OOS (LIVE too thin to
        # matter until it isn't — recomputed monthly per §9.5).
        n_events = sum(r["n_events"] or 0 for r in oos + live)
        n_months = sum(r["n_months"] or 0 for r in oos + live)
        t = oos[0]["t_clustered"]
        after_tax = oos[0]["exp_after_tax"]
        placebo_clean = bool(pl and _json.loads(pl["extra_json"] or "{}").get("clean"))
        # Sign consistency is a two-population test (§5.5 "where applicable"):
        # with a single-era population (e.g. clone13f — complete SF3 books only
        # exist 2022+) the split is NOT APPLICABLE, which must not read as a
        # hard failure. Only an actual sign FLIP between measured eras fails.
        if (is_rows and oos and is_rows[0]["mean_car"] is not None
                and oos[0]["mean_car"] is not None):
            sign_consistent = (is_rows[0]["mean_car"] > 0) == (oos[0]["mean_car"] > 0)
        else:
            sign_consistent = True      # split inapplicable — vacuously passes
        already_extended = study["status"] == "EXTEND"
        v = gates.alpha_verdict(
            t_clustered=t, n_months=n_months, n_events=n_events,
            exp_after_tax=after_tax, sign_consistent=sign_consistent,
            placebo_clean=placebo_clean, min_events=args.min_events,
            already_extended=already_extended)
        schema.set_study_status(conn, args.name, v["status"], verdict_at=_now_ms())
    finally:
        conn.close()
    print(f"{args.name}: {v['status']}"
          + (f" — {'; '.join(v['reasons'])}" if v["reasons"] else "")
          + f"  [t={t} n_events={n_events} n_months={n_months} "
            f"after_tax={after_tax} placebo_clean={placebo_clean} "
            f"sign_consistent={sign_consistent}]")


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

    ver = sub.add_parser("verdict")
    ver.add_argument("--name", required=True)
    ver.add_argument("--min-events", type=int, default=gates.ALPHA_MIN_EVENTS)
    ver.set_defaults(fn=cmd_verdict)

    rep = sub.add_parser("report")
    rep.set_defaults(fn=cmd_report)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()

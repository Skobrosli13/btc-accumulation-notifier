"""Cross-sectional walk-forward backtest of the stock swing archetypes -> seed win-rates.

Reuses the LIVE engine (``stock_scoring`` -> ``stock_levels`` -> ``stock_positions.reprice``)
so the backtest can't drift from production, and replays the SAME selection the live
collector applies each close — cross-sectional ``rank()`` -> expectancy-weighted
priority -> top-N -> per-(ticker, archetype) cooldown — so the measured population is
the *surfaced/alerted* population, not every raw archetype firing. Entries fill at
the NEXT bar's open (nothing is tradable at the close that signals it).

Honesty guards:
- PEAD events come from announcement-dated calendar windows (``earnings.surprise_history``);
  a per-ticker events map dominated by fiscal quarter-end dates — the look-ahead
  signature of the old period-aligned feed — is rejected outright.
- A ``random_entry`` baseline (same liquidity filter, same per-archetype R-frames,
  uniformly sampled entry dates, same next-bar-open fills and costs) is measured
  alongside, and each archetype's win-rate/expectancy is also reported as a DELTA
  over that baseline — whatever the R-frame plus the tape hand to ANY entry rule
  is not an edge.
- Trades are serially correlated (overlapping holding windows, clustered regimes);
  cells carry ``n_months`` (distinct entry months) and the dispersion of per-month
  mean R so the raw ``n`` can't masquerade as independent samples.

This is IN-SAMPLE (survivorship-biased: only currently-listed names, current
universe) — a *seed prior*, honestly weaker than the live out-of-sample record that
``stock_calibrate`` later writes. Keyless (prices only); PEAD is added when a
Finnhub key is present.

    python -m scripts.stock_backtest --limit 40
    python -m scripts.stock_backtest --limit 120 --write   # write app/stock_st_winrates.json
"""
from __future__ import annotations

import argparse
import bisect
import json
import logging
import random
import statistics
from datetime import datetime, timezone
from pathlib import Path

from app import stock_confidence, stock_levels, stock_positions, stock_scoring
from app.config import load_config
from app.sources.stocks import earnings as earnings_src
from app.sources.stocks import prices, universe

log = logging.getLogger("stock-backtest")

_WARMUP = 210          # bars before the first tradable signal
_HISTORY_BARS = 1400   # ~5.5 calendar years -> >=4 tradable years after warmup
_LIVE_WINDOW = 400     # feature window the live collector sees (stock_collect._PRICE_BARS)
_QE_GUARD = 0.40       # reject an earnings map with >40% quarter-end report dates
_BASELINE_EVERY = 21   # ~one random baseline entry per ticker-month
_UNIVERSE_N = 536      # live universe size (app/stock_universe.json) top-N is cut from
_PENDING_EXPIRY_MS = 5 * 86_400_000   # mirror of stock_collect._PENDING_EXPIRY_MS
_MIN_SIG_MONTHS = 6    # both arms need >= this many distinct entry months
DAY_MS = 86_400_000

METHOD = ("cross-sectional replay of the live selection (rank -> expectancy-weighted "
          "priority -> top-N -> cooldown) with the top-N scaled by sample fraction "
          f"(top_n_eff = max(1, round(stock_top_n * n_tickers / {_UNIVERSE_N})) so a "
          "sampled universe keeps live selectivity), next-bar-open fills — entries "
          "whose next bar is >5 calendar days after the decision bar are skipped as "
          "unfilled (mirror of the live PENDING expiry; skips logged) — net of "
          "costs; PEAD aligned to announcement dates (quarter-end-dominated maps "
          "rejected; candidate window mirrors the live lookback+4d calendar fetch, "
          "with the shared trading-bar bars_since gate doing the eligibility cut); "
          "effective-n = n_months (distinct entry months; trades are serially "
          "correlated so raw n overstates the sample); dispersion = stdev of "
          "per-month mean R; baseline = random_entry control (same liquidity filter "
          "and per-archetype R-frames, uniformly sampled entry dates) with per-cell "
          "delta_* fields vs that baseline (trade-weighted, display only); "
          "significance is fully month-clustered on BOTH arms: delta of per-month "
          "mean R (archetype minus baseline) must exceed 2*SE with "
          "SE = sqrt(var(arch months)/n_a + var(base months)/n_b), "
          f">= {_MIN_SIG_MONTHS} months on each arm, and a positive month-clustered "
          "win-rate delta")


def month_key(ts_ms: int) -> str:
    """'YYYY-MM' bucket of an epoch-ms timestamp (entry-month clustering)."""
    d = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return f"{d.year:04d}-{d.month:02d}"


def _bars(res) -> list[dict]:
    return [{"ts": b[0], "open": b[1], "high": b[2], "low": b[3], "close": b[4], "volume": b[5]}
            for b in res[0]]


def _earnings_by_ts(ticker: str, cfg, years: float) -> dict[int, dict]:
    """Map report_ts -> earnings row for PEAD in the backtest (empty without a key).

    Rejects a map where more than ``_QE_GUARD`` of report dates land on calendar
    quarter-end days: real announcements trail quarter-end by weeks, so that
    pattern means the feed is fiscal-period-aligned (look-ahead) — PEAD then runs
    dark for the ticker rather than fabricating an edge."""
    if not cfg.finnhub_active:
        return {}
    rows = earnings_src.surprise_history(ticker, cfg.finnhub_api_key, years=years)
    rows = [r for r in rows if r.get("surprise_pct") is not None]
    if not rows:
        return {}
    frac = earnings_src.quarter_end_fraction([r["report_ts"] for r in rows])
    if frac > _QE_GUARD:
        log.warning("%s: %.0f%% of report dates are quarter-ends (period-aligned feed, "
                    "look-ahead signature) — dropping PEAD for this ticker", ticker, frac * 100)
        return {}
    return {r["report_ts"]: r for r in rows}


def _latest_earnings(emap: dict[int, dict], last_ts: int, cfg) -> dict | None:
    """Latest earnings event in the candidate fetch window — mirrors live EXACTLY:
    ``stock_collect._fetch_earnings`` pulls a ``stock_pead_lookback_days + 4``
    CALENDAR-day window and the shared ``stock_scoring.pead_candidate`` applies the
    real eligibility gate in TRADING bars (1 <= bars_since <= lookback). Cutting at
    ``lookback`` calendar days here would silently drop the stalest weekend/holiday-
    straddling reports that live still alerts."""
    if not emap:
        return None
    lo = last_ts - (cfg.stock_pead_lookback_days + 4) * DAY_MS
    cands = [e for ts, e in emap.items() if lo <= ts <= last_ts]
    return max(cands, key=lambda e: e["report_ts"]) if cands else None


def _structure_stop(bars: list[dict], report_ts: int, direction: str) -> float | None:
    """Swing low/high since the earnings report — the PEAD thesis-invalidation level.
    (Mirror of app.stock_collect._structure_stop, kept local so the offline script
    doesn't import the collector.)"""
    seg = [b for b in bars if b["ts"] >= report_ts]
    if not seg:
        return None
    return min(b["low"] for b in seg) if direction == "BUY" else max(b["high"] for b in seg)


def _spy_regime_lookup(cfg, limit: int):
    """Per-date SPY-vs-200DMA regime (the live 'don't fight the tape' gate), as a
    ts -> 'bull'|'bear'|'unknown' lookup. None when SPY history is unavailable."""
    res = prices.daily_bars("SPY", cfg, limit=limit)
    if not res or len(res[0]) < 200:
        return None
    bars = res[0]
    closes = [b[4] for b in bars]
    ts_list: list[int] = []
    states: list[str] = []
    run = sum(closes[:200])
    for i in range(199, len(bars)):
        if i > 199:
            run += closes[i] - closes[i - 200]
        ts_list.append(bars[i][0])
        states.append("bull" if closes[i] >= run / 200 else "bear")

    def lookup(ts: int) -> str:
        j = bisect.bisect_right(ts_list, ts) - 1
        return states[j] if j >= 0 else "unknown"

    return lookup


def _resolve(trade_bars: list[dict], direction: str, entry: float, atr: float,
             archetype: str, cfg, structure_stop: float | None = None) -> dict | None:
    """Open at ``entry`` and resolve against ``trade_bars`` (fill bar first) via the
    live level/reprice engine. None if levels unavailable or still open at data end."""
    lv = stock_levels.compute(direction, entry, atr, archetype, cfg,
                              structure_stop=structure_stop)
    if not lv:
        return None
    pos = {"direction": direction, "entry": lv["entry"], "stop": lv["stop"],
           "t2": lv["t2"], "mfe_r": 0.0, "mae_r": 0.0}
    upd = stock_positions.reprice(pos, trade_bars, "", lv["time_stop_days"],
                                  cost_bps=cfg.stock_cost_bps)
    return upd


def run_backtest(bars_by_ticker: dict[str, list[dict]],
                 earnings_by_ticker: dict[str, dict[int, dict]],
                 cfg, regime_lookup=None) -> list[dict]:
    """Walk all tickers date-by-date TOGETHER and replay the live selection.

    Per date: liquid features for every ticker (universe ret_63 for rel-strength),
    ``pick_candidate`` per ticker, ``rank()`` under the day's regime, priority =
    ``priority_score(composite, prior expectancy)`` (the built-in PRIOR, not the
    committed winrates, to avoid circularity), a top-N cut SCALED by the sample
    fraction (``max(1, round(stock_top_n * n_tickers / _UNIVERSE_N))`` — live cuts
    top-15 from ~536 names, so an unscaled cut on a 40-120 ticker sample would be
    far less selective than production), then the live cooldown +
    no-open-duplicate gates. Surfaced setups fill at the NEXT bar's open; an entry
    whose next bar is >5 calendar days out is skipped as unfilled (mirror of the
    live PENDING expiry — the alert still arms the cooldown, as live records the
    alert before the fill attempt). Returns closed trades (context fixed at 0 —
    no historical insider/short-vol/revision data exists offline)."""
    prior_exp = {a: stock_confidence.base_rate(a, None)["expectancy_r"]
                 for a in stock_levels.ARCHETYPE_LEVELS}
    idx_by_ticker = {tk: {b["ts"]: i for i, b in enumerate(bars)}
                     for tk, bars in bars_by_ticker.items()}
    all_dates = sorted({b["ts"] for bars in bars_by_ticker.values() for b in bars})
    top_n_eff = max(1, round(cfg.stock_top_n * len(bars_by_ticker) / _UNIVERSE_N))
    log.info("replay top-N: %d (live top-%d scaled by %d/%d sampled tickers)",
             top_n_eff, cfg.stock_top_n, len(bars_by_ticker), _UNIVERSE_N)
    expired_unfilled = 0

    trades: list[dict] = []
    busy_until: dict[tuple[str, str], float] = {}   # (ticker, archetype) -> in-trade ts
    last_alert: dict[tuple[str, str], int] = {}     # (ticker, archetype) -> cooldown ts

    for date in all_dates:
        candidates: list = []
        universe_ret63: dict[str, float] = {}
        for tk, bars in bars_by_ticker.items():
            i = idx_by_ticker[tk].get(date)
            if i is None or i < _WARMUP or i >= len(bars) - 1:   # need a fill bar
                continue
            window = bars[max(0, i + 1 - _LIVE_WINDOW): i + 1]
            feat = stock_scoring.features(window)
            if not feat or not stock_scoring.liquid(feat, cfg):
                continue
            universe_ret63[tk] = feat.get("ret_63") or 0.0
            if not feat.get("atr"):
                continue   # live drops level-less candidates before they take a top-N slot
            earn = _latest_earnings(earnings_by_ticker.get(tk) or {}, feat["last_ts"], cfg)
            cand = stock_scoring.pick_candidate(tk, feat, window, earn, cfg)
            if cand is None:
                continue
            if cand.direction == "SELL" and not cfg.stock_allow_shorts:
                continue   # match live: Phase 1 is long-only
            cand._feat = feat
            cand._window = window
            cand._i = i
            candidates.append(cand)
        if not candidates:
            continue

        regime = regime_lookup(date) if regime_lookup else "unknown"
        ranked = stock_scoring.rank(candidates, regime, universe_ret63)
        records = sorted(((stock_scoring.priority_score(c.composite, prior_exp.get(c.archetype)), c)
                          for c in ranked), key=lambda r: r[0], reverse=True)

        for rank_no, (_priority, c) in enumerate(records, start=1):
            if rank_no > top_n_eff:
                break
            key = (c.ticker, c.archetype)
            if busy_until.get(key, 0) >= date:      # no pyramiding per archetype
                continue
            last = last_alert.get(key)
            if last is not None and (date - last) < cfg.stock_cooldown_days * DAY_MS:
                continue
            bars = bars_by_ticker[c.ticker]
            fill = bars[c._i + 1]
            if fill["ts"] - date > _PENDING_EXPIRY_MS:
                # Live inserts a PENDING position that expires unfilled when the
                # first post-signal bar is >5 calendar days out (halt/data gap).
                # The alert was still recorded, so the cooldown still arms.
                last_alert[key] = date
                expired_unfilled += 1
                continue
            structure = None
            if c.archetype == "pead_drift" and c.detail.get("report_ts"):
                structure = _structure_stop(c._window, c.detail["report_ts"], c.direction)
            upd = _resolve(bars[c._i + 1:], c.direction, fill["open"], c._feat["atr"],
                           c.archetype, cfg, structure_stop=structure)
            if upd is None:
                continue
            last_alert[key] = date   # the setup surfaced/alerted regardless of outcome
            if upd["status"] != "CLOSED":
                busy_until[key] = float("inf")   # still open at end of data
                continue
            busy_until[key] = upd["closed_ts"]
            trades.append({"ticker": c.ticker, "archetype": c.archetype,
                           "direction": c.direction, "entry": fill["open"],
                           "entry_ts": fill["ts"], "month": month_key(fill["ts"]),
                           "realized_r": upd["realized_r"], "exit_reason": upd["exit_reason"]})
    log.info("pending-expiry mirror: %d surfaced entries skipped unfilled (next bar >5d)",
             expired_unfilled)
    return trades


def baseline_trades(bars_by_ticker: dict[str, list[dict]], cfg, seed: int = 42) -> list[dict]:
    """``random_entry`` control: uniformly sampled entry dates per ticker (same
    liquidity filter, same per-archetype R-frames, same next-bar-open fills and
    costs — including the same live pending-expiry mirror: a next bar >5 calendar
    days out is skipped as unfilled). Each sampled date opens one BUY per archetype
    frame so every archetype cell has a matched baseline in its own frame."""
    rng = random.Random(seed)
    out: list[dict] = []
    skipped = 0
    for tk, bars in sorted(bars_by_ticker.items()):
        eligible = list(range(_WARMUP, len(bars) - 1))
        if not eligible:
            continue
        n_samples = min(len(eligible), max(1, len(eligible) // _BASELINE_EVERY))
        for i in sorted(rng.sample(eligible, n_samples)):
            window = bars[max(0, i + 1 - _LIVE_WINDOW): i + 1]
            feat = stock_scoring.features(window)
            if not feat or not stock_scoring.liquid(feat, cfg) or not feat.get("atr"):
                continue
            fill = bars[i + 1]
            if fill["ts"] - bars[i]["ts"] > _PENDING_EXPIRY_MS:
                skipped += 1                     # live would expire this fill unfilled
                continue
            for archetype in stock_levels.ARCHETYPE_LEVELS:
                upd = _resolve(bars[i + 1:], "BUY", fill["open"], feat["atr"], archetype, cfg)
                if upd is None or upd["status"] != "CLOSED":
                    continue
                out.append({"ticker": tk, "archetype": archetype, "direction": "BUY",
                            "entry": fill["open"], "entry_ts": fill["ts"],
                            "month": month_key(fill["ts"]),
                            "realized_r": upd["realized_r"], "exit_reason": upd["exit_reason"]})
    log.info("pending-expiry mirror: %d baseline entries skipped unfilled (next bar >5d)",
             skipped)
    return out


def _month_series(trades: list[dict]) -> tuple[list[float], list[float]]:
    """Per-month (mean R, win-rate) series over a trade list — the clustered units
    the significance test runs on (trades within a month are serially correlated,
    so months, not trades, are the closest thing to independent samples)."""
    months: dict[str, list[float]] = {}
    for t in trades:
        m = t.get("month")
        if m:
            months.setdefault(m, []).append(float(t.get("realized_r") or 0.0))
    means = [sum(rs) / len(rs) for rs in months.values()]
    wins = [sum(1 for r in rs if r > 0) / len(rs) for rs in months.values()]
    return means, wins


def build_cells(trades: list[dict], baseline: list[dict] | None = None) -> dict:
    """Per-archetype win-rate cells with sample-honesty fields.

    Keeps the reader-compatible keys (``n`` / ``win_rate`` / ``expectancy_r``) and adds:
    - ``n_months`` + ``expectancy_r_month_std`` — distinct entry months and the
      dispersion of per-month mean R (the honest effective sample vs the raw,
      serially-correlated ``n``);
    - ``baseline_*`` / ``delta_*`` when a random-entry control is supplied
      (trade-weighted, for display — significance uses the month-clustered test);
    - ``alignment: announcement_date`` on ``pead_drift`` cells (the validity marker
      the confidence model requires before trusting a PEAD base rate)."""
    summary = stock_positions.summarize(trades)
    base_summary = stock_positions.summarize(baseline or [])
    by_arch: dict[str, list[dict]] = {}
    for t in trades:
        by_arch.setdefault(t.get("archetype", "?"), []).append(t)
    by_arch_base: dict[str, list[dict]] = {}
    for t in baseline or []:
        by_arch_base.setdefault(t.get("archetype", "?"), []).append(t)
    cells: dict[str, dict] = {}
    for k, v in summary["archetypes"].items():
        if not v["n"]:
            continue
        month_means, month_wins = _month_series(by_arch.get(k, []))
        cell = {"n": v["n"], "win_rate": v["win_rate"], "expectancy_r": v["expectancy_r"],
                "n_months": len(month_means),
                "expectancy_r_month_std": (round(statistics.stdev(month_means), 3)
                                           if len(month_means) >= 2 else None)}
        b = base_summary["archetypes"].get(k)
        if baseline is not None and b and b["n"]:
            cell.update({
                "baseline_n": b["n"],
                "baseline_win_rate": b["win_rate"],
                "baseline_expectancy_r": b["expectancy_r"],
                "delta_win_rate": round(v["win_rate"] - b["win_rate"], 3),
                "delta_expectancy_r": round(v["expectancy_r"] - b["expectancy_r"], 3),
            })
        # Pre-registered significance rule — fully month-clustered on BOTH arms:
        # the delta of per-month mean R (archetype minus random-entry baseline)
        # must clear 2 SE with SE = sqrt(var_a/n_a + var_b/n_b) over
        # >= _MIN_SIG_MONTHS distinct months on each arm, plus a positive
        # month-clustered win-rate delta. The delta_* fields above stay
        # trade-weighted for display only: one clustered month of correlated
        # winners can inflate those, but not this test (each month counts once).
        # Cells without a control stay not_significant here; the live promotion
        # path (scripts/stock_calibrate) recomputes significance against the
        # seed's stored baseline instead.
        significant = False
        base_means, base_wins = _month_series(by_arch_base.get(k, []))
        if (baseline is not None and len(month_means) >= _MIN_SIG_MONTHS
                and len(base_means) >= _MIN_SIG_MONTHS):
            delta_m = (sum(month_means) / len(month_means)
                       - sum(base_means) / len(base_means))
            se = (statistics.variance(month_means) / len(month_means)
                  + statistics.variance(base_means) / len(base_means)) ** 0.5
            wr_delta_m = (sum(month_wins) / len(month_wins)
                          - sum(base_wins) / len(base_wins))
            significant = delta_m > 2 * se and wr_delta_m > 0
        cell["not_significant"] = not significant
        if k == "pead_drift":
            cell["alignment"] = "announcement_date"
        cells[k] = cell
    return cells


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40, help="sample this many universe tickers")
    ap.add_argument("--bars", type=int, default=_HISTORY_BARS,
                    help="daily bars of history per ticker (venue permitting)")
    ap.add_argument("--seed", type=int, default=42, help="random_entry baseline seed")
    ap.add_argument("--write", action="store_true", help="write app/stock_st_winrates.json")
    args = ap.parse_args(argv)
    cfg = load_config()

    uni = universe.read_universe_file(cfg.stock_universe_path)[: args.limit]
    years = args.bars / 252 + 0.5
    bars_by_ticker: dict[str, list[dict]] = {}
    earnings_by_ticker: dict[str, dict[int, dict]] = {}
    for u in uni:
        tk = u["ticker"]
        res = prices.daily_bars(tk, cfg, limit=args.bars)
        if not res or len(res[0]) < _WARMUP + 30:
            continue
        bars_by_ticker[tk] = _bars(res)
        earnings_by_ticker[tk] = _earnings_by_ts(tk, cfg, years)
        log.info("%s: %d bars, %d earnings events", tk, len(bars_by_ticker[tk]),
                 len(earnings_by_ticker[tk]))

    regime_lookup = _spy_regime_lookup(cfg, args.bars)
    if regime_lookup is None:
        log.warning("SPY history unavailable — regime fixed at 'unknown'")
    trades = run_backtest(bars_by_ticker, earnings_by_ticker, cfg, regime_lookup)
    baseline = baseline_trades(bars_by_ticker, cfg, seed=args.seed)
    log.info("%d surfaced trades, %d baseline trades across %d tickers",
             len(trades), len(baseline), len(bars_by_ticker))

    cells = build_cells(trades, baseline)
    summary = stock_positions.summarize(trades)
    base_cells = {k: {"n": v["n"], "win_rate": v["win_rate"], "expectancy_r": v["expectancy_r"]}
                  for k, v in stock_positions.summarize(baseline)["archetypes"].items() if v["n"]}
    winrates = {"generated_at": datetime.now(timezone.utc).isoformat(),
                "source": "backtest",
                "n_tickers": len(bars_by_ticker),
                "note": ("In-sample cross-sectional seed (survivorship-biased); measures the "
                         "SURFACED population (rank -> priority -> sample-scaled top-N -> "
                         "cooldown), context fixed at 0."),
                "method": METHOD,
                "baseline": base_cells,
                "archetypes": cells}
    print(json.dumps({"overall": summary["overall"], "archetypes": cells,
                      "baseline": base_cells, "n_tickers": len(bars_by_ticker)}, indent=2))
    if args.write:
        out = Path(__file__).resolve().parents[1] / "app" / "stock_st_winrates.json"
        out.write_text(json.dumps(winrates, indent=2))
        log.info("wrote %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Long-term "long buys" collector (cron: weekly).

Reads price history from `stock_prices` (already maintained daily by the swing
collector) for momentum/trend, refreshes Massive financials in throttled slices
(5/min free limit; financials are quarterly so staleness is fine), computes
fundamentals, runs the gate->rank->combine engine, and maintains the SPY-benchmarked
accumulation forward-test.

    python -m app.stock_lt_collect --dry-run --limit 40 --financials-limit 40
    python -m app.stock_lt_collect                       # live weekly run
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from datetime import datetime, timezone

from . import stock_fundamentals, stock_lt_scoring, stock_lt_store, stock_store, store
from .config import Config, load_config
from .sources.stocks import massive, prices

log = logging.getLogger("stock-lt-collect")

_STALE_DAYS = 80          # refresh financials older than this
_THROTTLE_S = 12.5        # ~5 calls/min for Massive financials (free-tier limit)
_MIN_BARS = 210           # need ~1yr history for 200DMA + 12-1 momentum
_MAX_BAR_AGE_MS = 7 * 86_400_000   # ~5 trading days: older stored bars = dead ticker, don't score
_SPY_BARS = 60            # SPY history depth: must cover the close-deferral window
_SPY_MATCH_TOL_MS = 5 * 86_400_000    # exit bar pairs with the nearest SPY bar <= 5 cal days away
_MAX_DEFER_MS = 30 * 86_400_000       # a close deferred past this force-closes (data_gap)


def _momentum_trend(bars: list[dict]) -> tuple[float | None, bool]:
    """(12-1 momentum, above_200dma) from daily bars (oldest->newest)."""
    closes = [b["close"] for b in bars]
    n = len(closes)
    if n < _MIN_BARS:
        return None, False
    dma200 = sum(closes[-200:]) / 200
    above = closes[-1] >= dma200
    if n >= 252:
        mom = closes[-21] / closes[-252] - 1.0   # 12-1: 12mo ago -> 1mo ago
    else:
        mom = closes[-21] / closes[0] - 1.0
    return mom, above


def _refresh_financials(conn, cfg: Config, universe: list[dict], limit: int,
                        throttle: bool) -> int:
    """Refresh the stalest/missing financials, up to ``limit`` (rate-limited). Returns
    how many were fetched."""
    if not cfg.massive_active:
        return 0
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    fresh = stock_lt_store.financials_freshness(conn)
    cutoff = now_ms - _STALE_DAYS * 86400_000
    # priority: never-fetched first, then stalest
    def staleness(tk):
        return fresh.get(tk, -1)
    todo = [u["ticker"] for u in universe if fresh.get(u["ticker"], 0) < cutoff]
    todo.sort(key=staleness)   # missing (-1/0) first, then oldest
    fetched = 0
    for tk in todo[:limit]:
        periods = massive.financials(tk, cfg.massive_api_key, limit=2, timeframe="annual")
        if periods:
            shares = stock_fundamentals._v(periods[0], "income_statement", "diluted_average_shares")
            stock_lt_store.upsert_financials(conn, tk, shares, periods)
            fetched += 1
        if throttle:
            time.sleep(_THROTTLE_S)
    log.info("financials: refreshed %d (of %d stale/missing)", fetched, len(todo))
    return fetched


def _spy_quote(cfg: Config) -> tuple[float, int, dict[int, float], str] | None:
    """(latest SPY close, its bar ts, {bar_ts: close} for date-matching, venue).

    Holdings are opened/closed against the SPY close of the SAME bar date as the
    name's close — a stale name price benchmarked against a fresh SPY quote would
    corrupt the excess-return forward-test with timestamp-mismatched pairs.
    ``_SPY_BARS`` (~12 weeks) covers the deferral window so a delisted/frozen name
    whose last bar is weeks old can still find a date-matched pair and close."""
    res = prices.daily_bars("SPY", cfg, limit=_SPY_BARS)
    if not res:
        return None
    bars, src = res
    by_ts = {b[0]: b[4] for b in bars}
    return bars[-1][4], bars[-1][0], by_ts, src


def _nearest_spy(spy_by_ts: dict[int, float], ts: int,
                 tol_ms: int | None = _SPY_MATCH_TOL_MS) -> float | None:
    """SPY close of the bar NEAREST to ``ts`` (exchange-holiday tolerant), or None
    when the nearest bar is further than ``tol_ms`` away (``tol_ms=None`` = no
    bound — the force-close last resort)."""
    if not spy_by_ts or ts is None:
        return None
    best = min(spy_by_ts, key=lambda k: abs(k - ts))
    if tol_ms is not None and abs(best - ts) > tol_ms:
        return None
    return spy_by_ts[best]


def _benchmark_basis(spy_src: str | None) -> str:
    """Honesty label for the excess-vs-SPY measure, derived from the venue's ACTUAL
    adjustment basis (prices.VENUE_BASIS) — only a split+dividend (total-return)
    series measures dividends; everything else is a price-only comparison."""
    return ("total_return_adjusted"
            if prices.VENUE_BASIS.get(spy_src or "") == "split_div"
            else "price_only_ex_dividends")


def run(cfg: Config, *, dry_run: bool = False, limit: int | None = None,
        financials_limit: int = 40, throttle: bool = True) -> dict:
    now = datetime.now(timezone.utc)
    run_ts = now.isoformat()
    conn = store.connect(cfg.db_path)
    store.init_db(conn)
    stock_store.init_stock_db(conn)
    stock_lt_store.init_stock_lt_db(conn)

    universe = stock_store.get_universe(conn)
    if limit:
        universe = universe[:limit]
    if not dry_run:
        _refresh_financials(conn, cfg, universe, financials_limit, throttle)

    q = _spy_quote(cfg)
    spy, spy_ts, spy_by_ts, spy_src = q if q else (None, None, {}, None)
    now_ms = int(now.timestamp() * 1000)

    candidates = []
    stale_n = 0
    for u in universe:
        tk = u["ticker"]
        bars = stock_store.recent_prices(conn, tk, 300)
        if len(bars) < _MIN_BARS:
            continue
        # Stored prices come from the daily swing collector; a series that stopped
        # updating (delisted/renamed/venue drop) must not be scored as current.
        if now_ms - bars[-1]["ts"] > _MAX_BAR_AGE_MS:
            stale_n += 1
            continue
        fin = stock_lt_store.get_financials(conn, tk)
        if not fin or not fin["periods"]:
            continue
        price = bars[-1]["close"]
        shares = fin["diluted_shares"]
        mktcap = (price * shares) if (price and shares) else None
        metrics = stock_fundamentals.compute(fin["periods"], price, market_cap=mktcap, shares=shares)
        if not metrics:
            continue
        mom, above = _momentum_trend(bars)
        candidates.append({"ticker": tk, "sector": u.get("sector"), "metrics": metrics,
                           "momentum_12_1": mom, "above_200dma": above, "price": price,
                           "last_ts": bars[-1]["ts"]})

    survivors, gated = stock_lt_scoring.rank_long_buys(candidates)

    # sector-median earnings yield among survivors (for the illustrative fair-value band)
    sector_ey: dict[str, list[float]] = {}
    for c in survivors:
        ey = (c["metrics"] or {}).get("earnings_yield")
        if ey is not None:
            sector_ey.setdefault(c.get("sector") or "?", []).append(ey)
    sector_median = {s: statistics.median(v) for s, v in sector_ey.items() if v}

    top_n = cfg.stock_lt_top_n
    signals, surfaced_tickers = [], set()
    for i, c in enumerate(survivors):
        rank_no = i + 1
        surfaced = rank_no <= top_n
        if surfaced:
            surfaced_tickers.add(c["ticker"])
        m = c["metrics"]
        fv = stock_lt_scoring.fair_value_band(c, sector_median.get(c.get("sector") or "?"))
        detail = {
            "value_rank": c["value_rank"], "quality_rank": c["quality_rank"],
            "momentum_rank": c["momentum_rank"], "sector": c.get("sector"),
            "momentum_12_1": (round(c["momentum_12_1"] * 100, 1) if c.get("momentum_12_1") is not None else None),
            "piotroski": (m.get("piotroski") or {}).get("score"),
            "altman": m.get("altman"), "fair_value": fv, "surfaced": surfaced,
            "metrics": {k: (round(m[k], 4) if isinstance(m.get(k), float) else m.get(k))
                        for k in ("earnings_yield", "ocf_yield", "sales_yield", "book_yield",
                                  "shareholder_yield", "gross_profitability", "roic",
                                  "operating_margin", "accruals", "asset_growth",
                                  "revenue_growth", "eps_growth", "debt_to_equity")},
        }
        signals.append({
            "ticker": c["ticker"], "rank": rank_no, "conviction": c["conviction"],
            "value_rank": c["value_rank"], "quality_rank": c["quality_rank"],
            "momentum_rank": c["momentum_rank"],
            "piotroski": (m.get("piotroski") or {}).get("score"),
            "altman_z": (m.get("altman") or {}).get("z"), "sector": c.get("sector"),
            "price": round(c["price"], 2), "surfaced": int(surfaced),
            "detail_json": json.dumps(detail, default=str),
        })

    # --- forward-test holdings vs SPY ---
    fired, deferred, forced = _manage_holdings(conn, cfg, run_ts, surfaced_tickers,
                                               survivors, candidates, spy, spy_by_ts,
                                               now, dry_run)

    # Benchmark basis honesty: derived from the venue's ACTUAL adjustment basis
    # (prices.VENUE_BASIS) — only stooq serves a total-return (split+dividend)
    # series today; every other venue is split-only, so a price-only excess
    # understates a value tilt's dividend yield vs SPY by roughly 1%/yr.
    basis = _benchmark_basis(spy_src)
    readings = {"massive": cfg.massive_active, "spy": spy, "spy_ts": spy_ts,
                "financials_cached": len(stock_lt_store.financials_freshness(conn)),
                "gated_out": len(gated), "stale_priced_n": stale_n,
                "deferred_closes_n": len(deferred),
                # closes force-resolved after >30d of deferral (dead/delisted names)
                # — priced from the last available date-matched pair, flagged here
                # so the forward-test reader can see they are data-gap artifacts.
                "forced_closes_n": len(forced), "forced_closes": forced,
                "benchmark_basis": basis, "spy_venue": spy_src,
                "benchmark_note": ("Excess vs SPY on venue-adjusted closes; dividends "
                                   "accrue only where the venue dividend-adjusts — "
                                   "price-only legs bias the measure AGAINST the strategy.")}
    if not dry_run:
        if spy is not None:
            store.set_meta(conn, "lt_spy_close", str(spy))   # for open-holding mark-to-market
        stock_lt_store.record_lt_run(conn, run_ts=run_ts, universe_n=len(universe),
                                     scored_n=len(candidates), survivors_n=len(survivors),
                                     readings=readings)
        stock_lt_store.record_lt_signals(conn, run_ts, signals)
    conn.close()

    summary = {"run_ts": run_ts, "universe_n": len(universe), "scored_n": len(candidates),
               "survivors_n": len(survivors), "spy": spy, "stale_priced_n": stale_n,
               "top": [{"rank": s["rank"], "ticker": s["ticker"], "conviction": s["conviction"],
                        "value": s["value_rank"], "quality": s["quality_rank"],
                        "mom": s["momentum_rank"], "piotroski": s["piotroski"],
                        "altman_z": s["altman_z"], "sector": s["sector"]}
                       for s in signals[:top_n]],
               "new_holdings": fired, "deferred_closes": deferred,
               "forced_closes": forced}
    return summary


def _manage_holdings(conn, cfg, run_ts, surfaced: set, survivors: list, candidates: list,
                     spy, spy_by_ts: dict, now, dry_run) -> tuple[list, list, list]:
    """Open holdings for new conviction names; close those that dropped out (excess
    vs SPY) using a DATE-MATCHED name/SPY close pair (nearest SPY bar within
    ``_SPY_MATCH_TOL_MS``), else defer the close to a later run. A close deferred
    past ``_MAX_DEFER_MS`` (dead/delisted name whose bars froze outside the SPY
    window) force-closes from the last available pair rather than staying open —
    and mispriced against today's SPY — forever. Dropped names are tagged
    'dropped_by_conviction' (still scored, fell off the list) or 'data_gap'
    (vanished from the scorable set — stale prices, missing financials) so the
    forward-test separates conviction exits from data artifacts.
    Returns (opened tickers, deferred-close tickers, force-closed tickers)."""
    if spy is None:
        return [], [], []
    price_of = {c["ticker"]: (c["price"], c.get("last_ts")) for c in candidates}
    conviction_of = {c["ticker"]: c.get("conviction", 0) for c in survivors}
    scored = set(price_of)
    opened: list = []
    deferred: list = []
    forced: list = []
    open_h = stock_lt_store.open_lt_holdings(conn)
    held = {h["ticker"]: h for h in open_h}
    now_ms = int(now.timestamp() * 1000)
    # Roll the split-guard anchor forward: while the entry bar is still within
    # price retention (~500 days before prune_stock deletes it), keep entry_close
    # on the venue's CURRENT adjustment basis. Without this the guard silently
    # falls back to the frozen entry for exactly the engine's natural multi-year
    # holds, re-booking the fake-excess-return bug the anchor exists to prevent.
    for h in held.values():
        if not h.get("entry_ts"):
            continue
        cur = stock_store.close_at(conn, h["ticker"], h["entry_ts"])
        if cur is not None and cur != h.get("entry_close"):
            h["entry_close"] = cur
            if not dry_run:
                stock_lt_store.update_lt_entry_close(conn, h["id"], cur)
    # open new (entry paired with the SPY close of the same bar date where available)
    for tk in surfaced:
        if tk not in held and tk in price_of and not dry_run:
            px, ts = price_of[tk]
            stock_lt_store.open_lt_holding(conn, ticker=tk, opened_run_ts=run_ts, opened_ts=now_ms,
                                           entry=px, spy_entry=spy_by_ts.get(ts, spy),
                                           conviction=conviction_of.get(tk, 0), entry_ts=ts,
                                           entry_close=px)
            opened.append(tk)
    # close dropped
    for tk, h in held.items():
        if tk in surfaced or dry_run:
            continue
        reason = "dropped_by_conviction" if tk in scored else "data_gap"
        px, ts = price_of.get(tk, (None, None))
        if px is None:
            bars = stock_store.recent_prices(conn, tk, 1)
            if bars:
                px, ts = bars[-1]["close"], bars[-1]["ts"]
        if px is None:
            deferred.append(tk)   # no exit price at all — nothing to pair
            continue
        # Split re-base guard: entry_close is the entry bar's close rolled to the
        # venue's CURRENT adjustment basis (refreshed every run above), so it
        # re-expresses the frozen entry in the same basis as the exit even after
        # the entry bar itself is pruned; close_at is the fallback for legacy rows
        # opened before the column existed. NO anchor at all -> defer rather than
        # silently book the frozen (possibly pre-split) entry.
        entry = h.get("entry_close")
        if entry is None and h.get("entry_ts"):
            entry = stock_store.close_at(conn, tk, h["entry_ts"])
        if not entry:
            deferred.append(tk)
            continue
        spy_exit = _nearest_spy(spy_by_ts, ts)
        force = False
        if spy_exit is None:
            # No date-matched pair (a phantom exit priced weeks ago against
            # today's SPY would corrupt the record). Defer — but bound it: a name
            # whose bars froze >30 days ago (delisting/acquisition) would defer
            # forever, so force-close from the nearest pair still available.
            if now_ms - ts <= _MAX_DEFER_MS:
                deferred.append(tk)
                continue
            spy_exit = _nearest_spy(spy_by_ts, ts, tol_ms=None) or spy
            reason = "data_gap"
            force = True
            log.warning("LT holding %s deferred past %d days — force-closing from "
                        "the last available date-matched pair", tk,
                        _MAX_DEFER_MS // 86_400_000)
        name_ret = (px / entry - 1) if entry else 0
        spy_ret = (spy_exit / h["spy_entry"] - 1) if h["spy_entry"] else 0
        stock_lt_store.close_lt_holding(conn, h["id"], closed_ts=now_ms, exit_price=px,
                                        spy_exit=spy_exit,
                                        excess_return=round(name_ret - spy_ret, 4),
                                        exit_reason=reason)
        if force:
            forced.append(tk)
    return opened, deferred, forced


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="Long-term stock long-buys collector.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=None, help="cap universe (testing)")
    p.add_argument("--financials-limit", type=int, default=40, help="max financials refreshed this run")
    p.add_argument("--no-throttle", action="store_true", help="skip the 5/min Massive throttle (testing)")
    args = p.parse_args(argv)
    cfg = load_config()
    try:
        summary = run(cfg, dry_run=args.dry_run, limit=args.limit,
                      financials_limit=args.financials_limit, throttle=not args.no_throttle)
        print(json.dumps(summary, indent=2, default=str))
        return 0
    except Exception:  # noqa: BLE001
        log.exception("stock LT collect failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())

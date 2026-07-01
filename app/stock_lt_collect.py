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


def _spy_close(cfg: Config) -> float | None:
    res = prices.daily_bars("SPY", cfg, limit=5)
    return res[0][-1][4] if res else None


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

    spy = _spy_close(cfg)

    candidates = []
    for u in universe:
        tk = u["ticker"]
        bars = stock_store.recent_prices(conn, tk, 300)
        if len(bars) < _MIN_BARS:
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
                           "momentum_12_1": mom, "above_200dma": above, "price": price})

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
    fired = _manage_holdings(conn, cfg, run_ts, surfaced_tickers, survivors, spy, now, dry_run)

    readings = {"massive": cfg.massive_active, "spy": spy,
                "financials_cached": len(stock_lt_store.financials_freshness(conn)),
                "gated_out": len(gated)}
    if not dry_run:
        if spy is not None:
            store.set_meta(conn, "lt_spy_close", str(spy))   # for open-holding mark-to-market
        stock_lt_store.record_lt_run(conn, run_ts=run_ts, universe_n=len(universe),
                                     scored_n=len(candidates), survivors_n=len(survivors),
                                     readings=readings)
        stock_lt_store.record_lt_signals(conn, run_ts, signals)
    conn.close()

    summary = {"run_ts": run_ts, "universe_n": len(universe), "scored_n": len(candidates),
               "survivors_n": len(survivors), "spy": spy,
               "top": [{"rank": s["rank"], "ticker": s["ticker"], "conviction": s["conviction"],
                        "value": s["value_rank"], "quality": s["quality_rank"],
                        "mom": s["momentum_rank"], "piotroski": s["piotroski"],
                        "altman_z": s["altman_z"], "sector": s["sector"]}
                       for s in signals[:top_n]],
               "new_holdings": fired}
    return summary


def _manage_holdings(conn, cfg, run_ts, surfaced: set, survivors: list, spy, now, dry_run) -> list:
    """Open holdings for new conviction names; close those that dropped out (excess vs SPY)."""
    if spy is None:
        return []
    price_of = {c["ticker"]: c["price"] for c in survivors}
    opened = []
    open_h = stock_lt_store.open_lt_holdings(conn)
    held = {h["ticker"]: h for h in open_h}
    now_ms = int(now.timestamp() * 1000)
    # open new
    for tk in surfaced:
        if tk not in held and tk in price_of and not dry_run:
            stock_lt_store.open_lt_holding(conn, ticker=tk, opened_run_ts=run_ts, opened_ts=now_ms,
                                           entry=price_of[tk], spy_entry=spy,
                                           conviction=next((c["conviction"] for c in survivors if c["ticker"] == tk), 0))
            opened.append(tk)
    # close dropped
    for tk, h in held.items():
        if tk not in surfaced and not dry_run:
            exit_price = price_of.get(tk)
            if exit_price is None:
                bars = stock_store.recent_prices(conn, tk, 1)
                exit_price = bars[-1]["close"] if bars else h["entry"]
            name_ret = (exit_price / h["entry"] - 1) if h["entry"] else 0
            spy_ret = (spy / h["spy_entry"] - 1) if h["spy_entry"] else 0
            stock_lt_store.close_lt_holding(conn, h["id"], closed_ts=now_ms, exit_price=exit_price,
                                            spy_exit=spy, excess_return=round(name_ret - spy_ret, 4))
    return opened


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

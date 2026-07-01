"""Walk-forward backtest of the stock swing archetypes -> seed win-rates.

Reuses the LIVE engine (``stock_scoring`` -> ``stock_levels`` -> ``stock_positions.reprice``)
so the backtest can't drift from production. For a sample of the universe it walks
each ticker's history day-by-day, opens a simulated position whenever an archetype
fires (no pyramiding per archetype), resolves it exactly like the live tracker, and
aggregates realized R by archetype.

This is IN-SAMPLE (survivorship-biased: only currently-listed names, current
universe) — a *seed prior*, honestly weaker than the live out-of-sample record that
``stock_calibrate`` later writes. Keyless (prices only); PEAD is added when a
Finnhub key is present.

    python -m scripts.stock_backtest --limit 40
    python -m scripts.stock_backtest --limit 120 --write   # write app/stock_st_winrates.json
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from app import stock_levels, stock_positions, stock_scoring
from app.config import load_config
from app.sources.stocks import earnings as earnings_src
from app.sources.stocks import prices, universe

log = logging.getLogger("stock-backtest")
_WARMUP = 210


def _bars(res) -> list[dict]:
    return [{"ts": b[0], "open": b[1], "high": b[2], "low": b[3], "close": b[4], "volume": b[5]}
            for b in res[0]]


def _earnings_by_ts(ticker: str, cfg) -> dict[int, dict]:
    """Map report_ts -> earnings row for PEAD in the backtest (empty without a key)."""
    if not cfg.finnhub_active:
        return {}
    rows = earnings_src.surprise_history(ticker, cfg.finnhub_api_key, limit=20)
    return {r["report_ts"]: r for r in rows if r.get("surprise_pct") is not None}


def backtest_ticker(ticker: str, bars: list[dict], cfg, earnings_map: dict[int, dict]) -> list[dict]:
    trades: list[dict] = []
    busy_until: dict[str, int] = {}   # archetype -> ts still in a trade
    for i in range(_WARMUP, len(bars) - 1):
        window = bars[:i + 1]
        feat = stock_scoring.features(window)
        if not feat or not stock_scoring.liquid(feat, cfg):
            continue
        # nearest earnings within the lookback window (for PEAD)
        earn = None
        if earnings_map:
            lo = feat["last_ts"] - cfg.stock_pead_lookback_days * 86400_000
            cands = [e for ts, e in earnings_map.items() if lo <= ts <= feat["last_ts"]]
            earn = max(cands, key=lambda e: e["report_ts"]) if cands else None
        cand = stock_scoring.pick_candidate(ticker, feat, window, earn, cfg)
        if cand is None:
            continue
        if cand.direction == "SELL" and not cfg.stock_allow_shorts:
            continue   # match live: Phase 1 is long-only
        if busy_until.get(cand.archetype, 0) >= feat["last_ts"]:
            continue
        lv = stock_levels.compute(cand.direction, feat["price"], feat["atr"], cand.archetype, cfg)
        if not lv:
            continue
        pos = {"direction": cand.direction, "entry": lv["entry"], "stop": lv["stop"],
               "t2": lv["t2"], "mfe_r": 0.0, "mae_r": 0.0}
        upd = stock_positions.reprice(pos, bars[i + 1:], "", lv["time_stop_days"],
                                      cost_bps=cfg.stock_cost_bps)
        if upd["status"] != "CLOSED":
            continue
        trades.append({"ticker": ticker, "archetype": cand.archetype,
                       "realized_r": upd["realized_r"], "exit_reason": upd["exit_reason"]})
        busy_until[cand.archetype] = upd["closed_ts"]
    return trades


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40, help="sample this many universe tickers")
    ap.add_argument("--write", action="store_true", help="write app/stock_st_winrates.json")
    args = ap.parse_args(argv)
    cfg = load_config()

    uni = universe.read_universe_file(cfg.stock_universe_path)[: args.limit]
    all_trades: list[dict] = []
    for u in uni:
        tk = u["ticker"]
        res = prices.daily_bars(tk, cfg, limit=600)
        if not res or len(res[0]) < _WARMUP + 30:
            continue
        bars = _bars(res)
        trades = backtest_ticker(tk, bars, cfg, _earnings_by_ts(tk, cfg))
        all_trades.extend(trades)
        log.info("%s: %d trades", tk, len(trades))

    summary = stock_positions.summarize(all_trades)
    winrates = {"generated_at": datetime.now(timezone.utc).isoformat(),
                "source": "backtest", "note": "In-sample walk-forward seed (survivorship-biased).",
                "archetypes": {k: {"n": v["n"], "win_rate": v["win_rate"],
                                   "expectancy_r": v["expectancy_r"]}
                               for k, v in summary["archetypes"].items() if v["n"]}}
    print(json.dumps({"overall": summary["overall"], "archetypes": summary["archetypes"],
                      "n_tickers": len(uni)}, indent=2))
    if args.write:
        out = Path(__file__).resolve().parents[1] / "app" / "stock_st_winrates.json"
        out.write_text(json.dumps(winrates, indent=2))
        log.info("wrote %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Stock swing-tracker collector entrypoint (cron: once/day after the US close).

Mirrors ``collect_once`` philosophy — a short, idempotent
fetch -> score -> decide -> persist cycle with SQLite as the only state — but the
shape is cross-sectional: rank the whole universe each close and surface the
strongest setups, each with entry/stop/targets/confidence, and advance the open
positions that ARE the forward-test.

    python -m app.stock_collect                 # live
    python -m app.stock_collect --dry-run       # compute & print; no notify, no DB write
    python -m app.stock_collect --dry-run --limit 25 --skip-insider   # fast smoke

Flags: --limit caps the universe (testing), --skip-insider/--skip-estimates/
--skip-congress skip the heavier/optional context layers.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import (notify, stock_confidence, stock_levels, stock_positions, stock_scoring,
               stock_store, store)
from .config import Config, load_config
from .sources.stocks import (congress, earnings, estimates, insider, prices, shortvol,
                             universe)

log = logging.getLogger("stock-collect")

_PRICE_BARS = 400          # ~18 months of daily bars per ticker
_INSIDER_LOOKBACK_DAYS = 90
_EST_SNAP_MIN_HOURS = 20   # don't re-snapshot estimates more than once/day


def _winrates() -> dict:
    try:
        return json.loads(Path(__file__).with_name("stock_st_winrates.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _structure_stop(bars: list[dict], report_ts: int, direction: str) -> float | None:
    """Swing low/high since the earnings report — the PEAD thesis-invalidation level."""
    seg = [b for b in bars if b["ts"] >= report_ts]
    if not seg:
        return None
    return min(b["low"] for b in seg) if direction == "BUY" else max(b["high"] for b in seg)


# --- Fetch stages ------------------------------------------------------------

def _sync_universe(conn, cfg: Config, dry_run: bool, limit: int | None) -> list[dict]:
    resolved = universe.resolve_universe(cfg.stock_universe_path, cfg.sec_user_agent)
    if not resolved:
        return stock_store.get_universe(conn)
    if not dry_run:
        stock_store.upsert_universe(conn, resolved)
    uni = [{"ticker": t, "name": n, "sector": s, "cik": c} for (t, n, s, c) in resolved]
    return uni[:limit] if limit else uni


def _fetch_prices(conn, cfg: Config, tickers: list[str], dry_run: bool) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    ok = 0
    for tk in tickers:
        res = prices.daily_bars(tk, cfg, limit=_PRICE_BARS)
        if not res:
            continue
        bars, src = res
        rows = [(b[0], b[1], b[2], b[3], b[4], b[5]) for b in bars]
        if not dry_run:
            stock_store.upsert_prices(conn, tk, rows, source=src)
        out[tk] = [{"ts": b[0], "open": b[1], "high": b[2], "low": b[3],
                    "close": b[4], "volume": b[5]} for b in bars]
        ok += 1
    log.info("prices: %d/%d tickers fetched", ok, len(tickers))
    return out


def _fetch_earnings(conn, cfg: Config, tickers: set[str], dry_run: bool) -> dict[str, dict]:
    if not cfg.finnhub_active:
        return {}
    today = datetime.now(timezone.utc)
    frm = (today - timedelta(days=cfg.stock_pead_lookback_days + 4)).strftime("%Y-%m-%d")
    to = today.strftime("%Y-%m-%d")
    rows = [r for r in earnings.earnings_calendar(cfg.finnhub_api_key, frm, to)
            if r["ticker"] in tickers]
    if not dry_run and rows:
        stock_store.upsert_earnings(conn, rows)
    latest: dict[str, dict] = {}
    for r in rows:  # keep the most recent report per ticker
        cur = latest.get(r["ticker"])
        if cur is None or r["report_ts"] > cur["report_ts"]:
            latest[r["ticker"]] = r
    log.info("earnings: %d reports in window across %d tickers", len(rows), len(latest))
    return latest


def _fetch_shortvol(conn, cfg: Config, tickers: set[str], dry_run: bool) -> dict[str, dict]:
    if not cfg.stock_shortvol_active:
        return {}
    res = shortvol.latest_short_volume(cfg.sec_user_agent)
    if not res:
        return {}
    ts, table = res
    out: dict[str, dict] = {}
    rows = []
    for tk in tickers:
        row = table.get(tk)
        if not row:
            continue
        ratio = shortvol.short_ratio(row)
        out[tk] = {**row, "short_ratio": ratio}
        rows.append({"ticker": tk, "ts": ts, "short_vol": row["short_vol"],
                     "short_exempt": row["short_exempt"], "total_vol": row["total_vol"]})
    if not dry_run and rows:
        stock_store.upsert_shortvol(conn, rows)
    log.info("shortvol: %d/%d tickers matched in FINRA file", len(out), len(tickers))
    return out


def _fetch_insider(conn, cfg: Config, universe_rows: list[dict], dry_run: bool) -> dict[str, dict]:
    if not cfg.stock_insider_active:
        return {}
    since = int((datetime.now(timezone.utc) - timedelta(days=_INSIDER_LOOKBACK_DAYS)).timestamp() * 1000)
    clusters: dict[str, dict] = {}
    n = 0
    for u in universe_rows:
        cik = u.get("cik")
        if not cik:
            continue
        rows = insider.insider_transactions(cik, u["ticker"], cfg.sec_user_agent, since)
        if rows:
            if not dry_run:
                stock_store.upsert_insider(conn, rows)
            # cluster read straight from the fetched rows (dry-run safe)
            buyers = {r["insider"] for r in rows if r["txn_code"] == "P" and r["insider"]}
            usd = sum(r["value"] or 0 for r in rows if r["txn_code"] == "P")
            if buyers:
                clusters[u["ticker"]] = {"buyers": len(buyers), "usd": usd,
                                         "any_officer": any(r["is_officer"] for r in rows),
                                         "any_director": any(r["is_director"] for r in rows)}
            n += 1
    log.info("insider: scanned %d CIKs, %d with buy-clusters", n, len(clusters))
    return clusters


def _snapshot_estimates(conn, cfg: Config, tickers: list[str]) -> dict[str, dict]:
    """Snapshot recommendation trends (accrues revision history) and return the
    revision delta per ticker where two snapshots now exist. Always writes (the
    accrual only works if persisted) — skipped entirely in dry-run by the caller."""
    if not cfg.finnhub_active:
        return {}
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    deltas: dict[str, dict] = {}
    for tk in tickers:
        rec = estimates.recommendation(tk, cfg.finnhub_api_key)
        if not rec:
            continue
        stock_store.record_estimate_snap(
            conn, ticker=tk, snap_ts=now_ms, period=rec.get("period"),
            strong_buy=rec.get("strong_buy"), buy=rec.get("buy"), hold=rec.get("hold"),
            sell=rec.get("sell"), strong_sell=rec.get("strong_sell"), eps_avg=None)
        d = estimates.revision_delta(stock_store.last_two_estimate_snaps(conn, tk))
        if d:
            deltas[tk] = d
    return deltas


def _market_regime(cfg: Config) -> str:
    """Broad-market trend from SPY vs its 200DMA (the 'don't fight the tape' gate)."""
    res = prices.daily_bars("SPY", cfg, limit=260)
    if not res:
        return "unknown"
    closes = [b[4] for b in res[0]]
    if len(closes) < 200:
        return "unknown"
    ma = sum(closes[-200:]) / 200
    return "bull" if closes[-1] >= ma else "bear"


# --- Position lifecycle ------------------------------------------------------

def _advance_positions(conn, cfg: Config, run_ts: str, bars_by_ticker: dict[str, list[dict]],
                       dry_run: bool) -> list[dict]:
    """Reprice every open position against new bars; close or update excursion."""
    events = []
    for pos in stock_store.open_positions(conn):
        bars = bars_by_ticker.get(pos["ticker"])
        if not bars:
            continue
        new_bars = [b for b in bars if b["ts"] > pos["opened_ts"]]
        if not new_bars:
            continue
        tstop = pos.get("time_stop_days") or cfg.stock_time_stop_days
        upd = stock_positions.reprice(pos, new_bars, run_ts, tstop, cost_bps=cfg.stock_cost_bps)
        if upd["status"] == "CLOSED":
            events.append({"ticker": pos["ticker"], "archetype": pos["archetype"],
                           **upd})
            if not dry_run:
                stock_store.close_position(conn, pos["id"], closed_run_ts=run_ts,
                                           closed_ts=upd["closed_ts"], exit_price=upd["exit_price"],
                                           realized_r=upd["realized_r"], exit_reason=upd["exit_reason"],
                                           mfe_r=upd["mfe_r"], mae_r=upd["mae_r"],
                                           gross_r=upd.get("gross_r"), cost_r=upd.get("cost_r"))
        elif not dry_run:
            stock_store.update_position_excursion(conn, pos["id"], upd["mfe_r"], upd["mae_r"])
    return events


# --- Main --------------------------------------------------------------------

def run(cfg: Config, *, dry_run: bool = False, limit: int | None = None,
        skip_insider: bool = False, skip_estimates: bool = False,
        skip_congress: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    run_ts = now.isoformat()
    conn = store.connect(cfg.db_path)
    store.init_db(conn)
    stock_store.init_stock_db(conn)

    universe_rows = _sync_universe(conn, cfg, dry_run, limit)
    tickers = [u["ticker"] for u in universe_rows]
    tset = set(tickers)

    # Also reprice any open-position tickers no longer in the (capped) universe.
    open_tks = {p["ticker"] for p in stock_store.open_positions(conn)}
    fetch_tks = list(dict.fromkeys(tickers + [t for t in open_tks if t not in tset]))

    bars_by_ticker = _fetch_prices(conn, cfg, fetch_tks, dry_run)
    earnings_by = _fetch_earnings(conn, cfg, tset, dry_run)
    shortvol_by = _fetch_shortvol(conn, cfg, tset, dry_run)
    insider_by = {} if skip_insider else _fetch_insider(conn, cfg, universe_rows, dry_run)
    revision_by = {} if (skip_estimates or dry_run) else _snapshot_estimates(conn, cfg, tickers)
    regime = _market_regime(cfg)

    if cfg.stock_congress_active and not skip_congress and not dry_run:
        try:
            since = int((now - timedelta(days=45)).timestamp() * 1000)
            congress.recent_house_trades(since, tset, cfg.sec_user_agent)  # accrual only for now
        except Exception:  # noqa: BLE001
            pass

    # --- Score the universe ---
    candidates: list = []
    universe_ret63: dict[str, float] = {}
    for u in universe_rows:
        tk = u["ticker"]
        bars = bars_by_ticker.get(tk)
        feat = stock_scoring.features(bars) if bars else None
        if not feat or not stock_scoring.liquid(feat, cfg):
            continue
        universe_ret63[tk] = feat.get("ret_63") or 0.0
        cand = stock_scoring.pick_candidate(tk, feat, bars, earnings_by.get(tk), cfg)
        if cand is None:
            continue
        # Phase 1 is long-only; skip short setups (e.g. negative-PEAD) unless enabled.
        if cand.direction == "SELL" and not cfg.stock_allow_shorts:
            continue
        sv = shortvol_by.get(tk)
        ctx_score, ctx_parts = stock_scoring.context_score(
            insider_by.get(tk), {"short_ratio": (sv or {}).get("short_ratio")} if sv else None,
            revision_by.get(tk))
        cand.context = ctx_score
        cand.detail["context"] = ctx_parts
        cand.detail["feat"] = {"price": feat["price"], "atr": feat["atr"],
                               "rsi": feat.get("rsi"), "sector": u.get("sector"),
                               "name": u.get("name")}
        cand._bars = bars  # stash for level structure-stop
        cand._feat = feat
        candidates.append(cand)

    ranked = stock_scoring.rank(candidates, regime, universe_ret63)
    winrates = _winrates()

    # Pass 1: levels + confidence + expected-value PRIORITY per candidate.
    records: list = []
    for c in ranked:
        feat = c._feat
        structure = None
        if c.archetype == "pead_drift" and c.detail.get("report_ts"):
            structure = _structure_stop(c._bars, c.detail["report_ts"], c.direction)
        lv = stock_levels.compute(c.direction, feat["price"], feat["atr"], c.archetype,
                                  cfg, structure_stop=structure)
        if lv is None:
            continue
        conf = stock_confidence.confidence(c, winrates)
        priority = stock_scoring.priority_score(c.composite, conf.get("expectancy_r"))
        records.append((priority, c, feat, lv, conf))
    # Rank by expected value so the documented edge (PEAD) surfaces ABOVE trending
    # momentum noise instead of being buried under it in a strong tape.
    records.sort(key=lambda r: r[0], reverse=True)

    # Pass 2: assign rank/surfaced, build signals, open positions for the top setups.
    signals: list[dict] = []
    to_alert: list[dict] = []
    for i, (priority, c, feat, lv, conf) in enumerate(records):
        rank_no = i + 1
        surfaced = rank_no <= cfg.stock_top_n
        detail = {**c.detail, "archetype_label": stock_scoring.ARCHETYPE_LABELS[c.archetype],
                  "confidence": conf, "levels": lv, "rel": round(c.rel, 3),
                  "regime": c.regime, "regime_state": regime, "surfaced": surfaced,
                  "priority": round(priority, 1),
                  "edge_class": ("edge" if stock_scoring.is_edge(c.archetype) else "unproven")}
        sig = {
            "ticker": c.ticker, "rank": rank_no, "direction": c.direction,
            "archetype": c.archetype, "composite": round(c.composite, 1),
            "confidence": conf["prob"],
            "pead": (round(c.primary, 3) if c.archetype == "pead_drift" else None),
            "technical": (round(c.primary, 3) if c.archetype != "pead_drift" else None),
            "insider": c.detail.get("context", {}).get("insider"),
            "shortvol": c.detail.get("context", {}).get("shortvol"),
            "revision": c.detail.get("context", {}).get("revision"),
            "price": round(feat["price"], 4), "entry": lv["entry"], "stop": lv["stop"],
            "t1": lv["t1"], "t2": lv["t2"], "atr": lv["atr"], "rr": lv["rr"],
            "detail_json": json.dumps(detail, default=str),
        }
        signals.append(sig)

        # Open a forward-test position for a surfaced NEW setup (cooldown + no dup).
        if surfaced:
            last = stock_store.last_stock_alert(conn, c.ticker, c.archetype)
            cooled = (last is None or
                      (feat["last_ts"] - (last["ts"] or 0)) >= cfg.stock_cooldown_days * 86400_000)
            has_open = stock_store.has_open_position(conn, c.ticker, c.archetype)
            if cooled and not has_open:
                if not dry_run:
                    stock_store.insert_position(
                        conn, ticker=c.ticker, opened_run_ts=run_ts, opened_ts=feat["last_ts"],
                        direction=c.direction, archetype=c.archetype, confidence=conf["prob"],
                        entry=lv["entry"], stop=lv["stop"], t1=lv["t1"], t2=lv["t2"], atr=lv["atr"],
                        time_stop_days=lv["time_stop_days"])
                to_alert.append({"sig": sig, "detail": detail, "ts": feat["last_ts"]})

    # --- Advance existing positions (the forward-test) ---
    pos_events = _advance_positions(conn, cfg, run_ts, bars_by_ticker, dry_run)

    # --- Persist run + signals ---
    readings = {"regime": regime, "universe_n": len(universe_rows), "scored_n": len(ranked),
                "layers": {"prices": bool(bars_by_ticker), "earnings": cfg.finnhub_active,
                           "insider": cfg.stock_insider_active and not skip_insider,
                           "shortvol": cfg.stock_shortvol_active,
                           "revision": bool(revision_by)},
                "price_source": cfg.stock_price_source}
    if not dry_run:
        stock_store.record_stock_run(conn, run_ts=run_ts, universe_n=len(universe_rows),
                                     scored_n=len(ranked), readings=readings)
        stock_store.record_stock_signals(conn, run_ts, signals)

    # --- Alert on the newly-opened top setups (owner-only, batched) ---
    fired = _maybe_alert(conn, cfg, to_alert, now, dry_run)

    if not dry_run:
        stock_store.prune_stock(conn, 500)
    conn.close()

    summary = {"run_ts": run_ts, "regime": regime, "universe_n": len(universe_rows),
               "scored_n": len(ranked), "top": [
                   {"rank": s["rank"], "ticker": s["ticker"], "dir": s["direction"],
                    "archetype": s["archetype"], "composite": s["composite"],
                    "confidence": s["confidence"], "entry": s["entry"], "stop": s["stop"],
                    "t1": s["t1"], "t2": s["t2"], "rr": s["rr"]}
                   for s in signals[:cfg.stock_top_n]],
               "closed_positions": pos_events, "alerts": fired}
    return summary


def _maybe_alert(conn, cfg: Config, to_alert: list[dict], now, dry_run: bool) -> list[str]:
    if not to_alert:
        return []
    lines = []
    for a in to_alert:
        s, d = a["sig"], a["detail"]
        conf = d["confidence"]
        lines.append(
            f"{s['direction']} {s['ticker']} — {d['archetype_label']} "
            f"(conf {conf['prob']*100:.0f}% {conf['label']})\n"
            f"  {d.get('catalyst','')}\n"
            f"  entry {s['entry']}  stop {s['stop']}  T1 {s['t1']}  T2 {s['t2']}  "
            f"({s['rr']}R, risk {d['levels'].get('risk_pct')}%)")
    title = f"Stock swing setups ({len(to_alert)})"
    body = ("New swing setups (alert-only, not advice; confidence is a backtested "
            "prior until the live tracker confirms it):\n\n" + "\n\n".join(lines))
    if dry_run:
        log.info("[dry-run] STOCK ALERT x%d\n%s\n%s", len(to_alert), title, body)
        return [f"{a['sig']['ticker']}/{a['sig']['archetype']}" for a in to_alert]
    ok = notify.send(cfg, title, body)
    fired = []
    if ok:
        for a in to_alert:
            s = a["sig"]
            stock_store.record_stock_alert(
                conn, ts=a["ts"], created_at=now.isoformat(), ticker=s["ticker"],
                archetype=s["archetype"], direction=s["direction"], entry=s["entry"],
                stop=s["stop"], t1=s["t1"], t2=s["t2"], confidence=s["confidence"],
                message=body, sent=True)
            fired.append(f"{s['ticker']}/{s['archetype']}")
    else:
        log.warning("stock alert send failed; will retry next run")
    return fired


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="Stock swing-tracker collector (one run).")
    p.add_argument("--dry-run", action="store_true",
                   help="compute and print; no notifications, no DB write")
    p.add_argument("--limit", type=int, default=None, help="cap the universe (testing)")
    p.add_argument("--skip-insider", action="store_true", help="skip the SEC insider layer")
    p.add_argument("--skip-estimates", action="store_true", help="skip Finnhub estimate snapshots")
    p.add_argument("--skip-congress", action="store_true", help="skip the congressional layer")
    args = p.parse_args(argv)
    cfg = load_config()
    try:
        summary = run(cfg, dry_run=args.dry_run, limit=args.limit,
                      skip_insider=args.skip_insider, skip_estimates=args.skip_estimates,
                      skip_congress=args.skip_congress)
        print(json.dumps(summary, indent=2, default=str))
        return 0
    except Exception:  # noqa: BLE001 - clean exit code for cron
        log.exception("stock collect run failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())

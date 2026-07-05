"""Stock swing-tracker collector entrypoint (cron: once/day after the US close).

Mirrors ``collect_once`` philosophy — a short, idempotent
fetch -> score -> decide -> persist cycle with SQLite as the only state — but the
shape is cross-sectional: rank the whole universe each close and surface the
strongest setups, each with entry/stop/targets/confidence, and advance the open
positions that ARE the forward-test.

    python -m app.stock_collect                 # live
    python -m app.stock_collect --dry-run       # compute & print; no notify, no DB write
    python -m app.stock_collect --dry-run --limit 25 --skip-insider   # fast smoke

Flags: --limit caps the universe (testing), --skip-insider/--skip-estimates skip the
heavier/optional context layers.
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from datetime import datetime, timedelta, timezone

from . import (stock_confidence, stock_levels, stock_positions,
               stock_scoring, stock_store, store)
from .config import Config, load_config
from .sources.stocks import earnings, estimates, insider, prices, universe

log = logging.getLogger("stock-collect")

_PRICE_BARS = 400          # ~18 months of daily bars per ticker
_INSIDER_LOOKBACK_DAYS = 90
_EST_SNAP_MIN_HOURS = 20   # don't re-snapshot estimates more than once/day
_MIN_PRICE_COVERAGE = 0.80   # below this the cross-section is biased -> degraded run, no ranking
_PENDING_EXPIRY_MS = 5 * 86_400_000   # ~3 trading days (incl. weekend): unfilled pending expires
_REBASE_TOL = 0.02         # entry-bar close drift beyond this => split/adjustment re-base
_YAHOO_DELAY_S = 0.15      # keyless-path politeness delay (+ jitter) between chart calls


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


def _bar_dicts(bars: list[tuple]) -> list[dict]:
    return [{"ts": b[0], "open": b[1], "high": b[2], "low": b[3],
             "close": b[4], "volume": b[5]} for b in bars]


def _fetch_prices(conn, cfg: Config, tickers: list[str], dry_run: bool
                  ) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Returns (bars per ticker, serving venue per ticker). The keyless Yahoo path
    gets a small jittered delay so 500+ sequential chart calls from one IP don't
    trip its rate limiter and silently bias the cross-section."""
    out: dict[str, list[dict]] = {}
    srcs: dict[str, str] = {}
    for tk in tickers:
        res = prices.daily_bars(tk, cfg, limit=_PRICE_BARS)
        if not res:
            continue
        bars, src = res
        rows = [(b[0], b[1], b[2], b[3], b[4], b[5]) for b in bars]
        if not dry_run:
            stock_store.upsert_prices(conn, tk, rows, source=src)
        out[tk] = _bar_dicts(bars)
        srcs[tk] = src
        if src == "yahoo":
            time.sleep(_YAHOO_DELAY_S + random.random() * 0.2)
    log.info("prices: %d/%d tickers fetched", len(out), len(tickers))
    return out, srcs


def _fetch_earnings(conn, cfg: Config, tickers: set[str], dry_run: bool
                    ) -> tuple[dict[str, dict], int]:
    """Returns (latest report per ticker, raw row count for the data-flow health read)."""
    if not cfg.finnhub_active:
        return {}, 0
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
    return latest, len(rows)


def _fetch_insider(conn, cfg: Config, universe_rows: list[dict], dry_run: bool
                   ) -> tuple[dict[str, dict], int, int]:
    """Returns (buy-clusters per ticker, CIKs attempted, CIKs that returned rows)."""
    if not cfg.stock_insider_active:
        return {}, 0, 0
    since = int((datetime.now(timezone.utc) - timedelta(days=_INSIDER_LOOKBACK_DAYS)).timestamp() * 1000)
    clusters: dict[str, dict] = {}
    attempted = 0
    ok = 0
    for u in universe_rows:
        cik = u.get("cik")
        if not cik:
            continue
        attempted += 1
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
            ok += 1
    log.info("insider: scanned %d/%d CIKs, %d with buy-clusters", ok, attempted, len(clusters))
    return clusters, attempted, ok


def _load_insider_clusters(conn, cfg: Config, tickers: list[str]) -> dict[str, dict]:
    """Read cached open-market BUY clusters from the store — populated out-of-band by
    the weekly ``stock_insider_scan`` cron. Lets the daily collector use insider
    context for PEAD ranking WITHOUT doing 500+ live SEC fetches on the hot path
    (the reason the daily cron runs ``--skip-insider`` in the first place)."""
    if not cfg.stock_insider_active:
        return {}
    since = int((datetime.now(timezone.utc) - timedelta(days=_INSIDER_LOOKBACK_DAYS)).timestamp() * 1000)
    out: dict[str, dict] = {}
    for tk in tickers:
        c = stock_store.insider_cluster(conn, tk, since)
        if c.get("buyers"):
            out[tk] = c
    return out


def _snapshot_estimates(conn, cfg: Config, tickers: list[str]) -> dict[str, dict]:
    """Snapshot recommendation trends (accrues revision history) and return the
    revision delta per ticker where two snapshots now exist. Writes when live (the
    accrual only works if persisted) — skipped entirely in dry-run by the caller.

    A ticker snapshotted within ``_EST_SNAP_MIN_HOURS`` is neither re-fetched nor
    re-snapshotted: a second same-day snapshot would make ``revision_delta`` compare
    two intraday reads and collapse the day-over-day shift to ~0."""
    if not cfg.finnhub_active:
        return {}
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    min_gap_ms = int(_EST_SNAP_MIN_HOURS * 3600_000)
    deltas: dict[str, dict] = {}
    for tk in tickers:
        snaps = stock_store.last_two_estimate_snaps(conn, tk)
        if snaps and now_ms - snaps[0]["snap_ts"] < min_gap_ms:
            d = estimates.revision_delta(snaps)   # fresh enough — reuse the accrued pair
            if d:
                deltas[tk] = d
            continue
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

def _bars_from_venue(cfg: Config, ticker: str, venue: str) -> list[dict] | None:
    """Re-fetch bars pinned to the position's entry venue so a position opened off
    (say) Yahoo's dividend-adjusted basis is never repriced against Alpaca's
    split-only basis. Fail-soft None."""
    try:
        res = prices.daily_bars(ticker, cfg, limit=_PRICE_BARS, venue=venue)
    except Exception:  # noqa: BLE001 - fail-soft (incl. venue param not supported)
        return None
    if not res:
        return None
    bars, src = res
    return _bar_dicts(bars) if src == venue else None


def _pinned_bars(cfg: Config, ticker: str, venue: str | None,
                 run_bars: list[dict], run_src: str | None) -> list[dict]:
    """This run's bars if they came from the pinned venue (or no venue is pinned),
    else a pinned re-fetch; falls back to the run bars, which the re-base check
    below then guards against a shifted adjustment basis."""
    if not venue or run_src == venue:
        return run_bars
    return _bars_from_venue(cfg, ticker, venue) or run_bars


def _advance_positions(conn, cfg: Config, run_ts: str, now_ms: int,
                       bars_by_ticker: dict[str, list[dict]],
                       src_by_ticker: dict[str, str], dry_run: bool) -> list[dict]:
    """Fill or expire PENDING setups, then reprice every open position against new
    bars — pinned to the entry venue and re-base-checked — closing or updating
    excursion. Fills happen at the NEXT bar's open (the price an alerted subscriber
    could actually get), never at the signal bar's close."""
    events: list[dict] = []
    to_reprice: list[tuple[dict, list[dict]]] = []
    just_filled: set[int] = set()

    # -- pending: fill at the next bar's open, or expire unfilled --
    for pos in stock_store.pending_positions(conn):
        tk = pos["ticker"]
        run_bars = bars_by_ticker.get(tk) or []
        bars = _pinned_bars(cfg, tk, pos.get("entry_venue"), run_bars, src_by_ticker.get(tk))
        new_bars = [b for b in bars if b["ts"] > pos["opened_ts"]]
        fill_bar = new_bars[0] if new_bars else None
        # Expired: no fill bar within the window (halted/delisted/data gap).
        if (fill_bar["ts"] if fill_bar else now_ms) - pos["opened_ts"] > _PENDING_EXPIRY_MS:
            if not dry_run:
                stock_store.expire_position(conn, pos["id"], closed_run_ts=run_ts,
                                            closed_ts=now_ms)
            events.append({"ticker": tk, "archetype": pos["archetype"],
                           "status": "EXPIRED", "exit_reason": "unfilled"})
            continue
        if fill_bar is None:
            continue   # still waiting for the next bar
        # Pending-gap re-base guard: atr/structure_stop were frozen in the SIGNAL
        # day's price basis. A split effective during the 1-3 day pending window
        # re-serves the whole series in post-split units, so the fill open would be
        # paired with a 10x-wrong ATR/stop. Compare the signal bar's close in the
        # fill-run series to the close stored at signal time and re-express the
        # frozen frame; if the signal bar vanished, the basis is unverifiable ->
        # expire the pending (data_gap), never guess.
        sig_close = pos.get("entry_bar_close")
        if sig_close:
            sig_bar = next((b for b in bars if b["ts"] == pos["opened_ts"]), None)
            if sig_bar is None or not sig_bar.get("close"):
                if not dry_run:
                    stock_store.expire_position(conn, pos["id"], closed_run_ts=run_ts,
                                                closed_ts=now_ms, reason="data_gap")
                events.append({"ticker": tk, "archetype": pos["archetype"],
                               "status": "EXPIRED", "exit_reason": "data_gap"})
                continue
            ratio = sig_bar["close"] / sig_close
            if abs(ratio - 1.0) > _REBASE_TOL:
                for k in ("atr", "structure_stop"):
                    if pos.get(k) is not None:
                        pos[k] = pos[k] * ratio
                log.info("re-based %s pending frame by %.4f before fill "
                         "(split/adjustment in the pending gap)", tk, ratio)
        lv = stock_levels.compute(pos["direction"], fill_bar["open"], pos.get("atr"),
                                  pos["archetype"], cfg,
                                  structure_stop=pos.get("structure_stop"))
        if lv is None:
            continue
        venue = pos.get("entry_venue") if bars is not run_bars else src_by_ticker.get(tk)
        if not dry_run:
            stock_store.fill_position(conn, pos["id"], filled_ts=fill_bar["ts"],
                                      entry=lv["entry"], stop=lv["stop"], t1=lv["t1"],
                                      t2=lv["t2"], entry_venue=venue,
                                      entry_bar_close=fill_bar["close"],
                                      last_reprice_ts=now_ms,
                                      atr=pos.get("atr"),
                                      structure_stop=pos.get("structure_stop"))
        just_filled.add(pos["id"])
        to_reprice.append(({**pos, "status": "OPEN", "filled_ts": fill_bar["ts"],
                            "entry": lv["entry"], "stop": lv["stop"], "t1": lv["t1"],
                            "t2": lv["t2"], "entry_venue": venue,
                            "entry_bar_close": fill_bar["close"]}, bars))

    # -- open: venue-pinned bars for repricing --
    for pos in stock_store.open_positions(conn):
        if pos["id"] in just_filled:
            continue   # already queued by the fill pass above (now status OPEN in DB)
        run_bars = bars_by_ticker.get(pos["ticker"])
        if not run_bars:
            continue
        bars = _pinned_bars(cfg, pos["ticker"], pos.get("entry_venue"), run_bars,
                            src_by_ticker.get(pos["ticker"]))
        to_reprice.append((pos, bars))

    for pos, bars in to_reprice:
        anchor_ts = pos.get("filled_ts") or pos["opened_ts"]
        # Re-base detection: venues retroactively adjust whole series for splits or
        # dividends; compare the entry bar's re-fetched close to the close stored at
        # fill time and rescale the frozen levels — otherwise a 10:1 split records a
        # fictitious -1R 'stop' straight into the track record.
        ref_close = pos.get("entry_bar_close")
        if ref_close:
            entry_bar = next((b for b in bars if b["ts"] == anchor_ts), None)
            if entry_bar is None or not entry_bar.get("close"):
                # entry bar vanished from the series -> basis unverifiable: void
                events.append({"ticker": pos["ticker"], "archetype": pos["archetype"],
                               "status": "CLOSED", "exit_reason": "rebased"})
                if not dry_run:
                    stock_store.void_position(conn, pos["id"], closed_run_ts=run_ts,
                                              closed_ts=now_ms)
                continue
            ratio = entry_bar["close"] / ref_close
            if abs(ratio - 1.0) > _REBASE_TOL:
                for k in ("entry", "stop", "t1", "t2", "atr"):
                    if pos.get(k) is not None:
                        pos[k] = pos[k] * ratio
                pos["entry_bar_close"] = entry_bar["close"]
                log.info("re-based %s levels by %.4f (split/adjustment)", pos["ticker"], ratio)
                if not dry_run:
                    stock_store.rebase_position(conn, pos["id"], entry=pos["entry"],
                                                stop=pos["stop"], t1=pos["t1"], t2=pos["t2"],
                                                atr=pos.get("atr"),
                                                entry_bar_close=pos["entry_bar_close"])
        # Filled positions include the fill bar itself (entry is its open; the rest
        # of that session can hit the stop/target — matches the backtest). Legacy
        # rows without filled_ts keep the old close-entry anchor.
        if pos.get("filled_ts"):
            new_bars = [b for b in bars if b["ts"] >= anchor_ts]
        else:
            new_bars = [b for b in bars if b["ts"] > anchor_ts]
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
            stock_store.update_position_excursion(conn, pos["id"], upd["mfe_r"], upd["mae_r"],
                                                  last_reprice_ts=now_ms)
    return events


# --- Main --------------------------------------------------------------------

def run(cfg: Config, *, dry_run: bool = False, limit: int | None = None,
        skip_insider: bool = False, skip_estimates: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    run_ts = now.isoformat()
    conn = store.connect(cfg.db_path)
    store.init_db(conn)
    stock_store.init_stock_db(conn)

    universe_rows = _sync_universe(conn, cfg, dry_run, limit)
    tickers = [u["ticker"] for u in universe_rows]
    tset = set(tickers)

    # Also reprice any open/pending-position tickers no longer in the (capped) universe.
    open_tks = {p["ticker"] for p in
                stock_store.open_positions(conn) + stock_store.pending_positions(conn)}
    fetch_tks = list(dict.fromkeys(tickers + [t for t in open_tks if t not in tset]))

    bars_by_ticker, src_by_ticker = _fetch_prices(conn, cfg, fetch_tks, dry_run)

    # Partial-sweep gate: ranking a biased subset (e.g. Yahoo rate-limited half the
    # universe) corrupts the cross-sectional percentiles AND silently drops setups,
    # so a low-coverage run advances positions but does NOT rank or alert.
    fetched_uni = sum(1 for t in tickers if t in bars_by_ticker)
    coverage = (fetched_uni / len(tickers)) if tickers else 0.0
    degraded = coverage < _MIN_PRICE_COVERAGE
    if degraded:
        log.warning("price coverage %.0f%% < %.0f%% — degraded run: skipping ranking/alerts",
                    coverage * 100, _MIN_PRICE_COVERAGE * 100)

    earnings_by, earnings_rows_n = _fetch_earnings(conn, cfg, tset, dry_run)
    if skip_insider:
        # Insider is ingested out-of-band by the weekly `stock_insider_scan` cron;
        # read its cached clusters here (cheap SQLite) instead of 500+ live SEC hits.
        insider_by = _load_insider_clusters(conn, cfg, tickers)
        insider_attempted, insider_ok = 0, len(insider_by)
    else:
        insider_by, insider_attempted, insider_ok = _fetch_insider(conn, cfg, universe_rows, dry_run)
    # Estimate snapshots are 1 Finnhub call/ticker -> only snapshot names that just
    # REPORTED (revision-confirmation matters most post-earnings) so the free 60/min
    # limit isn't blown by the full universe.
    revision_by = {} if (skip_estimates or dry_run) else _snapshot_estimates(
        conn, cfg, list(earnings_by.keys()))
    regime = _market_regime(cfg)

    # --- Score the universe (skipped entirely on a degraded sweep) ---
    candidates: list = []
    universe_ret63: dict[str, float] = {}
    for u in (universe_rows if not degraded else []):
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
        ctx_score, ctx_parts = stock_scoring.context_score(
            insider_by.get(tk), revision_by.get(tk))
        cand.context = ctx_score
        cand.detail["context"] = ctx_parts
        cand.detail["feat"] = {"price": feat["price"], "atr": feat["atr"],
                               "rsi": feat.get("rsi"), "sector": u.get("sector"),
                               "name": u.get("name")}
        cand._bars = bars  # stash for level structure-stop
        cand._feat = feat
        candidates.append(cand)

    ranked = stock_scoring.rank(candidates, regime, universe_ret63)
    # P3 retirement: stock_st_winrates.json is archived (the honest recalibration
    # measured every cell not-significant), so confidence runs on its built-in
    # PRIORs — a recorded label, never displayed as measured. archetype_maturity
    # over {} is always "forward", matching the artifact's own verdict.
    winrates: dict = {}

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
        records.append((priority, c, feat, lv, conf, structure))
    # Rank by expected value so the documented edge (PEAD) surfaces ABOVE trending
    # momentum noise instead of being buried under it in a strong tape.
    records.sort(key=lambda r: r[0], reverse=True)

    # Pass 2: assign rank/surfaced, build signals, open positions for the top setups.
    signals: list[dict] = []
    to_alert: list[dict] = []
    for i, (priority, c, feat, lv, conf, structure) in enumerate(records):
        rank_no = i + 1
        surfaced = rank_no <= cfg.stock_top_n
        detail = {**c.detail, "archetype_label": stock_scoring.ARCHETYPE_LABELS[c.archetype],
                  "confidence": conf, "levels": lv, "rel": round(c.rel, 3),
                  "regime": c.regime, "regime_state": regime, "surfaced": surfaced,
                  "priority": round(priority, 1),
                  # edge/forward derives from the measured win-rates cell, not a
                  # hardcoded archetype set (see stock_confidence.archetype_maturity).
                  "edge_class": stock_confidence.archetype_maturity(c.archetype, winrates)}
        sig = {
            "ticker": c.ticker, "rank": rank_no, "direction": c.direction,
            "archetype": c.archetype, "composite": round(c.composite, 1),
            "confidence": conf["prob"],
            "pead": (round(c.primary, 3) if c.archetype == "pead_drift" else None),
            "technical": (round(c.primary, 3) if c.archetype != "pead_drift" else None),
            "insider": c.detail.get("context", {}).get("insider"),
            "revision": c.detail.get("context", {}).get("revision"),
            "price": round(feat["price"], 4), "entry": lv["entry"], "stop": lv["stop"],
            "t1": lv["t1"], "t2": lv["t2"], "atr": lv["atr"], "rr": lv["rr"],
            "detail_json": json.dumps(detail, default=str),
        }
        signals.append(sig)

        # Open a PENDING forward-test position for a surfaced NEW setup (cooldown +
        # no dup). The fill happens on the NEXT bar's open — the signal-close price
        # is unattainable for anyone acting on an after-close alert.
        if surfaced:
            last = stock_store.last_stock_alert(conn, c.ticker, c.archetype)
            cooled = (last is None or
                      (feat["last_ts"] - (last["ts"] or 0)) >= cfg.stock_cooldown_days * 86400_000)
            has_open = stock_store.has_open_position(conn, c.ticker, c.archetype)
            if cooled and not has_open:
                if not dry_run:
                    # entry_bar_close = the SIGNAL bar's close: the anchor the fill
                    # pass uses to detect a split during the pending gap.
                    stock_store.insert_position(
                        conn, ticker=c.ticker, opened_run_ts=run_ts, opened_ts=feat["last_ts"],
                        direction=c.direction, archetype=c.archetype, confidence=conf["prob"],
                        entry=lv["entry"], stop=lv["stop"], t1=lv["t1"], t2=lv["t2"], atr=lv["atr"],
                        time_stop_days=lv["time_stop_days"], status="PENDING",
                        structure_stop=structure, entry_venue=src_by_ticker.get(c.ticker),
                        entry_bar_close=feat["price"])
                to_alert.append({"sig": sig, "detail": detail, "ts": feat["last_ts"]})

    # --- Advance existing positions (the forward-test) ---
    pos_events = _advance_positions(conn, cfg, run_ts, now_ms, bars_by_ticker,
                                    src_by_ticker, dry_run)

    # --- Persist run + signals ---
    venue_counts: dict[str, int] = {}
    for s in src_by_ticker.values():
        venue_counts[s] = venue_counts.get(s, 0) + 1
    counts = {
        "universe_n": len(universe_rows),
        "prices_attempted": len(fetch_tks),
        "prices_fetched": len(bars_by_ticker),
        "earnings_rows": earnings_rows_n if cfg.finnhub_active else None,
        "insider_attempted": (insider_attempted
                              if (cfg.stock_insider_active and not skip_insider) else None),
        "insider_ok": insider_ok if (cfg.stock_insider_active and not skip_insider) else None,
    }
    readings = {"regime": regime, "universe_n": len(universe_rows), "scored_n": len(ranked),
                "coverage": round(coverage, 3), "degraded": degraded, "counts": counts,
                "layers": {"prices": bool(bars_by_ticker), "earnings": cfg.finnhub_active,
                           # active whenever we have insider context to score with —
                           # cached from the weekly scan even when the daily run skips
                           # the live fetch.
                           "insider": cfg.stock_insider_active and bool(insider_by),
                           "revision": bool(revision_by)},
                "price_source": cfg.stock_price_source,   # config preference
                "price_venues": venue_counts}             # venues actually used
    if not dry_run:
        stock_store.record_stock_run(conn, run_ts=run_ts, universe_n=len(universe_rows),
                                     scored_n=len(ranked), readings=readings)
        stock_store.record_stock_signals(conn, run_ts, signals)

    # --- Alerts: retry last run's failed sends once, then send the new batch ---
    resent = _retry_unsent_alerts(conn, cfg, dry_run)
    fired = _maybe_alert(conn, cfg, to_alert, now, dry_run)

    if not dry_run:
        stock_store.prune_stock(conn, 500)
    conn.close()

    summary = {"run_ts": run_ts, "regime": regime, "universe_n": len(universe_rows),
               "scored_n": len(ranked), "coverage": round(coverage, 3),
               "degraded": degraded, "top": [
                   {"rank": s["rank"], "ticker": s["ticker"], "dir": s["direction"],
                    "archetype": s["archetype"], "composite": s["composite"],
                    "confidence": s["confidence"], "entry": s["entry"], "stop": s["stop"],
                    "t1": s["t1"], "t2": s["t2"], "rr": s["rr"]}
                   for s in signals[:cfg.stock_top_n]],
               "closed_positions": pos_events, "alerts": fired, "alerts_resent": resent}
    return summary


def _maybe_alert(conn, cfg: Config, to_alert: list[dict], now, dry_run: bool) -> list[str]:
    """Record new surfaced setups — WITHOUT an instant send (§4 fatigue budget).

    The redesign reserves instant push for ACT/RISK/FAIL, and swing setups are
    none of those (no verified edge — recording only). The alert row is still
    the canonical "we surfaced this" record: it arms the cooldown, opens the
    forward-test position, and shows on the dashboard's recording surfaces.
    No confidence %/maturity stamp in the stored message either — the retired
    calibration measured coin-flip, so those numbers were false precision.
    """
    if not to_alert:
        return []
    lines = [
        f"{a['sig']['direction']} {a['sig']['ticker']} — {a['detail']['archetype_label']}\n"
        f"  {a['detail'].get('catalyst', '')}\n"
        f"  entry {a['sig']['entry']}  stop {a['sig']['stop']}  "
        f"T1 {a['sig']['t1']}  T2 {a['sig']['t2']}  "
        f"({a['sig']['rr']}R, risk {a['detail']['levels'].get('risk_pct')}%)"
        for a in to_alert]
    body = ("Recorded swing setups (recording only — no verified edge, no instant "
            "alert; see the dashboard):\n\n" + "\n\n".join(lines))
    if dry_run:
        log.info("[dry-run] STOCK SETUPS RECORDED x%d\n%s", len(to_alert), body)
        return [f"{a['sig']['ticker']}/{a['sig']['archetype']}" for a in to_alert]
    # sent=True: the row IS the record now (no transport involved), and it must
    # arm the cooldown exactly as the old delivered-alert path did.
    fired = []
    for a in to_alert:
        s = a["sig"]
        stock_store.record_stock_alert(
            conn, ts=a["ts"], created_at=now.isoformat(), ticker=s["ticker"],
            archetype=s["archetype"], direction=s["direction"], entry=s["entry"],
            stop=s["stop"], t1=s["t1"], t2=s["t2"], confidence=s["confidence"],
            message=body, sent=True)
        fired.append(f"{s['ticker']}/{s['archetype']}")
    log.info("recorded %d swing setups (no instant alert — §4 fatigue budget)",
             len(fired))
    return fired


def _retry_unsent_alerts(conn, cfg: Config, dry_run: bool) -> list[str]:
    """Drain legacy sent=0 rows from before the instant-send retirement — mark
    them sent (the row is the record) so the cooldown arms; nothing is emailed."""
    rows = stock_store.unsent_stock_alerts(conn)
    if not rows or dry_run:
        return []
    for r in rows:
        stock_store.mark_stock_alert_retry(conn, r["id"], sent=True)
    log.info("marked %d legacy queued alerts as recorded (instant send retired)",
             len(rows))
    return [f"{r['ticker']}/{r['archetype']}" for r in rows]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="Stock swing-tracker collector (one run).")
    p.add_argument("--dry-run", action="store_true",
                   help="compute and print; no notifications, no DB write")
    p.add_argument("--limit", type=int, default=None, help="cap the universe (testing)")
    p.add_argument("--skip-insider", action="store_true", help="skip the SEC insider layer")
    p.add_argument("--skip-estimates", action="store_true", help="skip Finnhub estimate snapshots")
    args = p.parse_args(argv)
    cfg = load_config()
    try:
        summary = run(cfg, dry_run=args.dry_run, limit=args.limit,
                      skip_insider=args.skip_insider, skip_estimates=args.skip_estimates)
        print(json.dumps(summary, indent=2, default=str))
        return 0
    except Exception:  # noqa: BLE001 - clean exit code for cron
        log.exception("stock collect run failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())

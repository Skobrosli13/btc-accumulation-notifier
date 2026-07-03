"""Tests for the stock swing tracker — pure engine (no network) + store round-trip."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from app import (notify, stock_api, stock_collect, stock_confidence, stock_levels,
                 stock_positions, stock_scoring, stock_store, store)
from app.config import load_config

CFG = load_config()
DAY = 86_400_000


def _conn_mem():
    conn = store.connect(":memory:")
    store.init_db(conn)
    stock_store.init_stock_db(conn)
    return conn


def _bars(closes: list[float], vol: float = 2_000_000.0, start_ts: int = 1_600_000_000_000):
    """Build OHLCV daily bars from a close series (H/L padded ±0.5%)."""
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        hi = max(c, prev) * 1.005
        lo = min(c, prev) * 0.995
        out.append({"ts": start_ts + i * DAY, "open": prev, "high": hi, "low": lo,
                    "close": c, "volume": vol})
        prev = c
    return out


def _uptrend(n=220, base=50.0, step=0.4):
    return [base + i * step for i in range(n)]


# --- features + archetypes ---------------------------------------------------

def test_features_uptrend():
    f = stock_scoring.features(_bars(_uptrend()))
    assert f is not None
    assert f["above_200"] and f["above_50"]
    assert f["ret_63"] > 0
    assert f["atr"] and f["atr"] > 0


def test_features_too_short_returns_none():
    assert stock_scoring.features(_bars(_uptrend(30))) is None


def test_momentum_fires_on_uptrend_not_downtrend():
    # a realistic uptrend with periodic pullbacks (a perfectly linear ramp reads RSI 100,
    # which the "don't chase overbought" guard correctly rejects)
    closes = [50 + i * 0.5 - (2.0 if i % 4 == 0 else 0.0) for i in range(220)]
    up = stock_scoring.momentum_candidate("X", stock_scoring.features(_bars(closes)), CFG)
    assert up is not None and up.direction == "BUY" and up.archetype == "momentum"
    down = _uptrend()[::-1]  # strictly declining
    f = stock_scoring.features(_bars(down))
    assert stock_scoring.momentum_candidate("X", f, CFG) is None


def test_meanrev_fires_on_oversold_dip_in_uptrend():
    # long uptrend then a sharp multi-day drop to force RSI < oversold while price stays > 200DMA
    closes = _uptrend(200, base=50, step=0.6) + [170, 158, 148, 140, 134]
    f = stock_scoring.features(_bars(closes))
    assert f["rsi"] is not None and f["rsi"] < CFG.st_rsi_oversold
    c = stock_scoring.meanrev_candidate("X", f, CFG)
    assert c is not None and c.archetype == "mean_reversion" and c.direction == "BUY"


def test_momentum_and_meanrev_demoted_from_pick_candidate():
    # Phase 0 §0.4: momentum/mean_reversion still COMPUTE (features + candidate
    # functions), but pick_candidate no longer surfaces them as live setups.
    closes = [50 + i * 0.5 - (2.0 if i % 4 == 0 else 0.0) for i in range(220)]
    bars = _bars(closes)
    feat = stock_scoring.features(bars)
    assert stock_scoring.momentum_candidate("X", feat, CFG) is not None
    assert stock_scoring.pick_candidate("X", feat, bars, None, CFG) is None  # no setup

    mr_closes = _uptrend(200, base=50, step=0.6) + [170, 158, 148, 140, 134]
    mr_bars = _bars(mr_closes)
    mr_feat = stock_scoring.features(mr_bars)
    assert stock_scoring.meanrev_candidate("X", mr_feat, CFG) is not None
    assert stock_scoring.pick_candidate("X", mr_feat, mr_bars, None, CFG) is None


def test_pick_candidate_still_surfaces_pead():
    bars, report_ts = _pead_scenario(0.06)
    good = {"report_ts": report_ts, "surprise_pct": 9.0, "hour": "", "actual": 1.1, "estimate": 1.0}
    c = stock_scoring.pick_candidate("X", stock_scoring.features(bars), bars, good, CFG)
    assert c is not None and c.archetype == "pead_drift"


def test_pead_requires_confirming_reaction():
    closes = _uptrend(210, base=40, step=0.2)
    closes[-6] = closes[-7] * 1.06  # +6% reaction day (report at index -6)
    for j in range(-5, 0):
        closes[j] = closes[j - 1] * 1.005
    bars = _bars(closes)
    report_ts = bars[-6]["ts"]
    good = {"report_ts": report_ts, "surprise_pct": 9.0, "hour": "", "actual": 1.1, "estimate": 1.0}
    c = stock_scoring.pead_candidate("X", stock_scoring.features(bars), bars, good, CFG)
    assert c is not None and c.archetype == "pead_drift" and c.direction == "BUY"
    # Mixed sign (positive surprise, negative reaction) -> no clean drift -> None
    mixed_closes = list(closes); mixed_closes[-6] = mixed_closes[-7] * 0.94
    mbars = _bars(mixed_closes)
    mixed = {**good, "report_ts": mbars[-6]["ts"]}
    assert stock_scoring.pead_candidate("X", stock_scoring.features(mbars), mbars, mixed, CFG) is None


def test_small_surprise_ignored():
    bars = _bars(_uptrend(210))
    small = {"report_ts": bars[-5]["ts"], "surprise_pct": 1.0, "hour": "", "actual": 1, "estimate": 1}
    assert stock_scoring.pead_candidate("X", stock_scoring.features(bars), bars, small, CFG) is None


def _pead_scenario(reaction, n=210, step=0.2, vol=2_000_000.0):
    """Uptrend with an earnings reaction bar at index n-6; returns (bars, report_ts)."""
    closes = _uptrend(n, base=40, step=step)
    closes[n - 6] = closes[n - 7] * (1 + reaction)
    for j in range(n - 5, n):
        closes[j] = closes[j - 1] * 1.003
    b = _bars(closes, vol=vol)
    return b, b[n - 6]["ts"]


def test_pead_sue_bigger_reaction_scores_higher():
    # Same EPS surprise, but a bigger reaction relative to the stock's vol (higher SUE)
    # is a stronger drift setup.
    big_bars, ts_b = _pead_scenario(0.08)
    small_bars, ts_s = _pead_scenario(0.015)
    e_big = {"report_ts": ts_b, "surprise_pct": 8.0, "hour": "", "rev_surprise_pct": None}
    e_small = {"report_ts": ts_s, "surprise_pct": 8.0, "hour": "", "rev_surprise_pct": None}
    c_big = stock_scoring.pead_candidate("X", stock_scoring.features(big_bars), big_bars, e_big, CFG)
    c_small = stock_scoring.pead_candidate("X", stock_scoring.features(small_bars), small_bars, e_small, CFG)
    assert c_big and c_small
    assert c_big.primary > c_small.primary
    assert c_big.detail["reaction_sigma"] > c_small.detail["reaction_sigma"]


def test_pead_revenue_confluence():
    bars, ts = _pead_scenario(0.06)
    feat = stock_scoring.features(bars)
    agree = {"report_ts": ts, "surprise_pct": 8.0, "hour": "", "rev_surprise_pct": 5.0}
    diverge = {"report_ts": ts, "surprise_pct": 8.0, "hour": "", "rev_surprise_pct": -5.0}
    c_agree = stock_scoring.pead_candidate("X", feat, bars, agree, CFG)
    c_div = stock_scoring.pead_candidate("X", feat, bars, diverge, CFG)
    assert c_agree.primary > c_div.primary   # EPS+revenue beat drifts better than EPS-beat/rev-miss


# --- levels ------------------------------------------------------------------

def test_levels_buy_geometry():
    lv = stock_levels.compute("BUY", 100.0, 2.0, "momentum", CFG)
    assert lv["stop"] < lv["entry"] < lv["t1"] < lv["t2"]
    assert lv["rr"] and lv["rr"] > 0
    assert lv["time_stop_days"] == stock_levels.ARCHETYPE_LEVELS["momentum"][3]


def test_levels_sell_mirrored():
    lv = stock_levels.compute("SELL", 100.0, 2.0, "pead_drift", CFG)
    assert lv["stop"] > lv["entry"] > lv["t1"] > lv["t2"]


def test_structure_stop_only_tightens():
    loose = stock_levels.compute("BUY", 100.0, 5.0, "pead_drift", CFG)  # atr stop = 90
    tight = stock_levels.compute("BUY", 100.0, 5.0, "pead_drift", CFG, structure_stop=95.0)
    assert tight["stop"] == 95.0 and tight["stop"] > loose["stop"]
    # a LOOSER structure stop is ignored (never widens risk)
    ignored = stock_levels.compute("BUY", 100.0, 5.0, "pead_drift", CFG, structure_stop=80.0)
    assert ignored["stop"] == loose["stop"]


def test_levels_none_without_atr():
    assert stock_levels.compute("BUY", 100.0, None, "momentum", CFG) is None


# --- position repricer -------------------------------------------------------

def _pos(direction="BUY", entry=100.0, stop=95.0, t2=110.0):
    return {"direction": direction, "entry": entry, "stop": stop, "t2": t2, "mfe_r": 0, "mae_r": 0}


def test_reprice_stop_hit():
    bars = [{"ts": DAY, "open": 99, "high": 101, "low": 94, "close": 96}]  # low pierces stop 95
    u = stock_positions.reprice(_pos(), bars, "", 12)
    assert u["status"] == "CLOSED" and u["exit_reason"] == "stop"
    assert round(u["realized_r"], 2) == -1.0   # opened above the stop -> fills AT the stop


def test_reprice_gap_through_stop_fills_at_open():
    # bar OPENS below the stop: the stop price is unattainable, fill at the open
    bars = [{"ts": DAY, "open": 90, "high": 92, "low": 88, "close": 91}]
    u = stock_positions.reprice(_pos(), bars, "", 12)   # entry 100 stop 95 risk 5
    assert u["exit_reason"] == "stop" and u["exit_price"] == 90
    assert round(u["realized_r"], 2) == -2.0   # (90-100)/5, worse than -1R


def test_reprice_gap_through_stop_sell_mirrored():
    pos = _pos("SELL", entry=100.0, stop=105.0, t2=90.0)
    bars = [{"ts": DAY, "open": 110, "high": 112, "low": 108, "close": 111}]
    u = stock_positions.reprice(pos, bars, "", 12)
    assert u["exit_reason"] == "stop" and u["exit_price"] == 110   # max(open, stop)
    assert round(u["realized_r"], 2) == -2.0


def test_reprice_stop_without_open_falls_back_to_stop_price():
    # legacy bar dicts without 'open' keep the old stop-price fill
    bars = [{"ts": DAY, "high": 101, "low": 94, "close": 96}]
    u = stock_positions.reprice(_pos(), bars, "", 12)
    assert u["exit_reason"] == "stop" and u["exit_price"] == 95


def test_reprice_target_hit():
    bars = [{"ts": DAY, "high": 111, "low": 99, "close": 108}]  # high reaches t2 110
    u = stock_positions.reprice(_pos(), bars, "", 12)
    assert u["status"] == "CLOSED" and u["exit_reason"] == "t2"
    assert round(u["realized_r"], 2) == 2.0   # (110-100)/(100-95)


def test_reprice_stop_wins_same_bar_tie():
    bars = [{"ts": DAY, "high": 111, "low": 94, "close": 105}]  # touches both -> stop first
    u = stock_positions.reprice(_pos(), bars, "", 12)
    assert u["exit_reason"] == "stop"


def test_reprice_time_stop():
    bars = [{"ts": i * DAY, "high": 101, "low": 99, "close": 100.5} for i in range(1, 6)]
    u = stock_positions.reprice(_pos(), bars, "", time_stop_days=3)
    assert u["status"] == "CLOSED" and u["exit_reason"] == "time"


def test_reprice_still_open():
    bars = [{"ts": DAY, "high": 101, "low": 99, "close": 100.5}]
    u = stock_positions.reprice(_pos(), bars, "", time_stop_days=12)
    assert u["status"] == "OPEN"


def test_reprice_cost_aware_net_below_gross():
    # t2 hit: gross +2R; a 10bps round-trip cost shaves cost_r off the net.
    bars = [{"ts": DAY, "high": 111, "low": 99, "close": 108}]  # entry100 stop95 t2110 risk5
    u = stock_positions.reprice(_pos(), bars, "", 12, cost_bps=10)
    assert u["gross_r"] == 2.0
    assert u["cost_r"] > 0 and u["realized_r"] == round(2.0 - u["cost_r"], 3)
    assert u["realized_r"] < u["gross_r"]


def test_reprice_cost_bites_tight_stops_harder():
    wide = stock_positions.reprice(_pos(entry=100, stop=90, t2=120),
                                   [{"ts": DAY, "high": 121, "low": 99, "close": 118}], "", 12, cost_bps=10)
    tight = stock_positions.reprice(_pos(entry=100, stop=98, t2=104),
                                    [{"ts": DAY, "high": 105, "low": 99, "close": 104}], "", 12, cost_bps=10)
    assert tight["cost_r"] > wide["cost_r"]   # same cost %, smaller risk -> more R lost


def test_reprice_zero_cost_default_unchanged():
    bars = [{"ts": DAY, "high": 111, "low": 99, "close": 108}]
    u = stock_positions.reprice(_pos(), bars, "", 12)   # default cost_bps=0
    assert u["realized_r"] == u["gross_r"] == 2.0


def test_summarize_expectancy():
    closed = [{"archetype": "momentum", "realized_r": 2.0},
              {"archetype": "momentum", "realized_r": -1.0},
              {"archetype": "momentum", "realized_r": -1.0}]
    s = stock_positions.summarize(closed)
    assert s["overall"]["n"] == 3
    assert s["overall"]["win_rate"] == round(1 / 3, 3)   # summarize rounds to 3 dp
    assert abs(s["overall"]["expectancy_r"] - 0.0) < 1e-9


def test_summarize_excludes_voided_rows():
    closed = [{"archetype": "momentum", "realized_r": 2.0},
              {"archetype": "momentum", "realized_r": None, "exit_reason": "rebased"},
              {"archetype": "momentum", "realized_r": None}]
    s = stock_positions.summarize(closed)
    assert s["overall"]["n"] == 1              # voided rows never count as wins/losses
    assert s["overall"]["win_rate"] == 1.0


# --- confidence --------------------------------------------------------------

class _C:
    archetype = "pead_drift"
    primary = 0.9
    rel = 0.9
    regime = 1.0
    context = 0.5


def test_confidence_bounds_and_label():
    c = stock_confidence.confidence(_C(), {})
    assert 0.30 <= c["prob"] <= 0.80
    assert c["label"] == "backtested prior" and c["live_confirmed"] is False


def test_confidence_live_confirmed_only_for_live_source():
    # a backtest seed with high n is still a prior (not live-confirmed)
    seed = {"source": "backtest", "archetypes": {"pead_drift": {
        "n": 600, "win_rate": 0.6, "expectancy_r": 0.4, "alignment": "announcement_date"}}}
    assert stock_confidence.confidence(_C(), seed)["live_confirmed"] is False
    # only LIVE out-of-sample data flips the flag
    live = {"source": "live", "archetypes": {"pead_drift": {
        "n": 60, "win_rate": 0.6, "expectancy_r": 0.4, "alignment": "announcement_date"}}}
    c = stock_confidence.confidence(_C(), live)
    assert c["live_confirmed"] is True and c["n"] == 60


def test_confidence_cap():
    strong = {"archetypes": {"pead_drift": {"n": 500, "win_rate": 0.99, "expectancy_r": 2,
                                            "alignment": "announcement_date"}}}
    assert stock_confidence.confidence(_C(), strong)["prob"] <= 0.80


def test_expectancy_shrunk_toward_prior_like_win_rate():
    # mean_reversion prior expectancy 0.10; n=24 empirical 0.257 must be shrunk with
    # the same pseudo-count as win-rate, not taken verbatim.
    wr = {"archetypes": {"mean_reversion": {"n": 24, "win_rate": 0.625, "expectancy_r": 0.257}}}
    br = stock_confidence.base_rate("mean_reversion", wr)
    assert abs(br["expectancy_r"] - (0.257 * 24 + 0.10 * 30) / 54) < 1e-9
    assert br["expectancy_r"] < 0.257
    # a large-n cell barely moves
    wr2 = {"archetypes": {"momentum": {"n": 1010, "win_rate": 0.494, "expectancy_r": 0.099}}}
    assert abs(stock_confidence.base_rate("momentum", wr2)["expectancy_r"] - 0.099) < 0.01


def test_pead_cell_without_alignment_marker_falls_back_to_prior():
    # old (period-end-aligned) seeds are invalid for the announcement-anchored setup
    stale = {"archetypes": {"pead_drift": {"n": 159, "win_rate": 0.673, "expectancy_r": 0.408}}}
    br = stock_confidence.base_rate("pead_drift", stale)
    assert br["n"] == 0
    assert br["win_rate"] == stock_confidence.PRIOR["pead_drift"]["win_rate"]
    valid = {"archetypes": {"pead_drift": {"n": 159, "win_rate": 0.673, "expectancy_r": 0.408,
                                           "alignment": "announcement_date"}}}
    assert stock_confidence.base_rate("pead_drift", valid)["n"] == 159


def test_archetype_maturity_derived_from_winrates_cell():
    # no winrates at all -> forward
    assert stock_confidence.archetype_maturity("pead_drift", {}) == "forward"
    # unmarked pead cell (invalid alignment) -> forward even if flagged significant
    stale = {"archetypes": {"pead_drift": {"n": 500, "win_rate": 0.7, "expectancy_r": 0.4,
                                           "not_significant": False}}}
    assert stock_confidence.archetype_maturity("pead_drift", stale) == "forward"
    # valid + explicitly significant -> edge
    valid = {"archetypes": {"pead_drift": {"n": 500, "win_rate": 0.7, "expectancy_r": 0.4,
                                           "alignment": "announcement_date",
                                           "not_significant": False}}}
    assert stock_confidence.archetype_maturity("pead_drift", valid) == "edge"
    # valid but not significant (or unmarked significance) -> forward
    insig = {"archetypes": {"pead_drift": {"n": 40, "win_rate": 0.6, "expectancy_r": 0.2,
                                           "alignment": "announcement_date",
                                           "not_significant": True}}}
    assert stock_confidence.archetype_maturity("pead_drift", insig) == "forward"
    unmarked = {"archetypes": {"momentum": {"n": 1010, "win_rate": 0.494, "expectancy_r": 0.099}}}
    assert stock_confidence.archetype_maturity("momentum", unmarked) == "forward"


# --- ranking -----------------------------------------------------------------

def test_priority_score_edge_outranks_trending():
    # PEAD (+0.45R) at a LOWER composite should outrank momentum (+0.17R) at a higher
    # composite — the screener leads with expected value, not raw signal strength.
    pead = stock_scoring.priority_score(72, 0.45)
    mom = stock_scoring.priority_score(85, 0.17)
    assert pead > mom
    assert stock_scoring.is_edge("pead_drift") and not stock_scoring.is_edge("momentum")
    # None expectancy is treated as 0 (no boost, no crash)
    assert stock_scoring.priority_score(50, None) == 50


def test_rank_sorts_and_assigns_composite():
    a = stock_scoring.Candidate("A", "BUY", "momentum", 0.9)
    b = stock_scoring.Candidate("B", "BUY", "momentum", 0.3)
    ranked = stock_scoring.rank([b, a], "bull", {"A": 0.5, "B": 0.1})
    assert ranked[0].ticker == "A" and ranked[0].composite >= ranked[1].composite
    assert all(0 <= c.composite <= 100 for c in ranked)


# --- store round-trip --------------------------------------------------------

def test_store_roundtrip():
    conn = store.connect(":memory:")
    store.init_db(conn)
    stock_store.init_stock_db(conn)
    stock_store.upsert_universe(conn, [("AAA", "Alpha Inc", "Tech", "0000000001")])
    assert stock_store.get_universe(conn)[0]["cik"] == "0000000001"
    stock_store.upsert_prices(conn, "AAA", [(DAY, 10, 11, 9, 10.5, 1e6)], source="test")
    assert stock_store.recent_prices(conn, "AAA")[0]["close"] == 10.5

    pid = stock_store.insert_position(conn, ticker="AAA", opened_run_ts="t", opened_ts=DAY,
                                      direction="BUY", archetype="momentum", confidence=0.6,
                                      entry=10.5, stop=9.5, t1=11.5, t2=12.5, atr=0.5,
                                      time_stop_days=20)
    assert len(stock_store.open_positions(conn)) == 1
    assert stock_store.has_open_position(conn, "AAA", "momentum")
    stock_store.close_position(conn, pid, closed_run_ts="t2", closed_ts=3 * DAY,
                               exit_price=12.5, realized_r=2.0, exit_reason="t2",
                               mfe_r=2.0, mae_r=-0.2)
    assert not stock_store.open_positions(conn)
    assert stock_store.closed_positions(conn)[0]["realized_r"] == 2.0

    stock_store.record_stock_run(conn, run_ts="r1", universe_n=1, scored_n=1, readings={"regime": "bull"})
    stock_store.record_stock_signals(conn, "r1", [{
        "ticker": "AAA", "rank": 1, "direction": "BUY", "archetype": "momentum",
        "composite": 80.0, "confidence": 0.6, "pead": None, "technical": 0.7,
        "insider": None, "revision": None, "price": 10.5,
        "entry": 10.5, "stop": 9.5, "t1": 11.5, "t2": 12.5, "atr": 0.5, "rr": 2.0,
        "detail_json": '{"catalyst":"x"}'}])
    sigs = stock_store.latest_stock_signals(conn)
    assert sigs[0]["ticker"] == "AAA" and sigs[0]["detail"]["catalyst"] == "x"
    conn.close()


# --- position lifecycle: pending fill / expiry / rebase / void -----------------

def _pending(conn, ticker="AAA", sig_ts=100 * DAY, atr=2.0, entry_bar_close=100.0,
             structure_stop=None):
    return stock_store.insert_position(
        conn, ticker=ticker, opened_run_ts="r0", opened_ts=sig_ts, direction="BUY",
        archetype="momentum", confidence=0.6, entry=100.0, stop=95.0, t1=104.0,
        t2=107.0, atr=atr, time_stop_days=20, status="PENDING", entry_venue="test",
        entry_bar_close=entry_bar_close, structure_stop=structure_stop)


def test_pending_fills_at_next_bar_open():
    conn = _conn_mem()
    sig_ts = 100 * DAY
    _pending(conn, sig_ts=sig_ts)
    assert stock_store.has_open_position(conn, "AAA", "momentum")   # pending blocks dups
    assert not stock_store.open_positions(conn)                     # ...but is not open risk
    bars = [{"ts": sig_ts, "open": 99, "high": 101, "low": 98, "close": 100, "volume": 1e6},
            {"ts": sig_ts + DAY, "open": 102, "high": 103, "low": 101, "close": 102.5, "volume": 1e6}]
    events = stock_collect._advance_positions(conn, CFG, "r1", sig_ts + 2 * DAY,
                                              {"AAA": bars}, {"AAA": "test"}, False)
    assert events == []
    openp = stock_store.open_positions(conn)
    assert len(openp) == 1 and not stock_store.pending_positions(conn)
    pos = openp[0]
    assert pos["entry"] == 102.0                 # the NEXT bar's open, not the signal close
    assert pos["filled_ts"] == sig_ts + DAY
    assert pos["entry_bar_close"] == 102.5 and pos["entry_venue"] == "test"
    assert pos["stop"] < 102.0 < pos["t2"]       # levels recomputed off the fill price
    assert pos["last_reprice_ts"] == sig_ts + 2 * DAY
    conn.close()


def test_pending_expires_unfilled():
    conn = _conn_mem()
    sig_ts = 100 * DAY
    _pending(conn, sig_ts=sig_ts)
    # no bar ever arrives after the signal; 6 days later the setup expires
    bars = [{"ts": sig_ts, "open": 99, "high": 101, "low": 98, "close": 100, "volume": 1e6}]
    events = stock_collect._advance_positions(conn, CFG, "r1", sig_ts + 6 * DAY,
                                              {"AAA": bars}, {"AAA": "test"}, False)
    assert events and events[0]["exit_reason"] == "unfilled"
    assert not stock_store.pending_positions(conn)
    assert not stock_store.open_positions(conn)
    assert stock_store.closed_positions(conn) == []   # EXPIRED never enters the record
    assert not stock_store.has_open_position(conn, "AAA", "momentum")
    conn.close()


def test_pending_expires_when_first_bar_is_too_late():
    conn = _conn_mem()
    sig_ts = 100 * DAY
    _pending(conn, sig_ts=sig_ts)
    # data resumed only after a >5-day gap: too stale to fill honestly
    bars = [{"ts": sig_ts + 8 * DAY, "open": 102, "high": 103, "low": 101, "close": 102,
             "volume": 1e6}]
    stock_collect._advance_positions(conn, CFG, "r1", sig_ts + 8 * DAY,
                                     {"AAA": bars}, {"AAA": "test"}, False)
    assert not stock_store.pending_positions(conn)
    assert not stock_store.open_positions(conn)
    conn.close()


def test_pending_fill_rescales_frozen_frame_after_split_in_gap():
    conn = _conn_mem()
    sig_ts = 100 * DAY
    # signal-day basis: close 100, atr 2.0, structure stop 96 frozen on the row
    _pending(conn, sig_ts=sig_ts, atr=2.0, entry_bar_close=100.0, structure_stop=96.0)
    # a 10:1 split becomes effective during the pending gap: the fill-run series is
    # served entirely in post-split units (the signal bar's close now reads 10.0)
    bars = [{"ts": sig_ts, "open": 9.9, "high": 10.1, "low": 9.8, "close": 10.0, "volume": 1e7},
            {"ts": sig_ts + DAY, "open": 10.2, "high": 10.4, "low": 10.1, "close": 10.3, "volume": 1e7}]
    events = stock_collect._advance_positions(conn, CFG, "r1", sig_ts + 2 * DAY,
                                              {"AAA": bars}, {"AAA": "test"}, False)
    assert events == []
    pos = stock_store.open_positions(conn)[0]
    assert pos["entry"] == 10.2                    # fill at the next bar's open
    assert abs(pos["atr"] - 0.2) < 1e-9            # atr re-expressed post-split
    assert abs(pos["structure_stop"] - 9.6) < 1e-9
    # momentum stop = entry - 2.5*atr with the RESCALED atr (9.7), not the
    # pre-split-frame 10.2 - 5.0 = 5.2 (~50% below entry)
    assert abs(pos["stop"] - 9.7) < 1e-9
    # the reprice-side re-base guard re-anchors at the FILL bar's close
    assert abs(pos["entry_bar_close"] - 10.3) < 1e-9
    conn.close()


def test_pending_expires_data_gap_when_signal_bar_missing():
    conn = _conn_mem()
    sig_ts = 100 * DAY
    _pending(conn, sig_ts=sig_ts)
    # the fill-run series no longer contains the signal bar -> basis unverifiable
    bars = [{"ts": sig_ts + DAY, "open": 50, "high": 51, "low": 49, "close": 50, "volume": 1e6}]
    events = stock_collect._advance_positions(conn, CFG, "r1", sig_ts + 2 * DAY,
                                              {"AAA": bars}, {"AAA": "test"}, False)
    assert events and events[0]["status"] == "EXPIRED"
    assert events[0]["exit_reason"] == "data_gap"
    assert not stock_store.pending_positions(conn)
    assert not stock_store.open_positions(conn)
    assert stock_store.closed_positions(conn) == []   # EXPIRED never enters the record
    conn.close()


def test_pending_without_signal_anchor_fills_legacy_path():
    conn = _conn_mem()
    sig_ts = 100 * DAY
    # legacy PENDING row (created before entry_bar_close was stored at signal time)
    _pending(conn, sig_ts=sig_ts, entry_bar_close=None)
    bars = [{"ts": sig_ts + DAY, "open": 102, "high": 103, "low": 101, "close": 102.5,
             "volume": 1e6}]
    stock_collect._advance_positions(conn, CFG, "r1", sig_ts + 2 * DAY,
                                     {"AAA": bars}, {"AAA": "test"}, False)
    assert len(stock_store.open_positions(conn)) == 1   # no anchor -> no guard, fills
    conn.close()


def test_just_filled_position_advances_once_per_run():
    conn = _conn_mem()
    sig_ts = 100 * DAY
    _pending(conn, sig_ts=sig_ts)
    # fill bar rockets through T2 the same session: fill + close in ONE run must
    # produce exactly one CLOSED event (the re-queried open loop skips just-filled)
    bars = [{"ts": sig_ts, "open": 99, "high": 101, "low": 98, "close": 100, "volume": 1e6},
            {"ts": sig_ts + DAY, "open": 102, "high": 200, "low": 101, "close": 190, "volume": 1e6}]
    events = stock_collect._advance_positions(conn, CFG, "r1", sig_ts + 2 * DAY,
                                              {"AAA": bars}, {"AAA": "test"}, False)
    closed_events = [e for e in events if e.get("status") == "CLOSED"]
    assert len(closed_events) == 1 and closed_events[0]["exit_reason"] == "t2"
    assert len(stock_store.closed_positions(conn)) == 1
    conn.close()


def _filled(conn, ticker="AAA", fill_ts=101 * DAY):
    pid = _pending(conn, ticker=ticker, sig_ts=fill_ts - DAY)
    stock_store.fill_position(conn, pid, filled_ts=fill_ts, entry=100.0, stop=95.0,
                              t1=104.0, t2=110.0, entry_venue="test",
                              entry_bar_close=100.0, last_reprice_ts=fill_ts)
    return pid


def test_open_position_rebased_after_split_not_stopped_out():
    conn = _conn_mem()
    fill_ts = 101 * DAY
    _filled(conn, fill_ts=fill_ts)
    # the venue retroactively 10:1-split-adjusted the WHOLE series
    bars = [{"ts": fill_ts, "open": 9.9, "high": 10.2, "low": 9.8, "close": 10.0, "volume": 1e7},
            {"ts": fill_ts + DAY, "open": 10.1, "high": 10.4, "low": 10.0, "close": 10.3, "volume": 1e7}]
    events = stock_collect._advance_positions(conn, CFG, "r1", fill_ts + 2 * DAY,
                                              {"AAA": bars}, {"AAA": "test"}, False)
    assert events == []                        # NOT a fake -1R stop-out
    pos = stock_store.open_positions(conn)[0]
    assert abs(pos["entry"] - 10.0) < 1e-9 and abs(pos["stop"] - 9.5) < 1e-9
    assert abs(pos["entry_bar_close"] - 10.0) < 1e-9   # next run's ratio ~ 1
    conn.close()


def test_open_position_voided_when_entry_bar_missing():
    conn = _conn_mem()
    fill_ts = 101 * DAY
    _filled(conn, fill_ts=fill_ts)
    # re-fetched series no longer contains the entry bar -> basis unverifiable
    bars = [{"ts": fill_ts + DAY, "open": 50, "high": 51, "low": 49, "close": 50, "volume": 1e6}]
    events = stock_collect._advance_positions(conn, CFG, "r1", fill_ts + 2 * DAY,
                                              {"AAA": bars}, {"AAA": "test"}, False)
    assert events and events[0]["exit_reason"] == "rebased"
    closed = stock_store.closed_positions(conn)
    assert len(closed) == 1 and closed[0]["exit_reason"] == "rebased"
    assert closed[0]["realized_r"] is None
    # ...and the voided row never pollutes the aggregate record
    assert stock_positions.summarize(closed)["overall"]["n"] == 0
    conn.close()


def test_open_position_closes_normally_through_lifecycle():
    conn = _conn_mem()
    fill_ts = 101 * DAY
    _filled(conn, fill_ts=fill_ts)   # entry 100 stop 95 t2 110
    bars = [{"ts": fill_ts, "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1e6},
            {"ts": fill_ts + DAY, "open": 103, "high": 111, "low": 102, "close": 109, "volume": 1e6}]
    events = stock_collect._advance_positions(conn, CFG, "r1", fill_ts + 2 * DAY,
                                              {"AAA": bars}, {"AAA": "test"}, False)
    assert events and events[0]["exit_reason"] == "t2"
    assert stock_store.closed_positions(conn)[0]["exit_price"] == 110.0
    conn.close()


# --- alert send failure: retry-once + cooldown discipline ----------------------

def test_unsent_alert_does_not_arm_cooldown_and_retries_once():
    conn = _conn_mem()
    stock_store.record_stock_alert(conn, ts=DAY, created_at="t", ticker="AAA",
                                   archetype="momentum", direction="BUY", entry=10.0,
                                   stop=9.0, t1=11.0, t2=12.0, confidence=0.6,
                                   message="m", sent=False)
    assert stock_store.last_stock_alert(conn, "AAA", "momentum") is None  # cooldown NOT armed
    rows = stock_store.unsent_stock_alerts(conn)
    assert len(rows) == 1
    stock_store.mark_stock_alert_retry(conn, rows[0]["id"], sent=True)
    assert stock_store.unsent_stock_alerts(conn) == []
    assert stock_store.last_stock_alert(conn, "AAA", "momentum") is not None  # armed after resend
    # a failed retry is dropped for good (retry-once, no infinite loop)
    stock_store.record_stock_alert(conn, ts=DAY, created_at="t", ticker="BBB",
                                   archetype="momentum", direction="BUY", entry=10.0,
                                   stop=9.0, t1=11.0, t2=12.0, confidence=0.6,
                                   message="m", sent=False)
    rows = stock_store.unsent_stock_alerts(conn)
    stock_store.mark_stock_alert_retry(conn, rows[0]["id"], sent=False)
    assert stock_store.unsent_stock_alerts(conn) == []
    assert stock_store.last_stock_alert(conn, "BBB", "momentum") is None
    conn.close()


def _alert_payload(ticker="AAA"):
    sig = {"ticker": ticker, "direction": "BUY", "archetype": "momentum", "entry": 10.0,
           "stop": 9.0, "t1": 11.0, "t2": 12.0, "rr": 2.0, "confidence": 0.6}
    detail = {"archetype_label": "Momentum breakout", "catalyst": "x",
              "edge_class": "forward",
              "confidence": {"prob": 0.6, "label": "backtested prior"},
              "levels": {"risk_pct": 5.0}}
    return {"sig": sig, "detail": detail, "ts": DAY}


def test_alert_arms_cooldown_when_no_transport_configured(monkeypatch):
    conn = _conn_mem()
    now = datetime.now(timezone.utc)
    # empty-.env mode: send() is a no-op returning False, but the alert row is the
    # canonical record -> the cooldown must arm off creation (nothing to retry).
    monkeypatch.setattr(stock_collect.notify, "send", lambda *a, **k: False)
    monkeypatch.setattr(stock_collect.notify, "has_transport", lambda cfg: False)
    stock_collect._maybe_alert(conn, CFG, [_alert_payload("AAA")], now, False)
    assert stock_store.last_stock_alert(conn, "AAA", "momentum") is not None
    assert stock_store.unsent_stock_alerts(conn) == []   # no dead retry queued
    # a send FAILURE with a CONFIGURED transport keeps retry semantics: unarmed
    monkeypatch.setattr(stock_collect.notify, "has_transport", lambda cfg: True)
    stock_collect._maybe_alert(conn, CFG, [_alert_payload("BBB")], now, False)
    assert stock_store.last_stock_alert(conn, "BBB", "momentum") is None
    assert len(stock_store.unsent_stock_alerts(conn)) == 1
    conn.close()


def test_has_transport_predicate():
    bare = dataclasses.replace(CFG, resend_api_key="", email_to="", ntfy_topic="",
                               telegram_bot_token="", telegram_chat_id="")
    assert notify.has_transport(bare) is False
    assert notify.has_transport(dataclasses.replace(bare, ntfy_topic="t")) is True
    assert notify.has_transport(
        dataclasses.replace(bare, resend_api_key="k", email_to="a@b.c")) is True


# --- API: per-setup maturity rung ------------------------------------------------

def _signal_row(detail: dict) -> dict:
    return {"ticker": "AAA", "rank": 1, "direction": "BUY", "archetype": "momentum",
            "composite": 80.0, "confidence": 0.6, "pead": None, "technical": 0.7,
            "insider": None, "revision": None, "price": 10.5,
            "entry": 10.5, "stop": 9.5, "t1": 11.5, "t2": 12.5, "atr": 0.5, "rr": 2.0,
            "detail": detail}


def test_setup_serves_maturity_rung():
    # the rung persisted at signal time (what the alert email used) is preferred
    out = stock_api._setup_from_signal(_signal_row({"edge_class": "forward"}))
    assert out["maturity"] == "forward" and out["edge_class"] == "forward"
    assert stock_api._setup_from_signal(_signal_row({"edge_class": "edge"}))["maturity"] == "edge"
    # pre-migration rows (no stored edge_class) derive from the loaded win-rates;
    # the committed seed has no significant cells, so every archetype is 'forward'
    legacy = stock_api._setup_from_signal(_signal_row({}))
    assert legacy["maturity"] == "forward"
    assert legacy["edge_class"] == "unproven"   # legacy default untouched


def test_positions_annotated_with_maturity():
    rows = [{"ticker": "AAA", "archetype": "momentum"},
            {"ticker": "BBB", "archetype": "pead_drift"}]
    out = stock_api._annotate_maturity(rows)
    assert all(r["maturity"] in ("edge", "forward") for r in out)


# --- estimate snapshot guard ---------------------------------------------------

def test_estimate_snapshot_guard_once_per_day(monkeypatch):
    conn = _conn_mem()
    cfg = dataclasses.replace(CFG, finnhub_api_key="test-key")
    calls = {"n": 0}

    def fake_rec(tk, key):
        calls["n"] += 1
        return {"period": "2026-06", "strong_buy": 5, "buy": 10, "hold": 3,
                "sell": 1, "strong_sell": 0}

    monkeypatch.setattr(stock_collect.estimates, "recommendation", fake_rec)
    stock_collect._snapshot_estimates(conn, cfg, ["AAA"])
    assert calls["n"] == 1
    stock_collect._snapshot_estimates(conn, cfg, ["AAA"])   # within 20h -> guarded
    assert calls["n"] == 1                                  # no re-fetch either
    assert len(stock_store.last_two_estimate_snaps(conn, "AAA")) == 1  # single same-day snap
    conn.close()


# --- run readings: health counts contract ---------------------------------------

def test_recent_stock_runs_parse_counts():
    conn = _conn_mem()
    for i, fetched in enumerate([500, 0, 0, 0]):
        stock_store.record_stock_run(
            conn, run_ts=f"2026-06-0{i+1}T00:00:00+00:00", universe_n=500, scored_n=0,
            readings={"counts": {"prices_fetched": fetched, "earnings_rows": 0}})
    runs = stock_store.recent_stock_runs(conn, 3)
    assert len(runs) == 3
    assert all(r["readings"]["counts"]["prices_fetched"] == 0 for r in runs)  # newest 3
    conn.close()


def test_layer_status_degraded_on_zero_rows_for_n_runs():
    from app import stock_api
    zero = [{"readings": {"counts": {"earnings_rows": 0, "prices_fetched": 500}}}] * 3
    assert stock_api._layer_status(zero, "earnings_pead", True) == "degraded"
    assert stock_api._layer_status(zero, "prices", True) == "ok"
    assert stock_api._layer_status(zero, "earnings_pead", False) == "off"
    # a single zero-row run (blip) is NOT degraded
    blip = [{"readings": {"counts": {"earnings_rows": 0}}},
            {"readings": {"counts": {"earnings_rows": 12}}},
            {"readings": {"counts": {"earnings_rows": 7}}}]
    assert stock_api._layer_status(blip, "earnings_pead", True) == "ok"
    # runs from before the counts readings existed are tolerated (old shape)
    old = [{"readings": {"layers": {"earnings": True}}}] * 3
    assert stock_api._layer_status(old, "earnings_pead", True) == "ok"

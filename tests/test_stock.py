"""Tests for the stock swing tracker — pure engine (no network) + store round-trip."""
from __future__ import annotations

from app import (stock_confidence, stock_levels, stock_positions, stock_scoring,
                 stock_store, store)
from app.config import load_config

CFG = load_config()
DAY = 86_400_000


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
    bars = [{"ts": DAY, "high": 101, "low": 94, "close": 96}]  # low pierces stop 95
    u = stock_positions.reprice(_pos(), bars, "", 12)
    assert u["status"] == "CLOSED" and u["exit_reason"] == "stop"
    assert round(u["realized_r"], 2) == -1.0


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


def test_summarize_expectancy():
    closed = [{"archetype": "momentum", "realized_r": 2.0},
              {"archetype": "momentum", "realized_r": -1.0},
              {"archetype": "momentum", "realized_r": -1.0}]
    s = stock_positions.summarize(closed)
    assert s["overall"]["n"] == 3
    assert s["overall"]["win_rate"] == round(1 / 3, 3)   # summarize rounds to 3 dp
    assert abs(s["overall"]["expectancy_r"] - 0.0) < 1e-9


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
    seed = {"source": "backtest", "archetypes": {"pead_drift": {"n": 600, "win_rate": 0.6, "expectancy_r": 0.4}}}
    assert stock_confidence.confidence(_C(), seed)["live_confirmed"] is False
    # only LIVE out-of-sample data flips the flag
    live = {"source": "live", "archetypes": {"pead_drift": {"n": 60, "win_rate": 0.6, "expectancy_r": 0.4}}}
    c = stock_confidence.confidence(_C(), live)
    assert c["live_confirmed"] is True and c["n"] == 60


def test_confidence_cap():
    strong = {"archetypes": {"pead_drift": {"n": 500, "win_rate": 0.99, "expectancy_r": 2}}}
    assert stock_confidence.confidence(_C(), strong)["prob"] <= 0.80


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
        "insider": None, "shortvol": None, "revision": None, "price": 10.5,
        "entry": 10.5, "stop": 9.5, "t1": 11.5, "t2": 12.5, "atr": 0.5, "rr": 2.0,
        "detail_json": '{"catalyst":"x"}'}])
    sigs = stock_store.latest_stock_signals(conn)
    assert sigs[0]["ticker"] == "AAA" and sigs[0]["detail"]["catalyst"] == "x"
    conn.close()

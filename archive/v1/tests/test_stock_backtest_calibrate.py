"""Tests for the stock source adapters + the offline backtest/calibrate scripts.

All deterministic, no network (HTTP helpers are monkeypatched). Covers:
  * earnings.surprise_history — announcement-date alignment (never the fiscal
    period end), the (year, quarter) join, the quarter-end look-ahead guard,
    the <=55/min Finnhub pacing, and the window-coverage guard (partial history
    is dropped rather than passed off as complete).
  * prices — explicit start dates for Alpaca/Tiingo (the "latest bar only"
    degenerate defaults), Alpaca pagination, split-only adjustment basis on every
    venue, the daily_bars min-length fallthrough, and venue pinning.
  * insider — Form 4/A amendments superseding the originals they refile.
  * stock_backtest — next-bar-open fills, sample-scaled top-N + cooldown
    selection replay, the live pending-expiry mirror, the live lookback+4d PEAD
    candidate window, the random_entry baseline, and the win-rate cell shape
    (n_months, deltas, month-clustered significance, PEAD alignment marker).
  * stock_calibrate — rebased/pending exclusion, the live cell shape, and the
    merge-into-seed live promotion (Wilson-vs-stored-baseline significance).
"""
from __future__ import annotations

import statistics
from collections import Counter
from datetime import datetime, timezone

import pytest

from app import stock_levels
from app.sources.stocks import earnings, insider, prices
from scripts import stock_backtest, stock_calibrate
from tests.factories import make_config

DAY = 86_400_000


@pytest.fixture(autouse=True)
def _no_finnhub_pacing(monkeypatch):
    """Disable the Finnhub call pacing for tests (the pacing test re-enables it
    against a fake clock)."""
    monkeypatch.setattr(earnings, "_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(earnings, "_last_call", 0.0)


def _ms(datestr: str) -> int:
    d = datetime.strptime(datestr, "%Y-%m-%d")
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _mkbars(closes: list[float], vol: float = 2_000_000.0,
            start_ts: int = 1_600_000_000_000) -> list[dict]:
    """OHLCV daily bars from a close series (open = prior close, H/L padded)."""
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        hi, lo = max(c, prev) * 1.005, min(c, prev) * 0.995
        out.append({"ts": start_ts + i * DAY, "open": prev, "high": hi, "low": lo,
                    "close": c, "volume": vol})
        prev = c
    return out


def test_backtest_rejects_period_aligned_earnings_map(monkeypatch):
    cfg = make_config(finnhub_api_key="k")
    bad = [{"report_ts": _ms(d), "surprise_pct": 8.0}
           for d in ("2025-03-31", "2024-12-31", "2024-09-30")]
    good = [{"report_ts": _ms(d), "surprise_pct": 8.0}
            for d in ("2025-04-24", "2025-01-30", "2024-10-29")]
    monkeypatch.setattr(stock_backtest.earnings_src, "surprise_history", lambda *a, **k: bad)
    assert stock_backtest._earnings_by_ts("AAA", cfg, years=2.0) == {}
    monkeypatch.setattr(stock_backtest.earnings_src, "surprise_history", lambda *a, **k: good)
    assert len(stock_backtest._earnings_by_ts("AAA", cfg, years=2.0)) == 3


# --- backtest: fills, selection replay, baseline, cells ------------------------

def _pullback_uptrend(n: int) -> list[float]:
    # periodic pullbacks so RSI stays below the momentum "too extended" gate
    return [50 + i * 0.5 - (2.0 if i % 4 == 0 else 0.0) for i in range(n)]


def test_run_backtest_fills_next_bar_open_topn_and_cooldown():
    cfg = make_config(stock_top_n=1)
    bars_a = _mkbars(_pullback_uptrend(280))
    bars_b = _mkbars([c * 1.01 for c in _pullback_uptrend(280)])
    bbt = {"AAA": bars_a, "BBB": bars_b}
    trades = stock_backtest.run_backtest(bbt, {}, cfg)
    assert trades, "expected momentum trades from the uptrend"
    by_ts = {tk: {b["ts"]: b for b in bars} for tk, bars in bbt.items()}
    for t in trades:
        assert t["archetype"] == "momentum"
        assert t["entry"] == by_ts[t["ticker"]][t["entry_ts"]]["open"]   # next-bar OPEN
        assert t["month"] == stock_backtest.month_key(t["entry_ts"])
    # top-N=1: at most one NEW trade can open per session across the universe
    assert max(Counter(t["entry_ts"] for t in trades).values()) == 1
    # per-(ticker, archetype) cooldown honored between consecutive entries
    last_by_key: dict = {}
    for t in sorted(trades, key=lambda t: t["entry_ts"]):
        key = (t["ticker"], t["archetype"])
        if key in last_by_key:
            assert t["entry_ts"] - last_by_key[key] >= cfg.stock_cooldown_days * DAY
        last_by_key[key] = t["entry_ts"]


def test_run_backtest_pead_from_announcement_aligned_map():
    cfg = make_config()
    n = 270
    closes = [40 + i * 0.2 for i in range(n)]        # pure ramp: RSI~100 blocks momentum
    closes[250] = closes[249] * 1.06                 # earnings-day reaction
    for j in range(251, n):
        closes[j] = closes[j - 1] * 1.01
    bars = _mkbars(closes)
    report_ts = bars[250]["ts"]
    emap = {report_ts: {"report_ts": report_ts, "surprise_pct": 9.0, "hour": "",
                        "actual": 1.1, "estimate": 1.0}}
    trades = stock_backtest.run_backtest({"AAA": bars}, {"AAA": emap}, cfg)
    peads = [t for t in trades if t["archetype"] == "pead_drift"]
    assert peads and peads[0]["direction"] == "BUY"
    assert peads[0]["entry_ts"] > report_ts          # entry strictly after the announcement


def test_run_backtest_topn_scaled_by_sample_fraction():
    # Live cuts top-15 from ~536 names; a 2-ticker sample must scale to top-1,
    # not admit nearly every firing candidate (that would measure a far less
    # selective population than the live selection the seed claims to replay).
    cfg = make_config()                              # stock_top_n default = 15
    bars_a = _mkbars(_pullback_uptrend(280))
    bars_b = _mkbars([c * 1.01 for c in _pullback_uptrend(280)])
    trades = stock_backtest.run_backtest({"AAA": bars_a, "BBB": bars_b}, {}, cfg)
    assert trades
    # top_n_eff = max(1, round(15 * 2 / 536)) = 1 -> one new entry per session max
    assert max(Counter(t["entry_ts"] for t in trades).values()) == 1


def test_latest_earnings_window_mirrors_live_calendar_fetch():
    # Live _fetch_earnings pulls lookback+4 CALENDAR days and lets the shared
    # pead_candidate trading-bar gate decide; a report 11-14 calendar days old
    # (weekend/holiday-straddling) is alertable live and must stay visible here.
    cfg = make_config()                              # stock_pead_lookback_days = 10
    last_ts = _ms("2025-06-20")
    stale = {"report_ts": last_ts - 12 * DAY, "surprise_pct": 8.0}
    too_old = {"report_ts": last_ts - 15 * DAY, "surprise_pct": 8.0}
    emap = {stale["report_ts"]: stale, too_old["report_ts"]: too_old}
    assert stock_backtest._latest_earnings(emap, last_ts, cfg) is stale
    assert stock_backtest._latest_earnings(
        {too_old["report_ts"]: too_old}, last_ts, cfg) is None
    assert stock_backtest._latest_earnings({}, last_ts, cfg) is None


def test_run_backtest_skips_entries_live_would_expire_unfilled():
    # Mirror of stock_collect._PENDING_EXPIRY_MS: a fill bar >5 calendar days
    # after the decision bar is a halt/data gap live would expire unfilled.
    cfg = make_config(stock_top_n=1)
    bars = _mkbars(_pullback_uptrend(280))
    trades0 = stock_backtest.run_backtest({"AAA": bars}, {}, cfg)
    assert trades0
    first = min(trades0, key=lambda t: t["entry_ts"])
    fill_i = next(i for i, b in enumerate(bars) if b["ts"] == first["entry_ts"])
    # Shift everything from that fill bar onward by +6 days: the same signal now
    # sees its first post-decision bar 7 calendar days out.
    gapped = bars[:fill_i] + [{**b, "ts": b["ts"] + 6 * DAY} for b in bars[fill_i:]]
    trades1 = stock_backtest.run_backtest({"AAA": gapped}, {}, cfg)
    gap_fill_ts = bars[fill_i]["ts"] + 6 * DAY
    assert all(t["entry_ts"] != gap_fill_ts for t in trades1)   # skipped, not filled
    # and every recorded fill respects the live pending-expiry window
    ts_index = {b["ts"]: i for i, b in enumerate(gapped)}
    for t in trades1:
        i = ts_index[t["entry_ts"]]
        assert t["entry_ts"] - gapped[i - 1]["ts"] <= 5 * DAY


def test_baseline_trades_skip_gap_fills(monkeypatch):
    cfg = make_config()
    bars = _mkbars(_pullback_uptrend(280))
    gap_i = 240
    gapped = bars[:gap_i] + [{**b, "ts": b["ts"] + 6 * DAY} for b in bars[gap_i:]]
    monkeypatch.setattr(stock_backtest, "_BASELINE_EVERY", 1)   # sample every date
    base = stock_backtest.baseline_trades({"AAA": gapped}, cfg, seed=3)
    assert base
    gap_fill_ts = bars[gap_i]["ts"] + 6 * DAY
    assert all(t["entry_ts"] != gap_fill_ts for t in base)      # gap fill skipped


def test_baseline_trades_deterministic_buy_only():
    cfg = make_config()
    bbt = {"AAA": _mkbars(_pullback_uptrend(280))}
    b1 = stock_backtest.baseline_trades(bbt, cfg, seed=7)
    b2 = stock_backtest.baseline_trades(bbt, cfg, seed=7)
    assert b1 == b2 and b1
    assert {t["archetype"] for t in b1} <= set(stock_levels.ARCHETYPE_LEVELS)
    assert all(t["direction"] == "BUY" for t in b1)


def test_build_cells_months_baseline_alignment():
    trades = [
        {"archetype": "pead_drift", "realized_r": 1.0, "month": "2025-01"},
        {"archetype": "pead_drift", "realized_r": -1.0, "month": "2025-02"},
        {"archetype": "momentum", "realized_r": 0.5, "month": "2025-01"},
        {"archetype": "momentum", "realized_r": 0.5, "month": "2025-01"},
    ]
    baseline = [
        {"archetype": "momentum", "realized_r": -0.5, "month": "2025-01"},
        {"archetype": "momentum", "realized_r": 0.5, "month": "2025-02"},
    ]
    cells = stock_backtest.build_cells(trades, baseline)
    pead, mom = cells["pead_drift"], cells["momentum"]
    assert pead["alignment"] == "announcement_date"  # the PEAD validity marker
    assert "alignment" not in mom
    assert pead["n"] == 2 and pead["n_months"] == 2
    assert mom["n"] == 2 and mom["n_months"] == 1    # serially-clustered trades exposed
    assert mom["baseline_n"] == 2
    assert mom["delta_expectancy_r"] == pytest.approx(0.5)   # 0.5 vs baseline 0.0
    assert mom["delta_win_rate"] == pytest.approx(0.5)       # 1.0 vs baseline 0.5
    assert pead["expectancy_r_month_std"] == pytest.approx(
        statistics.stdev([1.0, -1.0]), abs=1e-3)
    assert mom["expectancy_r_month_std"] is None     # needs >=2 entry months


# --- significance: fully month-clustered on both arms ---------------------------

def test_build_cells_one_clustered_month_cannot_buy_significance():
    # 50 correlated winners inside a single month inflate the trade-weighted
    # delta but not the month-clustered test (each month counts once).
    trades = ([{"archetype": "momentum", "realized_r": 2.0, "month": "2025-01"}] * 50
              + [{"archetype": "momentum", "realized_r": -0.2, "month": f"2025-{m:02d}"}
                 for m in range(2, 8)])
    baseline = [{"archetype": "momentum", "realized_r": 0.0, "month": f"2025-{m:02d}"}
                for m in range(1, 8)]
    cells = stock_backtest.build_cells(trades, baseline)
    mom = cells["momentum"]
    assert mom["delta_expectancy_r"] > 1.0           # display delta stays trade-weighted
    assert mom["not_significant"] is True            # ...but can't buy the EDGE label


def test_build_cells_consistent_monthly_edge_is_significant():
    # Uniform positive month deltas — including zero month-variance, which the
    # old rounded-std guard wrongly treated as not-significant.
    trades = [{"archetype": "momentum", "realized_r": 0.5, "month": f"2025-{m:02d}"}
              for m in range(1, 9)]
    baseline = [{"archetype": "momentum", "realized_r": -0.1, "month": f"2025-{m:02d}"}
                for m in range(1, 9)]
    cells = stock_backtest.build_cells(trades, baseline)
    assert cells["momentum"]["not_significant"] is False


def test_build_cells_requires_six_months_on_both_arms():
    trades = [{"archetype": "momentum", "realized_r": 0.5, "month": f"2025-{m:02d}"}
              for m in range(1, 9)]
    thin_baseline = [{"archetype": "momentum", "realized_r": -0.1, "month": f"2025-{m:02d}"}
                     for m in range(1, 3)]           # only 2 baseline months
    cells = stock_backtest.build_cells(trades, thin_baseline)
    assert cells["momentum"]["not_significant"] is True
    # and with no control at all, never significant
    assert stock_backtest.build_cells(trades)["momentum"]["not_significant"] is True


# --- calibrate: exclusions + live cell shape -----------------------------------

def test_eligible_positions_excludes_rebased_and_pending():
    rows = [
        {"status": "CLOSED", "exit_reason": "t2", "realized_r": 2.0,
         "archetype": "momentum", "opened_ts": DAY},
        {"status": "CLOSED", "exit_reason": "rebased", "realized_r": 0.0,
         "archetype": "momentum", "opened_ts": DAY},
        {"status": "pending", "exit_reason": None, "realized_r": None,
         "archetype": "momentum", "opened_ts": DAY},
    ]
    out = stock_calibrate.eligible_positions(rows)
    assert len(out) == 1 and out[0]["exit_reason"] == "t2"


def test_live_cells_shape_and_alignment():
    closed = [
        {"archetype": "pead_drift", "realized_r": 1.0, "opened_ts": _ms("2025-01-10"),
         "status": "CLOSED"},
        {"archetype": "pead_drift", "realized_r": -0.5, "opened_ts": _ms("2025-02-10"),
         "status": "CLOSED"},
    ]
    cells = stock_calibrate.live_cells(closed)
    c = cells["pead_drift"]
    assert c["n"] == 2 and c["n_months"] == 2
    assert c["alignment"] == "announcement_date"     # live entries key off the calendar
    assert {"n", "win_rate", "expectancy_r", "n_months"} <= set(c)


# --- calibrate: live promotion merges into the seed ------------------------------

_SEED = {
    "generated_at": "2026-01-01T00:00:00+00:00", "source": "backtest",
    "note": "seed", "method": "seed method",
    "baseline": {"momentum": {"n": 900, "win_rate": 0.5, "expectancy_r": 0.0}},
    "archetypes": {
        "momentum": {"n": 1000, "win_rate": 0.49, "expectancy_r": 0.10, "n_months": 48,
                     "expectancy_r_month_std": 0.4, "baseline_n": 900,
                     "baseline_win_rate": 0.5, "baseline_expectancy_r": 0.0,
                     "delta_win_rate": -0.01, "delta_expectancy_r": 0.1,
                     "not_significant": True},
        "pead_drift": {"n": 60, "win_rate": 0.58, "expectancy_r": 0.3, "n_months": 20,
                       "expectancy_r_month_std": 0.5, "not_significant": True,
                       "alignment": "announcement_date"},
    },
}


def _closed_rows(archetype: str, n: int, wins: int) -> list[dict]:
    return [{"archetype": archetype, "status": "CLOSED",
             "realized_r": (1.0 if i < wins else -1.0),
             "opened_ts": _ms(f"2025-{(i % 12) + 1:02d}-10")} for i in range(n)]


def test_wilson_lo_known_values():
    assert stock_calibrate.wilson_lo(0.5, 0) == 0.0  # no information
    # textbook: 1 success in 1 trial, z=1.96 -> Wilson lower ~0.207
    assert stock_calibrate.wilson_lo(1.0, 1) == pytest.approx(0.2068, abs=1e-3)
    assert stock_calibrate.wilson_lo(0.875, 40) == pytest.approx(0.7389, abs=1e-3)


def test_merged_winrates_promotes_live_cell_and_preserves_baseline():
    closed = _closed_rows("momentum", 40, 35)        # live 87.5% over 40 trades
    merged = stock_calibrate.merged_winrates(_SEED, closed, "2026-07-01T00:00:00+00:00")
    assert merged is not None
    mom = merged["archetypes"]["momentum"]
    assert mom["n"] == 40 and mom["win_rate"] == pytest.approx(0.875)
    assert mom["source"] == "live"
    assert mom["baseline_win_rate"] == 0.5           # seed control PRESERVED
    assert mom["baseline_n"] == 900
    # wilson_lo(0.875, 40) ~ 0.739 > 0.5 -> the live record re-earns significance
    assert mom["not_significant"] is False
    assert mom["delta_win_rate"] == pytest.approx(0.375)
    # sub-threshold archetype keeps its seed cell verbatim
    assert merged["archetypes"]["pead_drift"] == _SEED["archetypes"]["pead_drift"]
    assert merged["source"] == "live+seed"
    assert merged["baseline"] == _SEED["baseline"]   # seed top-level control kept
    # the input seed dict is not mutated
    assert _SEED["archetypes"]["momentum"]["n"] == 1000


def test_merged_winrates_weak_live_record_stays_not_significant():
    closed = _closed_rows("momentum", 40, 22)        # 55%: wilson_lo ~0.398 < 0.5
    merged = stock_calibrate.merged_winrates(_SEED, closed, "now")
    mom = merged["archetypes"]["momentum"]
    assert mom["source"] == "live" and mom["not_significant"] is True


def test_merged_winrates_no_seed_baseline_stays_not_significant():
    # A perfect live record without a stored control can't buy the EDGE label.
    closed = _closed_rows("pead_drift", 45, 45)
    seed = {"archetypes": {"pead_drift": dict(_SEED["archetypes"]["pead_drift"])}}
    merged = stock_calibrate.merged_winrates(seed, closed, "now")
    p = merged["archetypes"]["pead_drift"]
    assert p["n"] == 45 and p["source"] == "live"
    assert p["not_significant"] is True
    assert p["alignment"] == "announcement_date"     # marker preserved


def test_merged_winrates_below_threshold_returns_none():
    closed = _closed_rows("momentum", 39, 30)        # < 40 live trades
    assert stock_calibrate.merged_winrates(_SEED, closed, "now") is None
    assert stock_calibrate.merged_winrates(None, [], "now") is None

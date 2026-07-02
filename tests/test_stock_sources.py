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


# --- earnings: announcement alignment -----------------------------------------

_CAL_ROW = {"symbol": "AAA", "date": "2025-05-01", "hour": "amc", "quarter": 1,
            "year": 2025, "epsActual": 1.2, "epsEstimate": 1.0,
            "revenueActual": 110.0, "revenueEstimate": 100.0}
_HIST_ROW = {"symbol": "AAA", "period": "2025-03-31", "quarter": 1, "year": 2025,
             "actual": 1.2, "estimate": 1.0, "surprisePercent": 20.0}


def test_surprise_history_uses_announcement_date_not_period_end(monkeypatch):
    def fake_get_json(url, params=None, **kw):
        if "/calendar/earnings" in url:
            return {"earningsCalendar": [_CAL_ROW]}
        if "/stock/earnings" in url:
            return [_HIST_ROW]
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(earnings, "get_json", fake_get_json)
    rows = earnings.surprise_history("AAA", "key", years=0.6)
    assert len(rows) == 1                          # window-overlap duplicates collapsed
    r = rows[0]
    assert r["report_ts"] == _ms("2025-05-01")     # the announcement date...
    assert r["report_ts"] != _ms("2025-03-31")     # ...never the fiscal period end
    assert r["hour"] == "amc"                      # amc reaction-day shift can apply
    assert r["surprise_pct"] == pytest.approx(20.0)
    assert r["rev_surprise_pct"] == pytest.approx(10.0)


def test_surprise_history_joins_estimate_from_stock_earnings(monkeypatch):
    cal = {**_CAL_ROW, "epsEstimate": None}        # calendar row missing the estimate

    def fake_get_json(url, params=None, **kw):
        if "/calendar/earnings" in url:
            return {"earningsCalendar": [cal]}
        return [_HIST_ROW]

    monkeypatch.setattr(earnings, "get_json", fake_get_json)
    rows = earnings.surprise_history("AAA", "key", years=0.3)
    assert rows and rows[0]["estimate"] == 1.0     # joined on (year, quarter)
    assert rows[0]["surprise_pct"] == pytest.approx(20.0)
    assert rows[0]["report_ts"] == _ms("2025-05-01")


def test_surprise_history_no_key_returns_empty():
    assert earnings.surprise_history("AAA", None) == []


class _FakeClock:
    """Stand-in for the ``time`` module inside earnings._pace."""

    def __init__(self):
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


def test_surprise_history_paces_finnhub_calls(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(earnings, "time", clock)
    monkeypatch.setattr(earnings, "_MIN_INTERVAL_S", 60.0 / 55.0)
    monkeypatch.setattr(earnings, "_last_call", 0.0)
    calls: list[str] = []

    def fake_get_json(url, params=None, **kw):
        calls.append(url)
        if "/stock/earnings" in url:
            return [_HIST_ROW]
        return {"earningsCalendar": [_CAL_ROW]}

    monkeypatch.setattr(earnings, "get_json", fake_get_json)
    earnings.surprise_history("AAA", "key", years=0.6)  # 1 history + 3 window calls
    assert len(calls) == 4
    # every call after the first is forced >= the pacing interval apart (<=55/min)
    assert clock.sleeps == pytest.approx([60.0 / 55.0] * 3)


def test_surprise_history_drops_partial_window_coverage(monkeypatch):
    """A window whose request FAILED (get_json -> None) is not an empty window:
    below 90% coverage the whole history is dropped so a rate-limit-biased
    partial record can't masquerade as complete."""
    state = {"win": 0}

    def fake_get_json(url, params=None, **kw):
        if "/stock/earnings" in url:
            return [_HIST_ROW]
        state["win"] += 1
        if state["win"] == 2:
            return None                       # 429/network failure after retries
        return {"earningsCalendar": [_CAL_ROW]}

    monkeypatch.setattr(earnings, "get_json", fake_get_json)
    # years=0.6 -> 3 windows; 1 failed -> 67% < 90% coverage -> ticker dropped
    assert earnings.surprise_history("AAA", "key", years=0.6) == []


def test_surprise_history_coverage_boundary_and_empty_windows(monkeypatch):
    state = {"win": 0}

    def fake_get_json(url, params=None, **kw):
        if "/stock/earnings" in url:
            return [_HIST_ROW]
        state["win"] += 1
        if state["win"] <= 2:
            return None                       # 2 failures out of 24 windows
        # answered-but-empty windows are NOT failures
        return {"earningsCalendar": [_CAL_ROW] if state["win"] == 3 else []}

    monkeypatch.setattr(earnings, "get_json", fake_get_json)
    # years=6.05 -> the 24-window cap; 22/24 = 91.7% >= 90% -> history kept
    rows = earnings.surprise_history("AAA", "key", years=6.05)
    assert len(rows) == 1
    assert state["win"] == 24


def test_quarter_end_fraction():
    qe = [_ms("2025-03-31"), _ms("2025-06-30"), _ms("2024-12-31")]
    ann = [_ms("2025-05-01"), _ms("2025-07-29")]
    assert earnings.quarter_end_fraction(qe) == 1.0
    assert earnings.quarter_end_fraction(ann) == 0.0
    assert earnings.quarter_end_fraction(qe + ann) == pytest.approx(0.6)
    assert earnings.quarter_end_fraction([]) == 0.0


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


# --- prices: explicit windows, split-only basis, fallthrough, pinning ----------

def test_alpaca_daily_sends_start_and_paginates(monkeypatch):
    calls: list[dict] = []

    def fake_get_json(url, params=None, headers=None, **kw):
        calls.append(dict(params))
        if "page_token" not in params:
            return {"bars": [{"t": "2024-01-02", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100}],
                    "next_page_token": "tok"}
        return {"bars": [{"t": "2024-01-03", "o": 1, "h": 2, "l": 0.5, "c": 1.6, "v": 100}],
                "next_page_token": None}

    monkeypatch.setattr(prices, "get_json", fake_get_json)
    bars = prices.alpaca_daily("AAA", "k", "s", limit=400)
    assert len(bars) == 2 and bars[0][0] < bars[1][0]
    start = datetime.strptime(calls[0]["start"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    days_back = (datetime.now(timezone.utc) - start).days
    assert days_back >= 400 * 1.6                    # Alpaca defaults start to TODAY otherwise
    assert calls[1]["page_token"] == "tok"


def test_tiingo_daily_split_only_basis(monkeypatch):
    captured: dict = {}
    # 2:1 split effective 01-03 (raw prices already post-split on the ex-date);
    # 01-02 also pays a dividend that must NOT rebase prices.
    data = [
        {"date": "2024-01-02", "open": 99.0, "high": 101.0, "low": 98.0, "close": 100.0,
         "volume": 1000, "splitFactor": 1.0, "divCash": 0.5,
         "adjOpen": 45.0, "adjHigh": 46.0, "adjLow": 44.0, "adjClose": 45.0, "adjVolume": 2200},
        {"date": "2024-01-03", "open": 50.0, "high": 51.0, "low": 49.0, "close": 50.0,
         "volume": 2000, "splitFactor": 2.0, "divCash": 0.0,
         "adjOpen": 46.0, "adjHigh": 47.0, "adjLow": 45.0, "adjClose": 46.0, "adjVolume": 2000},
        {"date": "2024-01-04", "open": 50.0, "high": 52.0, "low": 49.5, "close": 51.0,
         "volume": 2100, "splitFactor": 1.0, "divCash": 0.0,
         "adjOpen": 46.5, "adjHigh": 48.0, "adjLow": 46.0, "adjClose": 47.0, "adjVolume": 2100},
    ]

    def fake_get_json(url, params=None, **kw):
        captured.update(params)
        return data

    monkeypatch.setattr(prices, "get_json", fake_get_json)
    bars = prices.tiingo_daily("AAA", "tok", limit=400)
    assert "startDate" in captured                   # no startDate -> latest record only
    assert [b[4] for b in bars] == [50.0, 50.0, 51.0]   # pre-split bar halved by split ONLY
    assert [b[5] for b in bars] == [2000.0, 2000.0, 2100.0]  # volume doubled pre-split
    assert bars[0][4] != 45.0                        # total-return adjClose NOT used


def test_yahoo_daily_split_only_no_adjclose_scaling(monkeypatch):
    captured: dict = {}

    def fake_get_json(url, params=None, headers=None, **kw):
        captured.update(params)
        return {"chart": {"result": [{
            "timestamp": [1704153600, 1704240000],
            "indicators": {"quote": [{"open": [99.0, 100.0], "high": [101.0, 102.0],
                                      "low": [98.0, 99.0], "close": [100.0, 101.0],
                                      "volume": [1000, 1100]}],
                           "adjclose": [{"adjclose": [90.0, 91.0]}]}}]}}

    monkeypatch.setattr(prices, "get_json", fake_get_json)
    bars = prices.yahoo_daily("AAA", limit=400)
    assert [b[4] for b in bars] == [100.0, 101.0]    # dividend-adjusted ratio NOT applied
    assert captured["range"] == "2y"                 # derived from limit
    assert prices._yahoo_range_for(1400) == "10y"    # deep-history backtests reach past 2y


def test_venue_basis_map_split_only_except_stooq():
    assert set(prices.VENUE_BASIS) == {"alpaca", "tiingo", "yahoo", "massive", "stooq"}
    assert prices.VENUE_BASIS["stooq"] == "split_div"
    assert all(v == "split" for k, v in prices.VENUE_BASIS.items() if k != "stooq")


def test_daily_bars_rejects_degenerate_short_response(monkeypatch):
    cfg = make_config()                              # keyless -> yahoo, stooq
    one = [(DAY, 1, 2, 0.5, 1.5, 100)]
    many = [((i + 1) * DAY, 1, 2, 0.5, 1.5, 100) for i in range(80)]
    monkeypatch.setattr(prices, "yahoo_daily", lambda tk, limit=400, rng=None: one)
    monkeypatch.setattr(prices, "stooq_daily", lambda tk, limit=400: many)
    res = prices.daily_bars("AAA", cfg, limit=400)
    assert res is not None and res[1] == "stooq" and len(res[0]) == 80


def test_daily_bars_small_limit_accepts_short_response(monkeypatch):
    cfg = make_config()
    one = [(DAY, 1, 2, 0.5, 1.5, 100)]
    monkeypatch.setattr(prices, "yahoo_daily", lambda tk, limit=400, rng=None: one)
    res = prices.daily_bars("SPY", cfg, limit=5)     # e.g. the LT freshness probe
    assert res is not None and res[1] == "yahoo"


def test_daily_bars_venue_pinning(monkeypatch):
    cfg = make_config()
    called: list[str] = []
    many = [((i + 1) * DAY, 1, 2, 0.5, 1.5, 100) for i in range(80)]
    monkeypatch.setattr(prices, "yahoo_daily",
                        lambda tk, limit=400, rng=None: called.append("yahoo") or many)
    monkeypatch.setattr(prices, "stooq_daily", lambda tk, limit=400: many)
    res = prices.daily_bars("AAA", cfg, limit=400, venue="stooq")
    assert res is not None and res[1] == "stooq"
    assert called == []                              # pinned: other venues never tried
    # pinning to an unconfigured keyed venue fails soft
    assert prices.daily_bars("AAA", cfg, limit=400, venue="alpaca") is None


# --- insider: 4/A amendments --------------------------------------------------

def test_dedupe_amendments_prefers_amendment():
    orig = {"accession": "0001-24-000001-0", "insider": "DOE JOHN", "txn_code": "P",
            "txn_ts": 1000, "shares": 100.0, "price": 10.0, "value": 1000.0,
            "filed_ts": 1000, "form": "4"}
    amend = {**orig, "accession": "0001-24-000002-0", "filed_ts": 2000, "form": "4/A"}
    out = insider._dedupe_amendments([amend, orig])  # newest-first feed order
    assert len(out) == 1 and out[0]["accession"] == "0001-24-000002-0"
    out2 = insider._dedupe_amendments([orig, amend])
    assert len(out2) == 1 and out2[0]["form"] == "4/A"
    # a genuinely different transaction is NOT collapsed
    other = {**orig, "txn_ts": 2000, "accession": "x"}
    assert len(insider._dedupe_amendments([orig, other])) == 2


_FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerTradingSymbol>AAA</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>DOE JOHN</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isOfficer>1</isOfficer><isDirector>0</isDirector></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2025-06-02</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>100</value></transactionShares>
        <transactionPricePerShare><value>10</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""


def test_insider_transactions_collapse_amended_filing(monkeypatch):
    filings = [
        {"cikdir": "1", "acc_nodash": "2", "acc_dashed": "0001-24-000002",
         "form": "4/A", "filed_ts": 2000},
        {"cikdir": "1", "acc_nodash": "1", "acc_dashed": "0001-24-000001",
         "form": "4", "filed_ts": 1000},
    ]
    monkeypatch.setattr(insider, "_recent_filings", lambda cik, ua, count: filings)
    monkeypatch.setattr(insider, "_form4_xml_name", lambda cikdir, acc, ua: "form4.xml")
    monkeypatch.setattr(insider, "get_text", lambda url, **kw: _FORM4_XML)
    rows = insider.insider_transactions("0000000001", "AAA", "ua", since_ts=0)
    assert len(rows) == 1                            # amendment supersedes the original
    assert rows[0]["form"] == "4/A"
    assert rows[0]["accession"].startswith("0001-24-000002")
    assert rows[0]["value"] == 1000.0                # cluster USD counted once


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

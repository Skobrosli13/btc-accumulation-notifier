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

(The stock_backtest / stock_calibrate script tests moved to archive/v1/tests
with the scripts themselves — P3 retirement.)
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.sources.stocks import earnings, insider, prices
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



"""Tests for the SEC EDGAR announcement-date adapter and its wiring into
``earnings.surprise_history`` as the free-tier PEAD fallback.

All deterministic, no network — the EDGAR submissions payload, the ticker->CIK
map, and Finnhub's ``/stock/earnings`` surprises are monkeypatched. Covers:
  * edgar_earnings.announcement_dates — Item-2.02 8-K filtering (non-2.02 8-Ks and
    non-8-K forms excluded), acceptanceDateTime(UTC)->bmo/amc via America/New_York
    (with the JPM-style bmo case that a naive-ET read would misclassify), recent[]
    + files[] shard merge, and one-row-per-fiscal-quarter de-dup.
  * earnings.surprise_history — the free-tier-empty calendar falling back to EDGAR,
    the (year,quarter)/nearest-preceding-period-end join carrying the surprise, the
    return schema, and that a fiscal period-END is NEVER emitted as report_ts.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.sources.stocks import earnings, edgar_earnings


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    """Disable EDGAR + Finnhub call pacing (no real sleeps in tests)."""
    monkeypatch.setattr(edgar_earnings, "_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(edgar_earnings, "_last_call", 0.0)
    monkeypatch.setattr(earnings, "_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(earnings, "_last_call", 0.0)


def _ms(datestr: str) -> int:
    d = datetime.strptime(datestr, "%Y-%m-%d")
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


# --- A realistic AAPL-shaped submissions payload -------------------------------
# recent[] holds the two most-recent quarters; the older two have rolled into a
# files[] history shard. Mixed in: a NON-2.02 8-K (Item 5.02, an officer change)
# and a 10-Q — both must be excluded. AAPL is a Sep-end fiscal year, so a Feb
# announcement reports the prior fiscal-year Q1 etc.; the join is by nearest
# preceding period-END, not calendar alignment.
_RECENT = {
    "form":               ["8-K",                    "10-Q",       "8-K",                    "8-K"],
    "filingDate":         ["2024-08-01",             "2024-08-02", "2024-05-02",             "2024-04-15"],
    "acceptanceDateTime": ["2024-08-01T20:31:00.000Z", "2024-08-02T18:00:00.000Z",
                           "2024-05-02T20:30:00.000Z", "2024-04-15T13:00:00.000Z"],
    "items":              ["2.02,9.01",              "",           "2.02,9.01",              "5.02"],
    "primaryDocument":    ["a8k.htm", "aapl10q.htm", "a8k.htm", "a8k.htm"],
    "reportDate":         ["2024-08-01", "2024-06-29", "2024-05-02", "2024-04-15"],
}
# Older shard: the first two fiscal quarters of the lookback (Nov + Feb releases).
_SHARD = {
    "form":               ["8-K",                    "8-K"],
    "filingDate":         ["2024-02-01",             "2023-11-02"],
    "acceptanceDateTime": ["2024-02-01T21:30:30.000Z", "2023-11-02T20:33:00.000Z"],
    "items":              ["2.02,9.01",              "2.02,9.01"],
    "primaryDocument":    ["a8k.htm", "a8k.htm"],
    "reportDate":         ["2024-02-01", "2023-11-02"],
}
_SUBMISSIONS = {
    "cik": 320193, "name": "Apple Inc.",
    "filings": {"recent": _RECENT, "files": [{"name": "CIK0000320193-submissions-001.json"}]},
}

# Finnhub /stock/earnings surprises, keyed by fiscal (year, quarter); period is the
# fiscal-quarter-END date (weeks BEFORE the announcement). AAPL FY = Sep-end.
_SURPRISES = [
    {"symbol": "AAPL", "period": "2024-06-29", "year": 2024, "quarter": 3,
     "actual": 1.40, "estimate": 1.35, "surprisePercent": 3.7},
    {"symbol": "AAPL", "period": "2024-03-30", "year": 2024, "quarter": 2,
     "actual": 1.53, "estimate": 1.50, "surprisePercent": 2.0},
    {"symbol": "AAPL", "period": "2023-12-30", "year": 2024, "quarter": 1,
     "actual": 2.18, "estimate": 2.10, "surprisePercent": 3.8},
    {"symbol": "AAPL", "period": "2023-09-30", "year": 2023, "quarter": 4,
     "actual": 1.46, "estimate": 1.39, "surprisePercent": 5.0},
]


def _patch_edgar(monkeypatch, submissions=_SUBMISSIONS,
                 cikmap=None):
    """Patch the CIK map + submissions/shard HTTP so announcement_dates is offline."""
    cikmap = cikmap if cikmap is not None else {"AAPL": {"cik": "0000320193", "name": "Apple Inc."}}
    monkeypatch.setattr(edgar_earnings.universe, "sec_ticker_map", lambda ua: cikmap)

    def fake_get_json(url, params=None, headers=None, **kw):
        if "/submissions/CIK" in url and "submissions-" not in url:
            return submissions
        if "submissions-001.json" in url:
            return {"form": _SHARD["form"], "filingDate": _SHARD["filingDate"],
                    "acceptanceDateTime": _SHARD["acceptanceDateTime"],
                    "items": _SHARD["items"], "primaryDocument": _SHARD["primaryDocument"],
                    "reportDate": _SHARD["reportDate"]}
        raise AssertionError(f"unexpected EDGAR URL {url}")

    monkeypatch.setattr(edgar_earnings, "get_json", fake_get_json)


# --- announcement_dates --------------------------------------------------------

def test_announcement_dates_filters_item_202_and_merges_shards(monkeypatch):
    _patch_edgar(monkeypatch)
    rows = edgar_earnings.announcement_dates("AAPL", user_agent="test ua")
    # 4 earnings 8-Ks total (2 recent + 2 shard); the 10-Q and the 5.02 8-K excluded.
    assert len(rows) == 4
    tss = [r["report_ts"] for r in rows]
    assert tss == sorted(tss, reverse=True)                    # newest first
    dates = {datetime.fromtimestamp(r["report_ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
             for r in rows}
    assert dates == {"2024-08-01", "2024-05-02", "2024-02-01", "2023-11-02"}
    # the non-earnings forms never leak in
    assert _ms("2024-04-15") not in tss                        # the 5.02 officer-change 8-K
    assert _ms("2024-08-02") not in tss                        # the 10-Q


def test_announcement_dates_maps_acceptance_to_amc(monkeypatch):
    _patch_edgar(monkeypatch)
    rows = edgar_earnings.announcement_dates("AAPL", user_agent="test ua")
    by_date = {datetime.fromtimestamp(r["report_ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"): r
               for r in rows}
    # 2024-02-01 accept 21:30Z -> 16:30 ET (EST) -> amc
    assert by_date["2024-02-01"]["hour"] == "amc"
    # 2024-08-01 accept 20:31Z -> 16:31 ET (EDT) -> amc
    assert by_date["2024-08-01"]["hour"] == "amc"
    # report_ts is floored to the ET TRADING DATE (midnight UTC), matching the daily
    # bars' granularity so the PEAD reaction lookup aligns — the acceptance instant is
    # kept only to resolve the amc session (above), never emitted as report_ts.
    assert by_date["2024-02-01"]["report_ts"] == _ms("2024-02-01")


def test_session_bmo_uses_utc_to_et_conversion():
    # JPM-style: a 10:45Z acceptance is 06:45 ET (bmo). A naive-ET read (10:45)
    # would call it intraday, so the UTC->ET conversion is load-bearing.
    bmo_ts = int(datetime(2024, 4, 12, 10, 45, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert edgar_earnings._session_from_ts(bmo_ts) == "bmo"
    # 20:00Z -> 16:00 ET -> amc (>= 16:00 cut)
    amc_ts = int(datetime(2024, 4, 12, 20, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert edgar_earnings._session_from_ts(amc_ts) == "amc"
    # 14:00Z -> 10:00 ET -> intraday ''
    mid_ts = int(datetime(2024, 4, 12, 14, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert edgar_earnings._session_from_ts(mid_ts) == ""


def test_announcement_dates_dedupes_extra_202_per_quarter(monkeypatch):
    # A quarter with a preliminary 2.02 AND a corrective 2.02: keep the EARLIEST
    # (the moment the market first learned), one row per fiscal quarter.
    dup = {
        "form": ["8-K", "8-K"],
        "filingDate": ["2024-05-02", "2024-05-06"],
        "acceptanceDateTime": ["2024-05-02T20:30:00.000Z", "2024-05-06T18:00:00.000Z"],
        "items": ["2.02,9.01", "2.02"],
        "primaryDocument": ["a.htm", "b.htm"],
        "reportDate": ["2024-05-02", "2024-05-06"],
    }
    subs = {"cik": 1, "filings": {"recent": dup, "files": []}}
    _patch_edgar(monkeypatch, submissions=subs)
    rows = edgar_earnings.announcement_dates("AAPL", user_agent="ua")
    assert len(rows) == 1
    assert datetime.fromtimestamp(rows[0]["report_ts"] / 1000,
                                  tz=timezone.utc).strftime("%Y-%m-%d") == "2024-05-02"


def test_announcement_dates_no_cik_returns_empty(monkeypatch):
    monkeypatch.setattr(edgar_earnings.universe, "sec_ticker_map", lambda ua: {})
    monkeypatch.setattr(edgar_earnings, "get_json",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no HTTP w/o CIK")))
    assert edgar_earnings.announcement_dates("ZZZZ", user_agent="ua") == []


def test_announcement_dates_failsoft_on_bad_payload(monkeypatch):
    _patch_edgar(monkeypatch, submissions=None)
    assert edgar_earnings.announcement_dates("AAPL", user_agent="ua") == []


# --- surprise_history: free-tier EDGAR fallback --------------------------------

def _patch_finnhub_empty_calendar(monkeypatch):
    """Finnhub: /stock/earnings returns the surprises; every historical
    /calendar/earnings window returns [] (the free-tier signature)."""
    def fake_get_json(url, params=None, headers=None, **kw):
        if "/stock/earnings" in url:
            return _SURPRISES
        if "/calendar/earnings" in url:
            return {"earningsCalendar": []}          # free tier: historical windows empty
        raise AssertionError(f"unexpected Finnhub URL {url}")

    monkeypatch.setattr(earnings, "get_json", fake_get_json)


def test_surprise_history_falls_back_to_edgar_when_calendar_empty(monkeypatch):
    _patch_finnhub_empty_calendar(monkeypatch)
    _patch_edgar(monkeypatch)
    rows = earnings.surprise_history("AAPL", "finnhub-key", years=1.5,
                                     sec_user_agent="test ua")
    assert len(rows) == 4                                       # all four quarters joined
    tss = [r["report_ts"] for r in rows]
    assert tss == sorted(tss, reverse=True)

    by_date = {datetime.fromtimestamp(r["report_ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"): r
               for r in rows}
    # Announcement date is the EDGAR acceptance day (floored to the ET trading date,
    # midnight UTC), NOT the fiscal period end and NOT the intraday acceptance instant.
    aug = by_date["2024-08-01"]
    assert aug["report_ts"] == _ms("2024-08-01")
    assert aug["hour"] == "amc"
    # 2024-08-01 announcement joins the period-end 2024-06-29 surprise (FY24 Q3).
    assert aug["surprise_pct"] == pytest.approx(3.7)
    assert aug["actual"] == 1.40 and aug["estimate"] == 1.35
    # 2024-02-01 announcement joins the 2023-12-30 period-end (FY24 Q1), NOT the
    # 2023-09-30 one — nearest PRECEDING quarter-end (offset fiscal year handled).
    feb = by_date["2024-02-01"]
    assert feb["surprise_pct"] == pytest.approx(3.8)
    assert feb["actual"] == 2.18


def test_surprise_history_edgar_never_emits_a_period_end_as_report_ts(monkeypatch):
    _patch_finnhub_empty_calendar(monkeypatch)
    _patch_edgar(monkeypatch)
    rows = earnings.surprise_history("AAPL", "finnhub-key", years=1.5,
                                     sec_user_agent="test ua")
    period_end_ms = {_ms(s["period"]) for s in _SURPRISES}
    for r in rows:
        assert r["report_ts"] not in period_end_ms             # never the fiscal period end
    # and the quarter-end look-ahead guard reads the map as clean (0% quarter-ends)
    assert earnings.quarter_end_fraction([r["report_ts"] for r in rows]) == 0.0


def test_surprise_history_schema_matches_calendar_path(monkeypatch):
    _patch_finnhub_empty_calendar(monkeypatch)
    _patch_edgar(monkeypatch)
    rows = earnings.surprise_history("AAPL", "finnhub-key", years=1.5,
                                     sec_user_agent="test ua")
    expected = {"ticker", "period", "report_ts", "hour", "actual", "estimate",
                "surprise", "surprise_pct", "rev_actual", "rev_estimate", "rev_surprise_pct"}
    assert rows and set(rows[0]) == expected
    assert all(r["ticker"] == "AAPL" for r in rows)


def test_surprise_history_paid_calendar_path_ignores_edgar(monkeypatch):
    # When /calendar/earnings DOES answer (paid tier), the announcement-date path is
    # kept and EDGAR is never consulted (announcement_dates would raise if it were).
    cal_row = {"symbol": "AAPL", "date": "2024-05-02", "hour": "amc", "quarter": 2,
               "year": 2024, "epsActual": 1.53, "epsEstimate": 1.50}

    def fake_get_json(url, params=None, headers=None, **kw):
        if "/stock/earnings" in url:
            return _SURPRISES
        return {"earningsCalendar": [cal_row]}

    monkeypatch.setattr(earnings, "get_json", fake_get_json)
    monkeypatch.setattr(edgar_earnings, "announcement_dates",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("EDGAR must not run")))
    rows = earnings.surprise_history("AAPL", "finnhub-key", years=0.3,
                                     sec_user_agent="test ua")
    assert rows and rows[0]["report_ts"] == _ms("2024-05-02")
    assert rows[0]["surprise_pct"] == pytest.approx(2.0)


def test_surprise_history_stray_202_does_not_drop_a_clean_ticker(monkeypatch):
    """A mid-quarter guidance Item-2.02 (an extra announcement sharing a fiscal
    quarter's period-end) must NOT sink the coverage guard. The denominator counts
    DISTINCT joinable period-ends, so N clean quarters + 1 stray reads N/N (kept),
    not N/(N+1) (dropped)."""
    _patch_finnhub_empty_calendar(monkeypatch)
    # 4 real quarterly announcements + 1 stray dated mid-Q3 (shares the 2024-06-29
    # period-end with the real 2024-08-01 release). announcement_dates is newest-first.
    stray = [
        {"report_ts": _ms("2024-08-01"), "hour": "amc", "year": 2024, "quarter": 3},
        {"report_ts": _ms("2024-07-15"), "hour": "",    "year": 2024, "quarter": 3},  # stray
        {"report_ts": _ms("2024-05-02"), "hour": "amc", "year": 2024, "quarter": 2},
        {"report_ts": _ms("2024-02-01"), "hour": "amc", "year": 2024, "quarter": 1},
        {"report_ts": _ms("2023-11-02"), "hour": "amc", "year": 2023, "quarter": 4},
    ]
    monkeypatch.setattr(edgar_earnings, "announcement_dates", lambda *a, **k: stray)
    rows = earnings.surprise_history("AAPL", "finnhub-key", years=1.5,
                                     sec_user_agent="test ua")
    # 4 distinct period-ends all matched -> 100% coverage -> ticker KEPT (not dropped),
    # and the stray never double-attaches a surprise.
    assert len(rows) == 4
    assert _ms("2024-07-15") not in {r["report_ts"] for r in rows}


def test_surprise_history_no_edgar_fallback_when_no_surprises(monkeypatch):
    # Free-tier empty calendar AND no /stock/earnings surprises -> nothing to join.
    def fake_get_json(url, params=None, headers=None, **kw):
        if "/stock/earnings" in url:
            return []
        return {"earningsCalendar": []}

    monkeypatch.setattr(earnings, "get_json", fake_get_json)
    _patch_edgar(monkeypatch)
    assert earnings.surprise_history("AAPL", "finnhub-key", years=1.0,
                                     sec_user_agent="test ua") == []

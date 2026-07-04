"""SUE-PEAD event join (SRW-SUE magnitude x EDGAR announcement timing)."""
from __future__ import annotations

from datetime import datetime, timezone

from app.data.equities import pead
from app.data.equities.edgar import xbrl_eps


def _ms(iso: str) -> int:
    return int(datetime.strptime(iso, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def test_period_ends_from_payload():
    payload = {"units": {"USD/shares": [
        {"fy": 2022, "fp": "Q1", "start": "2022-01-01", "end": "2022-03-31", "val": 1.0, "filed": "2022-04-15"},
        {"fy": 2022, "fp": "FY", "start": "2022-01-01", "end": "2022-12-31", "val": 5.0, "filed": "2023-02-15"},
    ]}}
    ends = xbrl_eps.period_ends(payload)
    assert ends[(2022, 1)] == "2022-03-31"
    assert ends[(2022, 4)] == "2022-12-31"      # Q4 end == fiscal-year end


def test_match_events_joins_first_announcement_after_period_end():
    sue = {(2022, 1): 1.5, (2022, 2): -0.8}
    ends = {(2022, 1): "2022-03-31", (2022, 2): "2022-06-30"}
    announcements = [
        {"report_ts": _ms("2022-01-15"), "hour": "amc"},   # before Q1 end -> ignored
        {"report_ts": _ms("2022-04-28"), "hour": "amc"},   # Q1 announcement
        {"report_ts": _ms("2022-07-28"), "hour": "bmo"},   # Q2 announcement
    ]
    ev = pead.match_events(sue, ends, announcements)
    assert [e["quarter"] for e in ev] == [2, 1]            # newest-first
    q1 = next(e for e in ev if e["quarter"] == 1)
    assert q1["sue"] == 1.5 and q1["hour"] == "amc"
    assert q1["report_ts"] == _ms("2022-04-28") and q1["period_end"] == "2022-03-31"


def test_match_events_does_not_double_claim_an_announcement():
    # Only one announcement in-window for two quarters -> the earlier quarter
    # claims it; the later quarter gets nothing (no fabricated pairing).
    sue = {(2022, 1): 1.0, (2022, 2): 2.0}
    ends = {(2022, 1): "2022-03-31", (2022, 2): "2022-06-30"}
    announcements = [{"report_ts": _ms("2022-05-01"), "hour": ""}]
    ev = pead.match_events(sue, ends, announcements)
    assert len(ev) == 1 and ev[0]["quarter"] == 1        # Q1 (earlier end) claims it


def test_match_events_skips_quarter_without_sue():
    sue = {(2022, 2): 2.0}     # no SUE for Q1
    ends = {(2022, 1): "2022-03-31", (2022, 2): "2022-06-30"}
    announcements = [{"report_ts": _ms("2022-04-28"), "hour": ""},
                     {"report_ts": _ms("2022-07-28"), "hour": ""}]
    ev = pead.match_events(sue, ends, announcements)
    assert len(ev) == 1 and ev[0]["quarter"] == 2

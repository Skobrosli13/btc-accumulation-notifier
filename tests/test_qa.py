"""Equities data-quality detectors (§4.8)."""
from __future__ import annotations

from app.data.equities import qa


def test_price_spike_flags_unadjusted_move_but_not_one_with_an_action():
    dates = ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]
    # +100% on 01-05 (no action -> flagged); -50% on 01-07 (a split ACTION -> OK)
    closes = [10.0, 20.0, 21.0, 10.5]
    out = qa.detect_price_spikes("X", dates, closes, action_dates={"2026-01-07"})
    flagged = {f["date"] for f in out}
    assert "2026-01-05" in flagged        # 100% jump, no action
    assert "2026-01-07" not in flagged     # 50% drop but a corporate action explains it


def test_gap_detector_counts_business_days():
    # a 2-week hole (>5 sessions) between 01-05 and 01-20; a normal weekend is fine
    dates = ["2026-01-02", "2026-01-05", "2026-01-20", "2026-01-21"]
    out = qa.detect_gaps("X", dates, max_sessions=5)
    assert len(out) == 1 and out[0]["from"] == "2026-01-05" and out[0]["to"] == "2026-01-20"
    # a clean consecutive series (incl. a weekend gap) yields nothing
    assert qa.detect_gaps("X", ["2026-01-02", "2026-01-05", "2026-01-06"]) == []


def test_stale_fundamental():
    assert qa.detect_stale_fundamental("X", "2024-01-01", "2026-01-01")["age_days"] == 731
    assert qa.detect_stale_fundamental("X", "2025-11-01", "2026-01-01") is None   # fresh
    assert qa.detect_stale_fundamental("X", None, "2026-01-01") is None           # no data


def test_duplicate_permaticker():
    rows = [
        {"ticker": "AAPL", "permaticker": 199059, "isdelisted": "N"},
        {"ticker": "ZZ", "permaticker": 1, "isdelisted": "N"},
        {"ticker": "ZZ", "permaticker": 2, "isdelisted": "N"},     # two LISTED issuers share ZZ
        {"ticker": "OLD", "permaticker": 9, "isdelisted": "Y"},    # delisted -> ignored
    ]
    out = qa.detect_duplicate_permaticker(rows)
    assert len(out) == 1 and out[0]["ticker"] == "ZZ" and out[0]["permatickers"] == [1, 2]

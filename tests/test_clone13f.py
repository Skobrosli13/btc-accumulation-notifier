"""clone13f emitter fixtures — the pre-registered manager filters, pinned."""
from __future__ import annotations

from datetime import datetime, timezone

from app.events import clone13f as c13


def _snap(investor, quarter, tickers, aum=500e6):
    return {"investor": investor, "quarter": quarter, "aum": aum,
            "tickers": set(tickers)}


def _stable_history(investor, base=("A", "B", "C", "D")):
    """Three stable consecutive quarters (2 transitions of ~0 turnover)."""
    return [_snap(investor, "2024-06-30", base),
            _snap(investor, "2024-09-30", base),
            _snap(investor, "2024-12-31", base)]


def test_new_position_by_qualifying_manager_emits_at_deadline():
    snaps = _stable_history("FOCUSED LP") + [
        _snap("FOCUSED LP", "2025-03-31", ("A", "B", "C", "D", "NEW"))]
    ev = c13.cluster_events(snaps)
    assert len(ev) == 1
    e = ev[0]
    assert e["ticker"] == "NEW" and e["direction"] == "LONG"
    # 2025-03-31 + 45d = 2025-05-15
    want = int(datetime(2025, 5, 15, tzinfo=timezone.utc).timestamp() * 1000)
    assert e["event_ts"] == want
    assert e["meta"]["n_managers"] == 1


def test_filters_exclude_big_diversified_churny_and_young():
    # >30 holdings
    many = [f"T{i}" for i in range(31)]
    diversified = [_snap("BIG BOOK", q, many) for q in
                   ("2024-06-30", "2024-09-30", "2024-12-31")]
    diversified.append(_snap("BIG BOOK", "2025-03-31", many + ["NEW"]))
    # AUM out of range (too big / too small)
    whale = _stable_history("WHALE") + [
        _snap("WHALE", "2025-03-31", ("A", "B", "C", "D", "NEW"), aum=50e9)]
    minnow = _stable_history("MINNOW") + [
        _snap("MINNOW", "2025-03-31", ("A", "B", "C", "D", "NEW"), aum=10e6)]
    # churner: rebuilds the book every quarter (turnover >> 25%/yr)
    churn = [_snap("CHURN", "2024-06-30", ("A", "B", "C", "D")),
             _snap("CHURN", "2024-09-30", ("E", "F", "G", "H")),
             _snap("CHURN", "2024-12-31", ("I", "J", "K", "L")),
             _snap("CHURN", "2025-03-31", ("M", "N", "O", "NEW"))]
    # young: first-ever transition (only 1 observed turnover) can't qualify
    young = [_snap("YOUNG", "2024-12-31", ("A", "B")),
             _snap("YOUNG", "2025-03-31", ("A", "B", "NEW"))]
    assert c13.cluster_events(diversified + whale + minnow + churn + young) == []


def test_reporting_gap_breaks_the_chain():
    snaps = [_snap("GAPPY", "2024-03-31", ("A", "B", "C")),
             _snap("GAPPY", "2024-06-30", ("A", "B", "C")),
             # 2024-09-30 missing
             _snap("GAPPY", "2024-12-31", ("A", "B", "C")),
             _snap("GAPPY", "2025-03-31", ("A", "B", "C", "NEW"))]
    # only ONE consecutive transition (24Q4->25Q1) inside the trailing window
    # after the gap -> below MIN_PRIOR_TRANSITIONS -> no event
    assert c13.cluster_events(snaps) == []


def test_clustered_adds_aggregate_strength():
    a = _stable_history("MGR A") + [
        _snap("MGR A", "2025-03-31", ("A", "B", "C", "D", "HOT"))]
    b = _stable_history("MGR B", base=("X", "Y", "Z")) + [
        _snap("MGR B", "2025-03-31", ("X", "Y", "Z", "HOT"))]
    ev = c13.cluster_events(a + b)
    assert len(ev) == 1
    assert ev[0]["ticker"] == "HOT" and ev[0]["strength"] == 2.0
    assert ev[0]["meta"]["n_managers"] == 2


def test_turnover_hand_value():
    # prev {A,B,C,D}, cur {A,B,E}: added 1, dropped 2 -> 3/(2*4) = 0.375
    assert c13.quarterly_turnover({"A", "B", "C", "D"}, {"A", "B", "E"}) == 0.375
    assert c13.quarterly_turnover(set(), {"A"}) is None

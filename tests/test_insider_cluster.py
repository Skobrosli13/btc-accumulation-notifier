"""insider_cluster emitter fixtures — the pre-registered event definition, pinned."""
from __future__ import annotations

from datetime import datetime, timezone

from app.events import insider_cluster as ic


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _buy(ticker, owner, tdate, fdate, usd, title=None, officer="Y", director="N"):
    return {"ticker": ticker, "ownername": owner, "officertitle": title,
            "isofficer": officer, "isdirector": director,
            "transactiondate": tdate, "filingdate": fdate,
            "transactionvalue": usd}


def test_cluster_two_owners_over_50k_emits_at_latest_filing():
    fills = [_buy("AAA", "ALICE", "2024-03-01", "2024-03-02", 30_000.0),
             _buy("AAA", "BOB", "2024-03-08", "2024-03-11", 25_000.0)]
    ev = ic.cluster_events(fills)
    assert len(ev) == 1
    e = ev[0]
    assert e["ticker"] == "AAA" and e["direction"] == "LONG"
    assert e["event_ts"] == _ms("2024-03-11")          # latest FILING, not txn
    assert e["meta"]["agg_usd"] == 55_000.0
    assert e["meta"]["owners"] == ["ALICE", "BOB"]
    assert e["strength"] == 1.0


def test_single_owner_or_small_agg_never_fires():
    assert ic.cluster_events([_buy("AAA", "ALICE", "2024-03-01", "2024-03-02", 1_000_000.0)]) == []
    assert ic.cluster_events([_buy("AAA", "ALICE", "2024-03-01", "2024-03-02", 20_000.0),
                              _buy("AAA", "BOB", "2024-03-05", "2024-03-06", 20_000.0)]) == []


def test_window_gap_resets_cluster():
    fills = [_buy("AAA", "ALICE", "2024-03-01", "2024-03-02", 30_000.0),
             _buy("AAA", "BOB", "2024-03-21", "2024-03-22", 30_000.0)]   # 20d later
    assert ic.cluster_events(fills) == []


def test_cluster_emits_once_per_formation():
    fills = [_buy("AAA", "ALICE", "2024-03-01", "2024-03-02", 30_000.0),
             _buy("AAA", "BOB", "2024-03-05", "2024-03-06", 30_000.0),
             _buy("AAA", "CAROL", "2024-03-09", "2024-03-10", 40_000.0)]  # extends, no re-emit
    ev = ic.cluster_events(fills)
    assert len(ev) == 1
    # ...but a NEW cluster after a quiet gap fires again
    fills += [_buy("AAA", "DAVE", "2024-06-01", "2024-06-02", 30_000.0),
              _buy("AAA", "ERIN", "2024-06-05", "2024-06-06", 30_000.0)]
    assert len(ic.cluster_events(fills)) == 2


def test_routine_insider_excluded():
    # ALICE buys every March for 3 straight years -> her 2024 March buy is routine.
    fills = [_buy("AAA", "ALICE", "2021-03-05", "2021-03-06", 30_000.0),
             _buy("AAA", "ALICE", "2022-03-05", "2022-03-06", 30_000.0),
             _buy("AAA", "ALICE", "2023-03-05", "2023-03-06", 30_000.0),
             _buy("AAA", "ALICE", "2024-03-01", "2024-03-02", 30_000.0),
             _buy("AAA", "BOB", "2024-03-08", "2024-03-11", 25_000.0)]
    # Without ALICE the 2024 cluster is a single owner -> nothing fires in 2024.
    assert ic.cluster_events(fills) == []


def test_ceo_participation_scales_strength():
    fills = [_buy("AAA", "ALICE", "2024-03-01", "2024-03-02", 30_000.0,
                  title="Chief Executive Officer"),
             _buy("AAA", "BOB", "2024-03-08", "2024-03-09", 25_000.0)]
    assert ic.cluster_events(fills)[0]["strength"] == 1.5


def test_non_officer_director_rows_ignored():
    fills = [_buy("AAA", "FUND LP", "2024-03-01", "2024-03-02", 500_000.0,
                  officer="N", director="N"),
             _buy("AAA", "BOB", "2024-03-05", "2024-03-06", 60_000.0)]
    assert ic.cluster_events(fills) == []       # ten-percent owner isn't the signal


def test_value_falls_back_to_price_times_shares():
    fills = [{"ticker": "AAA", "ownername": "ALICE", "officertitle": None,
              "isofficer": "Y", "isdirector": "N", "transactiondate": "2024-03-01",
              "filingdate": "2024-03-02", "transactionvalue": None,
              "transactionpricepershare": 10.0, "transactionshares": 3000},
             _buy("AAA", "BOB", "2024-03-05", "2024-03-06", 25_000.0)]
    ev = ic.cluster_events(fills)
    assert len(ev) == 1 and ev[0]["meta"]["agg_usd"] == 55_000.0

"""Free network-activity adapter — parsing, z-scores, blockchain.com fallback, fail-soft."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.sources import netactivity

# 28 baseline days alternating 90/110 (mean 100, pstdev 10), one closed "latest"
# day at 200 (z = +10), then a trailing still-forming (today) day at 999 that MUST
# be dropped by the date-aware forming-day guard.
_BASE = [90.0 if i % 2 == 0 else 110.0 for i in range(28)]
_ACTIVE = _BASE + [200.0, 999.0]


def _dates_ending_today(n: int):
    today = datetime.now(timezone.utc).date()
    return [today - timedelta(days=(n - 1 - i)) for i in range(n)]   # oldest..today


def _cm_resp(values_by_metric: dict[str, list[float]]) -> dict:
    n = len(next(iter(values_by_metric.values())))
    dates = _dates_ending_today(n)
    rows = []
    for i, d in enumerate(dates):
        row = {"asset": "btc", "time": f"{d.isoformat()}T00:00:00.000000000Z"}
        for m, vals in values_by_metric.items():
            row[m] = str(vals[i])
        rows.append(row)
    return {"data": rows}


def _bc_resp(values: list[float]) -> dict:
    dates = _dates_ending_today(len(values))
    vals = []
    for d, v in zip(dates, values):
        x = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
        vals.append({"x": x, "y": v})
    return {"values": vals}


_ALL_CM = {m: _ACTIVE for m in ("AdrActCnt", "TxCnt", "TxTfrCnt", "AdrBalCnt")}


def test_coinmetrics_parsing_and_zscore(monkeypatch):
    calls: list[str] = []

    def fake(url, *a, **k):
        calls.append(url)
        return _cm_resp(_ALL_CM) if "coinmetrics" in url else None
    monkeypatch.setattr(netactivity, "get_json", fake)
    out = netactivity.netactivity()
    # Latest = the last CLOSED day (200), NOT today's forming day (999, dropped).
    assert out["na_active_addr"] == pytest.approx(200.0)
    assert out["na_active_addr_z"] == pytest.approx(10.0)
    assert out["na_tx_count_z"] == pytest.approx(10.0)
    assert out["na_transfers_z"] == pytest.approx(10.0)
    assert out["na_addr_balance_z"] == pytest.approx(10.0)
    assert set(out) == set(netactivity._NONE)
    # Coin Metrics supplied the core reads, so the blockchain.com fallback never ran.
    assert not any("blockchain.info" in u for u in calls)


def test_blockchain_fallback_for_core_reads(monkeypatch):
    def fake(url, *a, **k):
        if "coinmetrics" in url:
            return None                       # Coin Metrics dark -> fall back
        if "n-unique-addresses" in url:
            return _bc_resp(_ACTIVE)
        if "n-transactions" in url:
            return _bc_resp(_ACTIVE)
        return None
    monkeypatch.setattr(netactivity, "get_json", fake)
    out = netactivity.netactivity()
    assert out["na_active_addr"] == pytest.approx(200.0)   # from blockchain.com
    assert out["na_active_addr_z"] == pytest.approx(10.0)
    assert out["na_tx_count"] == pytest.approx(200.0)
    # blockchain.com only covers the two core reads; the richer ones stay None.
    assert out["na_transfers"] is None and out["na_addr_balance"] is None


def test_fails_soft_all_none(monkeypatch):
    monkeypatch.setattr(netactivity, "get_json", lambda *a, **k: None)
    assert netactivity.netactivity() == netactivity._NONE


def test_zscore_none_without_enough_history(monkeypatch):
    monkeypatch.setattr(netactivity, "get_json",
                        lambda url, *a, **k: _cm_resp({m: [100.0] * 6 for m in _ALL_CM})
                        if "coinmetrics" in url else None)
    out = netactivity.netactivity()
    assert out["na_active_addr"] is not None   # latest still reported
    assert out["na_active_addr_z"] is None      # but no baseline -> no z

"""Hash Ribbon (miner capitulation -> recovery) reading."""
from __future__ import annotations

import time

import pytest

from app.sources import miner

_DAY = 86400


def _daily_ts(n: int, end_offset_days: int = 1) -> list[float]:
    """``n`` daily timestamps ending ``end_offset_days`` before now (1 = the
    last CLOSED day, 0 = includes today's still-forming point)."""
    now = time.time()
    return [now - (n - 1 - i + end_offset_days) * _DAY for i in range(n)]


def _mempool_payload(series: list[float], end_offset_days: int = 1) -> dict:
    ts = _daily_ts(len(series), end_offset_days)
    return {"hashrates": [{"timestamp": t, "avgHashrate": v}
                          for t, v in zip(ts, series)]}


def _blockchain_payload(series: list[float], end_offset_days: int = 1) -> dict:
    ts = _daily_ts(len(series), end_offset_days)
    return {"values": [{"x": t, "y": v} for t, v in zip(ts, series)]}


def test_ribbon_healthy_regime_scores_zero():
    # Monotonic rise: 30d MA always >= 60d MA, never capitulates -> 0.
    series = [float(i) for i in range(1, 201)]
    assert miner._ribbon_score(series) == 0.0


def test_ribbon_capitulation_now_is_partial():
    # A fresh drop drags the 30d below the 60d right now -> stress, not a buy.
    series = [100.0] * 170 + [50.0] * 30
    assert miner._ribbon_score(series) == pytest.approx(0.3)


def test_ribbon_recovery_is_buy_window():
    # Dropped (capitulation) then recovered: 30d back above 60d with a recent
    # capitulation in the lookback -> the classic buy window.
    series = [100.0] * 100 + [50.0] * 40 + [100.0] * 30
    assert miner._ribbon_score(series) == 1.0


def test_ribbon_needs_enough_history():
    assert miner._ribbon_score([1.0] * 40) is None


def test_hash_ribbon_parses_mempool(monkeypatch):
    series = [100.0] * 100 + [50.0] * 40 + [100.0] * 30
    monkeypatch.setattr(miner, "get_json", lambda *a, **k: _mempool_payload(series))
    assert miner.hash_ribbon()["hash_ribbon"] == 1.0


def test_hash_ribbon_falls_back_to_blockchain(monkeypatch):
    series = [100.0] * 170 + [50.0] * 30
    blockchain = _blockchain_payload(series)

    def fake(url, *a, **k):
        if "mempool" in url:
            return None            # primary down
        return blockchain          # fallback up
    monkeypatch.setattr(miner, "get_json", fake)
    assert miner.hash_ribbon()["hash_ribbon"] == pytest.approx(0.3)


def test_mempool_drops_forming_day(monkeypatch):
    # mempool.space includes the in-progress UTC day; a few hours of Poisson
    # block arrivals must not enter the 30/60 SMAs (ribbon flips on noise).
    series = [100.0] * 70
    monkeypatch.setattr(miner, "get_json",
                        lambda *a, **k: _mempool_payload(series, end_offset_days=0))
    assert len(miner._mempool_series()) == 69   # today's partial point excluded


def test_stale_series_is_unusable(monkeypatch):
    # A frozen upstream (last point 10 days old) must read as unavailable, not
    # keep scoring its last ribbon state as current.
    series = [100.0] * 70
    monkeypatch.setattr(miner, "get_json",
                        lambda *a, **k: _mempool_payload(series, end_offset_days=10))
    assert miner._mempool_series() == []


def test_hash_ribbon_none_when_both_venues_stale(monkeypatch):
    series = [100.0] * 200

    def fake(url, *a, **k):
        if "mempool" in url:
            return _mempool_payload(series, end_offset_days=10)
        return _blockchain_payload(series, end_offset_days=10)
    monkeypatch.setattr(miner, "get_json", fake)
    assert miner.hash_ribbon() == {"hash_ribbon": None}


def test_hash_ribbon_fails_soft(monkeypatch):
    monkeypatch.setattr(miner, "get_json", lambda *a, **k: None)
    assert miner.hash_ribbon() == {"hash_ribbon": None}

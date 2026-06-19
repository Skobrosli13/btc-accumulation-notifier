"""Hash Ribbon (miner capitulation -> recovery) reading."""
from __future__ import annotations

import pytest

from app.sources import miner


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
    payload = {"hashrates": [{"timestamp": i, "avgHashrate": v} for i, v in enumerate(series)]}
    monkeypatch.setattr(miner, "get_json", lambda *a, **k: payload)
    assert miner.hash_ribbon()["hash_ribbon"] == 1.0


def test_hash_ribbon_falls_back_to_blockchain(monkeypatch):
    series = [100.0] * 170 + [50.0] * 30
    blockchain = {"values": [{"x": i, "y": v} for i, v in enumerate(series)]}

    def fake(url, *a, **k):
        if "mempool" in url:
            return None            # primary down
        return blockchain          # fallback up
    monkeypatch.setattr(miner, "get_json", fake)
    assert miner.hash_ribbon()["hash_ribbon"] == pytest.approx(0.3)


def test_hash_ribbon_fails_soft(monkeypatch):
    monkeypatch.setattr(miner, "get_json", lambda *a, **k: None)
    assert miner.hash_ribbon() == {"hash_ribbon": None}

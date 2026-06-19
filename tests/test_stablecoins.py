"""Stablecoin Supply Ratio (SSR) adapter."""
from __future__ import annotations

import pytest

from app.sources import stablecoins


def _fake(btc, assets):
    def fn(url, *a, **k):
        if "coingecko" in url:
            return [{"market_cap": btc}] if btc is not None else []
        if "llama" in url:
            return {"peggedAssets": assets}
        return None
    return fn


def test_ssr_sums_only_major_usd(monkeypatch):
    assets = [
        {"symbol": "USDT", "circulating": {"peggedUSD": 180e9}},
        {"symbol": "USDC", "circulating": {"peggedUSD": 60e9}},
        {"symbol": "DAI", "circulating": {"peggedUSD": 5e9}},
        {"symbol": "EURS", "circulating": {"peggedUSD": 1e9}},   # non-USD -> excluded
    ]
    monkeypatch.setattr(stablecoins, "get_json", _fake(1_200e9, assets))
    out = stablecoins.ssr()
    assert out["ssr"] == pytest.approx(1_200e9 / 245e9)   # excludes EURS


def test_ssr_fails_soft_on_missing_btc(monkeypatch):
    monkeypatch.setattr(stablecoins, "get_json", _fake(None, [{"symbol": "USDT", "circulating": {"peggedUSD": 1e9}}]))
    assert stablecoins.ssr() == {"ssr": None}


def test_ssr_fails_soft_on_no_stables(monkeypatch):
    monkeypatch.setattr(stablecoins, "get_json", _fake(1_200e9, []))
    assert stablecoins.ssr() == {"ssr": None}


def test_ssr_fails_soft_on_network_error(monkeypatch):
    monkeypatch.setattr(stablecoins, "get_json", lambda *a, **k: None)
    assert stablecoins.ssr() == {"ssr": None}

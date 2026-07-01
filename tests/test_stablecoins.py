"""Stablecoin Supply Ratio (SSR) adapter — dynamic USD-peg universe."""
from __future__ import annotations

import pytest

from app.sources import stablecoins


def _asset(symbol: str, mcap: float, peg_type: str | None = "peggedUSD") -> dict:
    a = {"symbol": symbol, "circulating": {"peggedUSD": mcap}}
    if peg_type is not None:
        a["pegType"] = peg_type
    return a


def _fake(btc, assets):
    def fn(url, *a, **k):
        if "coingecko" in url:
            return [{"market_cap": btc}] if btc is not None else []
        if "llama" in url:
            return {"peggedAssets": assets}
        return None
    return fn


def test_ssr_dynamic_universe_sums_usd_pegs_above_floor(monkeypatch):
    assets = [
        _asset("USDT", 180e9),
        _asset("USDC", 60e9),
        _asset("USDS", 7e9),                          # post-DAI major: counted now
        _asset("USDe", 5e9),
        _asset("EURS", 2e9, peg_type="peggedEUR"),    # non-USD peg -> excluded
        _asset("TINY", 0.5e9),                        # below the $1bn floor -> excluded
    ]
    monkeypatch.setattr(stablecoins, "get_json", _fake(1_260e9, assets))
    out = stablecoins.ssr()
    assert out["ssr"] == pytest.approx(1_260e9 / 252e9)


def test_ssr_falls_back_to_static_trio_without_pegtype(monkeypatch):
    # If DefiLlama stops carrying pegType, the static USDT/USDC/DAI trio keeps
    # the indicator alive rather than going dark.
    assets = [
        _asset("USDT", 180e9, peg_type=None),
        _asset("USDC", 60e9, peg_type=None),
        _asset("DAI", 5e9, peg_type=None),
        _asset("WEIRD", 50e9, peg_type=None),   # not in the static trio
    ]
    monkeypatch.setattr(stablecoins, "get_json", _fake(1_225e9, assets))
    out = stablecoins.ssr()
    assert out["ssr"] == pytest.approx(1_225e9 / 245e9)


def test_ssr_fails_soft_on_missing_btc(monkeypatch):
    monkeypatch.setattr(stablecoins, "get_json",
                        _fake(None, [_asset("USDT", 2e9)]))
    assert stablecoins.ssr() == {"ssr": None}


def test_ssr_fails_soft_on_no_stables(monkeypatch):
    monkeypatch.setattr(stablecoins, "get_json", _fake(1_200e9, []))
    assert stablecoins.ssr() == {"ssr": None}


def test_ssr_fails_soft_on_network_error(monkeypatch):
    monkeypatch.setattr(stablecoins, "get_json", lambda *a, **k: None)
    assert stablecoins.ssr() == {"ssr": None}

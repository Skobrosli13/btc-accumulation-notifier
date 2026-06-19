"""Free on-chain provider (bitcoin-data.com) parsing + provider precedence."""
from __future__ import annotations

import pytest

from app.sources import onchain

_BD = {
    "mvrv-zscore": {"d": "2026-06-08", "mvrvZscore": 0.34},
    "nupl": {"d": "2026-06-08", "nupl": 0.16},
    "sopr": {"d": "2026-06-08", "sopr": 0.99},
    "puell-multiple": {"d": "2026-06-08", "puellMultiple": 0.60},
    "realized-price": {"d": "2026-06-08", "realizedPrice": 50000.0},
}


def _fake_get_json(responses, calls=None):
    def _fn(url, *a, **k):
        if calls is not None:
            calls.append(url)
        for slug, val in responses.items():
            if slug in url:
                return val
        return None
    return _fn


def test_bitcoin_data_parsing(monkeypatch):
    monkeypatch.setattr(onchain, "get_json", _fake_get_json(_BD))
    out = onchain._from_bitcoin_data(price=60000.0)
    assert out["mvrv_z"] == pytest.approx(0.34)
    assert out["nupl"] == pytest.approx(0.16)
    assert out["sopr"] == pytest.approx(0.99)
    assert out["puell"] == pytest.approx(0.60)
    assert out["realized_ratio"] == pytest.approx(60000.0 / 50000.0)
    # scored keys + context keys (reserve_risk/rhodl, None here since not in responses)
    assert {"mvrv_z", "nupl", "sopr", "puell", "realized_ratio"} <= set(out)
    assert out["reserve_risk"] is None and out["rhodl"] is None


def test_bitcoin_data_fails_soft(monkeypatch):
    monkeypatch.setattr(onchain, "get_json", lambda *a, **k: None)
    out = onchain._from_bitcoin_data(price=60000.0)
    assert all(v is None for v in out.values())


def test_reserve_risk_from_static_file(monkeypatch):
    # reserve_risk is sourced from the rate-cap-free BGeometrics static file
    # ([[ts, value], ...]) and is now a SCORED key, not context-only.
    responses = {**_BD, "files/reserve_risk.json": [[1_700_000_000_000, 0.0015]]}
    monkeypatch.setattr(onchain, "get_json", _fake_get_json(responses))
    out = onchain._from_bitcoin_data(price=60000.0)
    assert out["reserve_risk"] == pytest.approx(0.0015)
    assert out["rhodl"] is None   # REST context metric, absent here


def test_bg_last_handles_malformed(monkeypatch):
    monkeypatch.setattr(onchain, "get_json", lambda *a, **k: [])      # empty list
    assert onchain._bg_last("reserve_risk") is None
    monkeypatch.setattr(onchain, "get_json", lambda *a, **k: None)    # fetch failed
    assert onchain._bg_last("reserve_risk") is None


def test_bg_last_skips_trailing_nulls(monkeypatch):
    # These files carry a trailing null for the current, not-yet-computed day.
    monkeypatch.setattr(onchain, "get_json",
                        lambda *a, **k: [[1, 0.001], [2, 0.002], [3, None]])
    assert onchain._bg_last("reserve_risk") == pytest.approx(0.002)


def test_realized_ratio_needs_price(monkeypatch):
    monkeypatch.setattr(onchain, "get_json", _fake_get_json(_BD))
    out = onchain._from_bitcoin_data(price=None)
    assert out["realized_ratio"] is None
    assert out["mvrv_z"] == pytest.approx(0.34)   # the other four still score


def test_onchain_free_by_default(monkeypatch):
    monkeypatch.delenv("GLASSNODE_API_KEY", raising=False)
    monkeypatch.delenv("ONCHAIN_FREE", raising=False)
    monkeypatch.setattr(onchain, "get_json", _fake_get_json(_BD))
    out = onchain.onchain(price=60000.0)
    assert out["mvrv_z"] == pytest.approx(0.34)
    assert out["realized_ratio"] == pytest.approx(1.2)


def test_glassnode_takes_precedence(monkeypatch):
    monkeypatch.setenv("GLASSNODE_API_KEY", "gn-key")
    calls: list[str] = []
    monkeypatch.setattr(onchain, "get_json", _fake_get_json({}, calls))
    onchain.onchain(price=60000.0)
    assert not any("bitcoin-data" in u for u in calls)   # free feed never hit


def test_onchain_optout_no_http(monkeypatch):
    monkeypatch.delenv("GLASSNODE_API_KEY", raising=False)
    monkeypatch.setenv("ONCHAIN_FREE", "false")
    calls: list[str] = []
    monkeypatch.setattr(onchain, "get_json", _fake_get_json({}, calls))
    out = onchain.onchain(price=60000.0)
    assert out == {"mvrv_z": None, "realized_ratio": None, "nupl": None,
                   "sopr": None, "puell": None}
    assert calls == []   # disabled -> no network at all

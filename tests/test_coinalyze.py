"""Coinalyze adapter: response parsing + fail-soft (no network; get_json mocked)."""
from __future__ import annotations

from app.sources import coinalyze


def _hist(rows):
    return [{"symbol": "BTCUSDT_PERP.A", "history": rows}]


def test_current_open_interest_and_funding(monkeypatch):
    def fake(url, params=None, headers=None, timeout=20):
        assert headers == {"api_key": "k"}
        if url.endswith("/open-interest"):
            return [{"symbol": "BTCUSDT_PERP.A", "value": 12345.6, "update": 1}]
        if url.endswith("/funding-rate"):
            return [{"symbol": "BTCUSDT_PERP.A", "value": 0.0001, "update": 1}]
        raise AssertionError(url)

    monkeypatch.setattr(coinalyze, "get_json", fake)
    assert coinalyze.open_interest("BTCUSDT_PERP.A", "k") == 12345.6
    assert coinalyze.funding_latest("BTCUSDT_PERP.A", "k") == 0.0001


def test_ohlcv_history_maps_fields_and_buyvol(monkeypatch):
    rows = [
        {"t": 1700000000, "o": 100, "h": 110, "l": 95, "c": 105,
         "v": 1000, "bv": 700, "tx": 50, "btx": 30},
        {"t": 1700014400, "o": 105, "h": 108, "l": 101, "c": 102,
         "v": 800, "bv": 300, "tx": 40, "btx": 18},
    ]
    monkeypatch.setattr(coinalyze, "get_json", lambda *a, **k: _hist(rows))
    out = coinalyze.ohlcv_history("BTCUSDT_PERP.A", "4hour", 96, "k")
    assert out[0] == {"ts": 1700000000000, "open": 100.0, "high": 110.0,
                      "low": 95.0, "close": 105.0, "volume": 1000.0, "buyvol": 700.0}
    assert out[1]["ts"] == 1700014400000 and out[1]["buyvol"] == 300.0


def test_liquidations_history_usd(monkeypatch):
    captured = {}

    def fake(url, params=None, headers=None, timeout=20):
        captured["params"] = params
        return _hist([{"t": 1700000000, "l": 1_000_000, "s": 500_000}])

    monkeypatch.setattr(coinalyze, "get_json", fake)
    out = coinalyze.liquidations_history("BTCUSDT_PERP.A", "4hour", 96, "k")
    assert out == [{"ts": 1700000000000, "long": 1_000_000.0, "short": 500_000.0}]
    # USD conversion is requested so long/short are notional dollars.
    assert captured["params"]["convert_to_usd"] == "true"


def test_oi_history_uses_close(monkeypatch):
    monkeypatch.setattr(coinalyze, "get_json",
                        lambda *a, **k: _hist([{"t": 1700000000, "o": 1, "h": 2, "l": 0.5, "c": 1.5}]))
    assert coinalyze.oi_history("BTCUSDT_PERP.A", "4hour", 96, "k") == [
        {"ts": 1700000000000, "oi": 1.5}]


def test_fail_soft_on_none_and_garbage(monkeypatch):
    monkeypatch.setattr(coinalyze, "get_json", lambda *a, **k: None)
    assert coinalyze.open_interest("X", "k") is None
    assert coinalyze.funding_latest("X", "k") is None
    assert coinalyze.ohlcv_history("X", "4hour", 96, "k") == []
    assert coinalyze.liquidations_history("X", "4hour", 96, "k") == []

    # A surprising shape (dict instead of list) must not raise.
    monkeypatch.setattr(coinalyze, "get_json", lambda *a, **k: {"unexpected": 1})
    assert coinalyze.ohlcv_history("X", "4hour", 96, "k") == []
    assert coinalyze.open_interest("X", "k") is None

"""Exchange adapter: Coinbase forming-candle flag, funding 8h-normalization,
Kraken limit cap, and the closed_only contract on the fallback path."""
from __future__ import annotations

import time

import pytest

from app.sources import exchange

_DAY = 86400


def _coinbase_payload(gran: int, n: int, include_forming: bool = True) -> list[list]:
    """Newest-first Coinbase rows [time, low, high, open, close, volume]; the
    newest row is the CURRENT (still-open) bucket when ``include_forming``."""
    cur = (int(time.time()) // gran) * gran  # current bucket open
    start = 0 if include_forming else 1
    return [[cur - i * gran, 99.0, 101.0, 100.0, 100.5, 12.0]
            for i in range(start, n + start)]


def test_coinbase_klines_marks_forming_bucket_unconfirmed(monkeypatch):
    monkeypatch.setattr(exchange, "get_json",
                        lambda *a, **k: _coinbase_payload(_DAY, 5))
    df = exchange._coinbase_klines("1d", "BTC-USDT")
    assert df is not None and len(df) == 5
    assert not bool(df["confirmed"].iloc[-1])   # current bucket = forming
    assert df["confirmed"].iloc[:-1].all()      # all earlier buckets closed
    # closed_only drops exactly the forming bar, matching OKX/Kraken semantics.
    assert len(exchange.closed_only(df)) == 4


def test_coinbase_klines_all_confirmed_when_no_forming_bucket(monkeypatch):
    monkeypatch.setattr(exchange, "get_json",
                        lambda *a, **k: _coinbase_payload(_DAY, 5, include_forming=False))
    df = exchange._coinbase_klines("1d", "BTC-USDT")
    assert df["confirmed"].all()


def test_coinbase_daily_history_flags_forming_and_can_drop_it(monkeypatch):
    payload = _coinbase_payload(_DAY, 10)
    monkeypatch.setattr(exchange, "get_json", lambda *a, **k: payload)
    df = exchange.coinbase_daily_history(10, "BTC-USDT")
    # Default keeps the live snapshot (price structure wants the latest close)
    # but flags it honestly.
    assert len(df) == 10 and not bool(df["confirmed"].iloc[-1])
    closed = exchange.coinbase_daily_history(10, "BTC-USDT", include_forming=False)
    assert len(closed) == 9 and closed["confirmed"].all()


# --- funding_latest: per-8h normalization (mirrors funding_7d_avg) ------------

def _funding_payload(rate: float, gap_ms: int | None) -> dict:
    row = {"fundingRate": str(rate)}
    if gap_ms is not None:
        row["fundingTime"] = "1700000000000"
        row["nextFundingTime"] = str(1_700_000_000_000 + gap_ms)
    return {"code": "0", "data": [row]}


def test_funding_latest_scales_4h_interval_to_8h(monkeypatch):
    # OKX on 4h funding: each print is ~half an 8h rate -> scale up by 2, so the
    # spike triggers / flash leg keep their documented 8h-fraction scale.
    monkeypatch.setattr(exchange, "get_json",
                        lambda *a, **k: _funding_payload(0.0003, 4 * 3600_000))
    assert exchange.funding_latest() == pytest.approx(0.0006)


def test_funding_latest_8h_interval_unchanged(monkeypatch):
    monkeypatch.setattr(exchange, "get_json",
                        lambda *a, **k: _funding_payload(0.0003, 8 * 3600_000))
    assert exchange.funding_latest() == pytest.approx(0.0003)


def test_funding_latest_missing_times_returns_raw(monkeypatch):
    monkeypatch.setattr(exchange, "get_json",
                        lambda *a, **k: _funding_payload(0.0003, None))
    assert exchange.funding_latest() == pytest.approx(0.0003)


def test_funding_latest_implausible_gap_returns_raw(monkeypatch):
    # A 1-second "interval" is a garbage payload; scaling by it would explode
    # the rate by ~5 orders of magnitude.
    monkeypatch.setattr(exchange, "get_json",
                        lambda *a, **k: _funding_payload(0.0003, 1000))
    assert exchange.funding_latest() == pytest.approx(0.0003)


def test_funding_latest_fails_soft(monkeypatch):
    monkeypatch.setattr(exchange, "get_json", lambda *a, **k: None)
    assert exchange.funding_latest() is None


# --- Kraken: respect the requested limit --------------------------------------

def _kraken_payload(n: int) -> dict:
    base = 1_700_000_000
    rows = [[base + i * _DAY, "100", "101", "99", "100.5", "100.2", "10", 5]
            for i in range(n)]
    return {"error": [], "result": {"XXBTZUSD": rows, "last": base + n * _DAY}}


def test_kraken_klines_respects_limit(monkeypatch):
    monkeypatch.setattr(exchange, "get_json", lambda *a, **k: _kraken_payload(10))
    df = exchange._kraken_klines("1d", "BTC-USDT", limit=4)
    assert len(df) == 4
    assert not bool(df["confirmed"].iloc[-1])   # newest row is still the forming one
    assert len(exchange._kraken_klines("1d", "BTC-USDT")) == 10  # no limit -> full


def test_klines_fallback_caps_kraken_window(monkeypatch):
    # A Kraken fallback batch must never be WIDER than the OKX primary's window
    # (persisted candles stay comparable across venues).
    def fake(url, *a, **k):
        if "okx.com" in url:
            return None          # OKX down
        if "kraken.com" in url:
            return _kraken_payload(10)
        return None
    monkeypatch.setattr(exchange, "get_json", fake)
    df = exchange.klines("1d", limit=3, symbol="BTC-USDT")
    assert df.attrs["source"] == "kraken"
    assert len(df) == 3

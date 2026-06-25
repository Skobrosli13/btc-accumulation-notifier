"""CoinDesk/CryptoCompare context adapter — parsing, z-scores, fail-soft, gating."""
from __future__ import annotations

import pytest

from app.sources import coindesk


def _rows_from(fields: dict[str, list[float]]) -> list[dict]:
    """Build daily rows (oldest->newest) from per-field value lists of equal length."""
    n = len(next(iter(fields.values())))
    return [{"time": 1_700_000_000 + i * 86_400,
             **{f: vals[i] for f, vals in fields.items()}} for i in range(n)]


# 28 baseline days alternating 90/110 (mean 100, pstdev 10), one closed "latest"
# day at 200 (z = +10), then a trailing still-forming day (999) that MUST be dropped.
_BASE = [90.0 if i % 2 == 0 else 110.0 for i in range(28)]
_ACTIVE = _BASE + [200.0, 999.0]


def _blockchain_resp(rows):           # blockchain nests the list under Data.Data
    return {"Response": "Success", "Data": {"Aggregated": False, "Data": rows}}


def _social_resp(rows):               # social puts the list directly under Data
    return {"Response": "Success", "Data": rows}


def test_onchain_parsing_and_zscore(monkeypatch):
    rows = _rows_from({k: _ACTIVE for k in
                       ("active_addresses", "large_transaction_count",
                        "new_addresses", "transaction_count")})
    monkeypatch.setattr(coindesk, "get_json", lambda *a, **k: _blockchain_resp(rows))
    out = coindesk.coindesk_onchain("key")
    # Latest = the last CLOSED day (200), NOT the forming day (999, dropped).
    assert out["cd_active_addr"] == pytest.approx(200.0)
    assert out["cd_active_addr_z"] == pytest.approx(10.0)
    assert out["cd_large_tx"] == pytest.approx(200.0)
    assert set(coindesk._ONCHAIN_KEYS) == set(out)


def test_social_parsing_and_zscore(monkeypatch):
    rows = _rows_from({k: _ACTIVE for k in
                       ("reddit_active_users", "reddit_posts_per_day",
                        "reddit_comments_per_day")})
    monkeypatch.setattr(coindesk, "get_json", lambda *a, **k: _social_resp(rows))
    out = coindesk.coindesk_social("key")
    assert out["cd_reddit_active"] == pytest.approx(200.0)
    assert out["cd_social_z"] == pytest.approx(10.0)   # mean of three identical +10 z's


def test_onchain_fails_soft(monkeypatch):
    monkeypatch.setattr(coindesk, "get_json", lambda *a, **k: None)
    assert coindesk.coindesk_onchain("key") == {k: None for k in coindesk._ONCHAIN_KEYS}


def test_social_fails_soft(monkeypatch):
    monkeypatch.setattr(coindesk, "get_json", lambda *a, **k: None)
    assert coindesk.coindesk_social("key") == {k: None for k in coindesk._SOCIAL_KEYS}


def test_zscore_none_without_enough_history(monkeypatch):
    short = _rows_from({"active_addresses": [100.0] * 6})
    monkeypatch.setattr(coindesk, "get_json", lambda *a, **k: _blockchain_resp(short))
    out = coindesk.coindesk_onchain("key")
    assert out["cd_active_addr"] is not None    # latest still reported
    assert out["cd_active_addr_z"] is None       # but no baseline -> no z


def test_coindesk_dark_without_key(monkeypatch):
    monkeypatch.delenv("COINDESK_API_KEY", raising=False)
    calls: list = []
    monkeypatch.setattr(coindesk, "get_json", lambda *a, **k: calls.append(a) or None)
    assert coindesk.coindesk() == coindesk._NONE
    assert calls == []   # no key -> no network at all


def test_coindesk_active_merges_both(monkeypatch):
    monkeypatch.setenv("COINDESK_API_KEY", "cc-key")
    bc = _rows_from({k: _ACTIVE for k in
                     ("active_addresses", "large_transaction_count",
                      "new_addresses", "transaction_count")})
    soc = _rows_from({k: _ACTIVE for k in
                      ("reddit_active_users", "reddit_posts_per_day",
                       "reddit_comments_per_day")})

    def fake(url, *a, **k):
        if "blockchain" in url:
            return _blockchain_resp(bc)
        if "social" in url:
            return _social_resp(soc)
        return None
    monkeypatch.setattr(coindesk, "get_json", fake)
    out = coindesk.coindesk()
    assert out["cd_active_addr_z"] == pytest.approx(10.0)
    assert out["cd_social_z"] == pytest.approx(10.0)
    assert set(coindesk._NONE) == set(out)   # full keyset, no leakage

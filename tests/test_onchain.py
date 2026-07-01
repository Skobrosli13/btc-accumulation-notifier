"""Free on-chain provider (bitcoin-data.com) parsing, freshness guards, and
provider precedence (Glassnode = strict augment, with free fallback)."""
from __future__ import annotations

import time
from datetime import date, timedelta

import pytest

from app.sources import onchain

_TODAY = date.today().isoformat()
_NOW_MS = int(time.time() * 1000)

_BD = {
    "mvrv-zscore": {"d": _TODAY, "mvrvZscore": 0.34},
    "nupl": {"d": _TODAY, "nupl": 0.16},
    "sopr": {"d": _TODAY, "sopr": 0.99},
    "puell-multiple": {"d": _TODAY, "puellMultiple": 0.60},
    "realized-price": {"d": _TODAY, "realizedPrice": 50000.0},
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
    responses = {**_BD, "files/reserve_risk.json": [[_NOW_MS, 0.0015]]}
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
                        lambda *a, **k: [[_NOW_MS - 2 * 86_400_000, 0.001],
                                         [_NOW_MS - 86_400_000, 0.002],
                                         [_NOW_MS, None]])
    assert onchain._bg_last("reserve_risk") == pytest.approx(0.002)


def test_bg_last_stale_returns_none(monkeypatch):
    # A frozen file generator must read as missing, not stale-scored-as-current.
    monkeypatch.setattr(onchain, "get_json",
                        lambda *a, **k: [[_NOW_MS - 10 * 86_400_000, 0.001]])
    assert onchain._bg_last("reserve_risk") is None


def test_cohort_metrics_from_static_files(monkeypatch):
    # LTH/STH-SOPR + LTH-MVRV come from the rate-cap-free static files.
    files = {"lth_sopr": 0.73, "sth_sopr": 0.99, "lth_mvrv": 1.29}

    def fake(url, *a, **k):
        for name, val in files.items():
            if f"files/{name}.json" in url:
                return [[_NOW_MS, val]]
        return None   # REST metrics absent -> None
    monkeypatch.setattr(onchain, "get_json", fake)
    out = onchain._from_bitcoin_data(price=60000.0)
    assert out["lth_sopr"] == pytest.approx(0.73)
    assert out["sth_sopr"] == pytest.approx(0.99)
    assert out["lth_mvrv"] == pytest.approx(1.29)


def test_realized_ratio_needs_price(monkeypatch):
    monkeypatch.setattr(onchain, "get_json", _fake_get_json(_BD))
    out = onchain._from_bitcoin_data(price=None)
    assert out["realized_ratio"] is None
    assert out["mvrv_z"] == pytest.approx(0.34)   # the other four still score


# --- Freshness + shape guards on the REST /last endpoint -----------------------

def test_bd_last_stale_returns_none(monkeypatch):
    old = (date.today() - timedelta(days=10)).isoformat()
    monkeypatch.setattr(onchain, "get_json",
                        lambda *a, **k: {"d": old, "sopr": 0.99})
    assert onchain._bd_last("sopr", "sopr") is None


def test_bd_last_fresh_within_budget(monkeypatch):
    yday = (date.today() - timedelta(days=1)).isoformat()
    monkeypatch.setattr(onchain, "get_json",
                        lambda *a, **k: {"d": yday, "sopr": 0.99})
    assert onchain._bd_last("sopr", "sopr") == pytest.approx(0.99)


def test_bd_last_non_dict_shape_fails_soft(monkeypatch):
    # A list / bare string (error page, API version change) must darken THIS
    # metric only — an escaping AttributeError would take the whole layer down.
    monkeypatch.setattr(onchain, "get_json", lambda *a, **k: ["oops"])
    assert onchain._bd_last("sopr", "sopr") is None
    monkeypatch.setattr(onchain, "get_json", lambda *a, **k: "error page")
    assert onchain._bd_last("sopr", "sopr") is None


def test_one_bad_endpoint_does_not_darken_layer(monkeypatch):
    def fake(url, *a, **k):
        if "mvrv-zscore" in url:
            return ["unexpected shape"]          # one flaky endpoint
        if "files/reserve_risk.json" in url:
            return [[_NOW_MS, 0.002]]
        for slug, val in _BD.items():
            if slug in url:
                return val
        return None
    monkeypatch.setattr(onchain, "get_json", fake)
    out = onchain._from_bitcoin_data(price=60000.0)
    assert out["mvrv_z"] is None                       # only the flaky metric
    assert out["sopr"] == pytest.approx(0.99)          # the rest still score
    assert out["reserve_risk"] == pytest.approx(0.002)


# --- Provider precedence --------------------------------------------------------

def test_onchain_free_by_default(monkeypatch):
    monkeypatch.delenv("GLASSNODE_API_KEY", raising=False)
    monkeypatch.delenv("ONCHAIN_FREE", raising=False)
    monkeypatch.setattr(onchain, "get_json", _fake_get_json(_BD))
    out = onchain.onchain(price=60000.0)
    assert out["mvrv_z"] == pytest.approx(0.34)
    assert out["realized_ratio"] == pytest.approx(1.2)


_GN_FILES = {"reserve_risk": 0.0021, "lth_sopr": 0.88,
             "sth_sopr": 1.01, "lth_mvrv": 1.4}


def _fake_gn_and_files(calls=None):
    gn = {
        "mvrv_z_score": [{"t": 1, "v": 2.5}],
        "price_realized_usd": [{"t": 1, "v": 50000.0}],
        "net_unrealized_profit_loss": [{"t": 1, "v": 0.55}],
        # 7 daily points; the LATEST raw daily (0.97), not the mean, must score.
        "indicators/sopr": [{"t": i, "v": v}
                            for i, v in enumerate([1.0] * 6 + [0.97])],
        "puell_multiple": [{"t": 1, "v": 0.8}],
    }

    def fn(url, *a, **k):
        if calls is not None:
            calls.append(url)
        if "glassnode" in url:
            for path, rows in gn.items():
                if path in url:
                    return rows
            return None
        for name, val in _GN_FILES.items():
            if f"files/{name}.json" in url:
                return [[_NOW_MS, val]]
        return None
    return fn


def test_glassnode_augments_with_free_file_metrics(monkeypatch):
    monkeypatch.setenv("GLASSNODE_API_KEY", "gn-key")
    monkeypatch.delenv("ONCHAIN_FREE", raising=False)
    calls: list[str] = []
    monkeypatch.setattr(onchain, "get_json", _fake_gn_and_files(calls))
    out = onchain.onchain(price=60000.0)
    assert out["mvrv_z"] == pytest.approx(2.5)
    assert out["realized_ratio"] == pytest.approx(60000.0 / 50000.0)
    # The four calibrated cohort metrics ride along from the free static files:
    # a paid key must strictly AUGMENT, never drop scored indicators.
    assert out["reserve_risk"] == pytest.approx(0.0021)
    assert out["lth_sopr"] == pytest.approx(0.88)
    assert out["sth_sopr"] == pytest.approx(1.01)
    assert out["lth_mvrv"] == pytest.approx(1.4)
    # The rate-limited free REST budget stays untouched on the keyed path.
    assert not any("bitcoin-data" in u for u in calls)


def test_glassnode_sopr_is_raw_daily_not_smoothed(monkeypatch):
    monkeypatch.setenv("GLASSNODE_API_KEY", "gn-key")
    monkeypatch.setattr(onchain, "get_json", _fake_gn_and_files())
    out = onchain.onchain(price=60000.0)
    # Latest raw daily value — the definition the committed thresholds were
    # tuned on — NOT the 7d mean (which would be ~0.996 here).
    assert out["sopr"] == pytest.approx(0.97)


def test_keyed_and_free_paths_return_same_key_set(monkeypatch):
    monkeypatch.delenv("GLASSNODE_API_KEY", raising=False)
    monkeypatch.delenv("ONCHAIN_FREE", raising=False)
    monkeypatch.setattr(onchain, "get_json", _fake_get_json(_BD))
    free_keys = set(onchain.onchain(price=60000.0))
    monkeypatch.setenv("GLASSNODE_API_KEY", "gn-key")
    monkeypatch.setattr(onchain, "get_json", _fake_gn_and_files())
    keyed_keys = set(onchain.onchain(price=60000.0))
    assert keyed_keys == free_keys


def test_glassnode_dark_falls_back_to_free_provider(monkeypatch):
    # A keyed configuration must never be LESS resilient than the free one.
    monkeypatch.setenv("GLASSNODE_API_KEY", "gn-key")
    monkeypatch.delenv("ONCHAIN_FREE", raising=False)

    def fake(url, *a, **k):
        if "glassnode" in url:
            return None            # keyed fetch dark (bad key / outage)
        for slug, val in _BD.items():
            if slug in url:
                return val
        return None
    monkeypatch.setattr(onchain, "get_json", fake)
    out = onchain.onchain(price=60000.0)
    assert out["mvrv_z"] == pytest.approx(0.34)   # free values, not all-None


def test_glassnode_exception_falls_back_to_free_provider(monkeypatch):
    monkeypatch.setenv("GLASSNODE_API_KEY", "gn-key")
    monkeypatch.delenv("ONCHAIN_FREE", raising=False)

    def boom(api_key, price):
        raise RuntimeError("glassnode down")
    monkeypatch.setattr(onchain, "_from_glassnode", boom)
    monkeypatch.setattr(onchain, "get_json", _fake_get_json(_BD))
    out = onchain.onchain(price=60000.0)
    assert out["mvrv_z"] == pytest.approx(0.34)


def test_glassnode_with_free_optout_stays_pure_glassnode(monkeypatch):
    # ONCHAIN_FREE=false is an explicit opt-out of the free feed: the keyed path
    # honors it (no BGeometrics/bitcoin-data traffic; cohort metrics read None).
    monkeypatch.setenv("GLASSNODE_API_KEY", "gn-key")
    monkeypatch.setenv("ONCHAIN_FREE", "false")
    calls: list[str] = []
    monkeypatch.setattr(onchain, "get_json", _fake_gn_and_files(calls))
    out = onchain.onchain(price=60000.0)
    assert out["mvrv_z"] == pytest.approx(2.5)
    assert out["reserve_risk"] is None
    assert not any("bgeometrics" in u or "bitcoin-data" in u for u in calls)


def test_onchain_optout_no_http(monkeypatch):
    monkeypatch.delenv("GLASSNODE_API_KEY", raising=False)
    monkeypatch.setenv("ONCHAIN_FREE", "false")
    calls: list[str] = []
    monkeypatch.setattr(onchain, "get_json", _fake_get_json({}, calls))
    out = onchain.onchain(price=60000.0)
    assert out == {"mvrv_z": None, "realized_ratio": None, "nupl": None,
                   "sopr": None, "puell": None}
    assert calls == []   # disabled -> no network at all

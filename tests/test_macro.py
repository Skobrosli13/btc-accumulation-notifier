"""Macro layer: Fed net liquidity + NFCI wiring and date alignment."""
from __future__ import annotations

from datetime import date

import pytest

from app.sources import macro


def test_as_of_picks_last_on_or_before():
    rows = [("2026-01-01", 1.0), ("2026-02-01", 2.0), ("2026-03-01", 3.0)]
    assert macro._as_of(rows, date(2026, 2, 15)) == 2.0
    assert macro._as_of(rows, date(2026, 3, 1)) == 3.0   # inclusive
    assert macro._as_of(rows, date(2025, 12, 1)) is None  # before all rows


def test_net_liquidity_yoy_units_and_alignment(monkeypatch):
    # WALCL/WTREGEN weekly ($M); RRPONTSYD daily ($B -> *1000). The year-ago point
    # is taken as-of (now - 364d) = the 2025-06-18 row here.
    walcl = [("2025-06-18", 6_000_000.0), ("2026-06-17", 6_700_000.0)]
    tga   = [("2025-06-18",   800_000.0), ("2026-06-17",   880_000.0)]
    rrp   = [("2025-06-18",       400.0), ("2026-06-17",       0.25)]
    series = {"WALCL": walcl, "WTREGEN": tga, "RRPONTSYD": rrp}
    monkeypatch.setattr(macro, "_series", lambda sid, key, limit=0: series.get(sid, []))

    level, yoy = macro._net_liquidity("k")
    # now  = 6,700,000 - 880,000 - 0.25*1000 = 5,819,750
    # prev = 6,000,000 - 800,000 - 400*1000  = 4,800,000  (RRP *1000 is the key unit fix)
    assert level == pytest.approx(5_819_750.0)
    assert yoy == pytest.approx((5_819_750.0 / 4_800_000.0 - 1.0) * 100.0)


def test_net_liquidity_missing_series_is_none(monkeypatch):
    monkeypatch.setattr(macro, "_series", lambda sid, key, limit=0: [])
    assert macro._net_liquidity("k") == (None, None)


def test_macro_includes_new_readings(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    monkeypatch.setattr(macro, "_m2_yoy", lambda key: 5.0)
    monkeypatch.setattr(macro, "_net_liquidity", lambda key: (5_000_000.0, 8.0))
    monkeypatch.setattr(macro, "_latest",
                        lambda sid, key: {"NFCI": 0.6}.get(sid))  # others -> None
    out = macro.macro()
    assert out["net_liq_yoy"] == pytest.approx(8.0)
    assert out["net_liq"] == pytest.approx(5_000_000.0)
    assert out["nfci"] == pytest.approx(0.6)
    assert out["m2_yoy"] == pytest.approx(5.0)


def test_macro_no_key_returns_none_readings(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    out = macro.macro()
    assert out["net_liq_yoy"] is None
    assert out["nfci"] is None
    assert out["net_liq"] is None

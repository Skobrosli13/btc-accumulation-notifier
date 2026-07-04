"""QVM factor screener — gate logic + intersection selection, hand-constructed."""
from __future__ import annotations

import pandas as pd

from app.lt import factor_screener as fs


def _name(ticker, *, pe, evebitda, fcf, mktcap, gp, assets, roic, netmargin,
          ncfo, netinc, ncfdiv, ncfcommon, de, currentratio, opinc, mom):
    return {"ticker": ticker, "pe": pe, "evebitda": evebitda, "fcf": fcf,
            "marketcap": mktcap, "gp": gp, "assets": assets, "roic": roic,
            "netmargin": netmargin, "ncfo": ncfo, "netinc": netinc,
            "ncfdiv": ncfdiv, "ncfcommon": ncfcommon, "de": de,
            "currentratio": currentratio, "opinc": opinc, "mom_12_1": mom}


def _universe():
    return pd.DataFrame([
        # cheapest on all value axes, quality, positive momentum, clean gate -> BUY
        _name("GOOD", pe=7, evebitda=5, fcf=200, mktcap=1000, gp=350, assets=1000,
              roic=0.28, netmargin=0.22, ncfo=170, netinc=110, ncfdiv=-25,
              ncfcommon=-45, de=1.0, currentratio=2.2, opinc=130, mom=0.30),
        # cheap but UNPROFITABLE + negative momentum -> value trap, gated out
        _name("TRAP", pe=9, evebitda=6, fcf=40, mktcap=1000, gp=60, assets=1000,
              roic=-0.10, netmargin=-0.05, ncfo=20, netinc=-15, ncfdiv=0,
              ncfcommon=0, de=1.2, currentratio=1.4, opinc=-8, mom=-0.20),
        # high quality + momentum but EXPENSIVE -> passes gate, fails value quintile
        _name("PRICEY", pe=45, evebitda=32, fcf=15, mktcap=3000, gp=500,
              assets=1000, roic=0.35, netmargin=0.28, ncfo=260, netinc=200,
              ncfdiv=-10, ncfcommon=-5, de=0.4, currentratio=3.0, opinc=240,
              mom=0.45),
        # cheap + quality but a SERIAL DILUTER (issuance 25% of mktcap) -> gated
        _name("DILUTE", pe=8, evebitda=5.5, fcf=120, mktcap=1000, gp=300,
              assets=1000, roic=0.20, netmargin=0.18, ncfo=140, netinc=95,
              ncfdiv=0, ncfcommon=250, de=1.0, currentratio=2.0, opinc=110,
              mom=0.25),
        # cheap + profitable but DISTRESSED (current ratio < 1) -> gated
        _name("SICK", pe=7.5, evebitda=5.2, fcf=90, mktcap=1000, gp=200,
              assets=1000, roic=0.12, netmargin=0.10, ncfo=100, netinc=70,
              ncfdiv=-5, ncfcommon=-10, de=5.5, currentratio=0.6, opinc=80,
              mom=0.15),
        # three mediocre fillers so percentiles are meaningful
        _name("MID1", pe=20, evebitda=14, fcf=50, mktcap=1500, gp=180,
              assets=1000, roic=0.10, netmargin=0.09, ncfo=80, netinc=70,
              ncfdiv=-5, ncfcommon=-5, de=1.5, currentratio=1.6, opinc=75,
              mom=0.08),
        _name("MID2", pe=22, evebitda=15, fcf=45, mktcap=1600, gp=160,
              assets=1000, roic=0.09, netmargin=0.08, ncfo=70, netinc=65,
              ncfdiv=-4, ncfcommon=-4, de=1.6, currentratio=1.5, opinc=68,
              mom=0.05),
        _name("MID3", pe=25, evebitda=17, fcf=40, mktcap=1700, gp=150,
              assets=1000, roic=0.08, netmargin=0.07, ncfo=60, netinc=58,
              ncfdiv=-3, ncfcommon=-3, de=1.7, currentratio=1.4, opinc=60,
              mom=0.03),
    ])


def test_gate_flags_each_reason():
    d = fs.compute_pillars(_universe()).set_index("ticker")
    assert d.loc["GOOD", "gate_pass"]
    assert not d.loc["TRAP", "gate_pass"]      # unprofitable + negative momentum
    assert not d.loc["DILUTE", "gate_pass"]    # serial diluter
    assert not d.loc["SICK", "gate_pass"]      # current ratio < 1 AND de > 4
    assert d.loc["PRICEY", "gate_pass"]        # clean gate — filtered later on value


def test_select_returns_the_intersection_only():
    sel = fs.select(_universe(), top_n=30)
    names = list(sel["ticker"])
    assert "GOOD" in names                     # cheap + quality + momentum + gate
    assert "PRICEY" not in names               # too expensive (fails value quintile)
    assert "TRAP" not in names and "DILUTE" not in names and "SICK" not in names


def test_value_pillar_orders_cheap_over_dear():
    d = fs.compute_pillars(_universe()).set_index("ticker")
    assert d.loc["GOOD", "value_pct"] > d.loc["PRICEY", "value_pct"]
    assert d.loc["GOOD", "value_pct"] >= 0.80  # top-quintile cheap


def test_empty_when_nothing_qualifies():
    # a universe of only expensive names -> honest empty list, not a floor
    dear = _universe().copy()
    dear["pe"] = 50
    dear["evebitda"] = 40
    dear["fcf"] = 1
    sel = fs.select(dear, top_n=30)
    assert sel.empty


def test_accruals_quality_prefers_cash_backed_earnings():
    # two identical names except one has CFO >> NI (cash-backed) -> higher quality
    base = _name("A", pe=15, evebitda=10, fcf=50, mktcap=1000, gp=200, assets=1000,
                 roic=0.15, netmargin=0.12, ncfo=90, netinc=80, ncfdiv=-5,
                 ncfcommon=-5, de=1.0, currentratio=1.5, opinc=85, mom=0.1)
    cash = {**base, "ticker": "CASH", "ncfo": 200}      # CFO 200 vs NI 80
    d = fs.compute_pillars(pd.DataFrame([base, cash])).set_index("ticker")
    assert d.loc["CASH", "accruals_quality"] > d.loc["A", "accruals_quality"]

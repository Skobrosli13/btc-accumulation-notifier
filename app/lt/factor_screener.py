"""QVM factor screener (pure) — gate → separate pillars → intersection (§6/§10).

Design follows the long-term plan's research consensus (Fama-French, Novy-Marx,
Piotroski, Sloan): value and quality and momentum are ranked SEPARATELY and a
name is a "long buy" only in the INTERSECTION — cheap AND working AND
quality-safe — never a blended average (which lets a great score on one axis
hide a value trap on another).

Inputs (one row per name, from SF1 ART/TTM as of the rebalance date + a 12-1
momentum column) — see :data:`REQUIRED_COLS`. Everything is a pandas transform
over a cross-section; percentiles are within the passed universe, so the caller
controls the comparison set (the PIT universe for that month).

Pillars:
  * **Gate** (value-trap purge, boolean): profitable, positive gross
    profitability (Novy-Marx floor), not distressed (current ratio, leverage),
    positive 12-1 momentum (the trend/"working" filter), not a serial diluter.
  * **Value** (higher = cheaper): earnings / EBITDA / FCF / shareholder yields.
  * **Quality**: gross profitability, ROIC, net margin, accruals quality
    (cash-backed earnings, Sloan).
  * **Momentum**: 12-1 total return.

Selection: gate-survivors with value in the top ``value_quantile`` AND positive
momentum AND quality in the top half; ranked by the mean of the three pillar
percentiles; top ``top_n``.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

REQUIRED_COLS = (
    "ticker", "pe", "evebitda", "fcf", "marketcap", "gp", "assets", "roic",
    "netmargin", "ncfo", "netinc", "ncfdiv", "ncfcommon", "de", "currentratio",
    "opinc", "mom_12_1",
)

# Gate thresholds (pre-registered; a change is Class B -> lt_factor-v2).
DE_MAX = 4.0                 # debt/equity distress ceiling
CURRENT_RATIO_MIN = 1.0
DILUTION_MAX_FRAC = 0.05     # net share issuance > 5% of mktcap in a year = diluter
VALUE_QUANTILE = 0.80        # top-quintile value to be a buy
QUALITY_MIN_PCT = 0.50       # top-half quality
DEFAULT_TOP_N = 30


def _pct_rank(s: pd.Series) -> pd.Series:
    """Cross-sectional percentile [0,1] (ties averaged); NaN -> neutral 0.5 so a
    single missing sub-factor doesn't disqualify a name."""
    return s.rank(pct=True).fillna(0.5)


def _safe_div(a: pd.Series, b: pd.Series) -> pd.Series:
    out = a / b.replace(0, np.nan)
    return out.replace([np.inf, -np.inf], np.nan)


def compute_pillars(df: pd.DataFrame) -> pd.DataFrame:
    """Add value/quality/momentum sub-metrics + pillar percentiles + gate flag.

    Returns a copy with columns: earn_yield, ebitda_yield, fcf_yield,
    shareholder_yield, gross_prof, accruals_quality, value_pct, quality_pct,
    momentum_pct, combined, gate_pass.
    """
    d = df.copy()
    for c in REQUIRED_COLS:
        if c not in d.columns:
            d[c] = np.nan

    # --- value sub-factors (higher = cheaper) ---
    d["earn_yield"] = 1.0 / d["pe"].where(d["pe"] > 0)
    d["ebitda_yield"] = 1.0 / d["evebitda"].where(d["evebitda"] > 0)
    d["fcf_yield"] = _safe_div(d["fcf"], d["marketcap"])
    # shareholder yield = (dividends paid + net buyback) / mktcap; ncfdiv and a
    # buyback ncfcommon are NEGATIVE cash flows, so negate.
    d["shareholder_yield"] = _safe_div(-(d["ncfdiv"].fillna(0) + d["ncfcommon"].fillna(0)),
                                       d["marketcap"])
    value_cols = ["earn_yield", "ebitda_yield", "fcf_yield", "shareholder_yield"]
    d["value_pct"] = pd.concat([_pct_rank(d[c]) for c in value_cols], axis=1).mean(axis=1)

    # --- quality sub-factors (higher = better) ---
    d["gross_prof"] = _safe_div(d["gp"], d["assets"])          # Novy-Marx
    # accruals quality = cash-backed earnings (Sloan): (CFO - NI)/assets, higher better
    d["accruals_quality"] = _safe_div(d["ncfo"] - d["netinc"], d["assets"])
    qual_cols = ["gross_prof", "roic", "netmargin", "accruals_quality"]
    d["quality_pct"] = pd.concat([_pct_rank(d[c]) for c in qual_cols], axis=1).mean(axis=1)

    # --- momentum ---
    d["momentum_pct"] = _pct_rank(d["mom_12_1"])

    d["combined"] = d[["value_pct", "quality_pct", "momentum_pct"]].mean(axis=1)

    # --- value-trap gate ---
    dilution = _safe_div(d["ncfcommon"], d["marketcap"])       # >0 = net issuance
    d["gate_pass"] = (
        (d["netinc"] > 0) & (d["opinc"] > 0)                   # profitable
        & (d["gross_prof"] > 0)                                # Novy-Marx floor
        & (d["currentratio"] >= CURRENT_RATIO_MIN)
        & (d["de"].fillna(0) <= DE_MAX)                        # not over-levered
        & (d["mom_12_1"] > 0)                                  # trend / "working"
        & (dilution.fillna(0) <= DILUTION_MAX_FRAC)            # not a serial diluter
    )
    return d


def select(df: pd.DataFrame, *, top_n: int = DEFAULT_TOP_N,
           value_quantile: float = VALUE_QUANTILE,
           quality_min_pct: float = QUALITY_MIN_PCT) -> pd.DataFrame:
    """The long-buy list: gate-survivors in the value/quality/momentum
    INTERSECTION, ranked by combined pillar percentile, top ``top_n``.

    Returns the selected rows (with pillar columns) sorted best-first. Empty
    frame when nothing qualifies — an honest 'no buys this month', not a floor.
    """
    d = compute_pillars(df)
    buys = d[
        d["gate_pass"]
        & (d["value_pct"] >= value_quantile)
        & (d["momentum_pct"] > 0)                              # positive-momentum names only
        & (d["mom_12_1"] > 0)
        & (d["quality_pct"] >= quality_min_pct)
    ].copy()
    return buys.sort_values("combined", ascending=False).head(top_n).reset_index(drop=True)

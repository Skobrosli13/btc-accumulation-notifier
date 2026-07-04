"""Position sizing (§7): min(vol-parity, quarter-Kelly, NAV cap) — pure.

    size = min( vol-parity weight at a 15% annual portfolio-vol target,
                0.25 × Kelly from OOS expectancy/variance,
                7% of NAV )

- **Vol-parity**: each of ``n`` concurrent positions gets an equal share of the
  portfolio's vol budget under a zero-correlation idealization —
  w = target_vol / (asset_vol × √n). (Correlation is guarded separately by the
  limits module's 0.6 rejection, not priced here.)
- **Kelly**: continuous-approximation f* = μ/σ² on PER-TRADE fractional
  returns, quartered — Kelly on estimated edges over-bets; 0.25× is the
  pre-registered humility factor.
- **Cap**: 7% NAV for equities; BTC is ONE position capped at 15% NAV (§7).

All inputs are fractions (0.02 = 2%). Missing/degenerate inputs size to 0.0 —
the system never sizes a position it cannot price the risk of.
"""
from __future__ import annotations

import math

TARGET_PORTFOLIO_VOL = 0.15
KELLY_FRACTION = 0.25
NAV_CAP_EQUITY = 0.07
NAV_CAP_BTC = 0.15


def vol_parity_weight(asset_vol_annual: float, n_concurrent: int, *,
                      target_vol: float = TARGET_PORTFOLIO_VOL) -> float:
    """Equal-vol-budget weight: target / (vol × √n). 0 on degenerate inputs."""
    if asset_vol_annual <= 0 or n_concurrent <= 0:
        return 0.0
    return target_vol / (asset_vol_annual * math.sqrt(n_concurrent))


def quarter_kelly(expectancy: float, variance: float, *,
                  fraction: float = KELLY_FRACTION) -> float:
    """0.25 × (μ/σ²) on per-trade fractional returns; 0 when μ ≤ 0 or σ² ≤ 0
    (no edge ⇒ no size; Kelly is never negative here — shorts size their own
    positive-μ leg)."""
    if expectancy <= 0 or variance <= 0:
        return 0.0
    return fraction * (expectancy / variance)


def position_size(*, asset_vol_annual: float, n_concurrent: int,
                  expectancy: float, variance: float,
                  is_btc: bool = False) -> float:
    """The §7 sizing rule: min of the three legs, as a fraction of NAV."""
    cap = NAV_CAP_BTC if is_btc else NAV_CAP_EQUITY
    legs = (vol_parity_weight(asset_vol_annual, n_concurrent),
            quarter_kelly(expectancy, variance),
            cap)
    return max(0.0, min(legs))

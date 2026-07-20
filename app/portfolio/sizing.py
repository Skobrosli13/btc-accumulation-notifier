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

**expectancy=None is NOT expectancy=0.0.** The distinction carries the honesty
rule for the unvalidated sources sharing the paper book (swing picks, long-buys):

  expectancy = 0.0   "measured, and there is no edge"  ⇒ Kelly leg = 0 ⇒ SIZE 0.
  expectancy = None  "never measured — no OOS study"   ⇒ Kelly leg DROPPED; size
                     on vol-parity alone under NAV_CAP_UNVALIDATED.

An unvalidated pick therefore gets *risk* sizing, never *edge* sizing, and its
cap is a third of a validated one. It cannot size itself up by being confident;
only a pre-registered study that cleared the gates earns the Kelly leg.
"""
from __future__ import annotations

import math

TARGET_PORTFOLIO_VOL = 0.15
KELLY_FRACTION = 0.25
NAV_CAP_EQUITY = 0.07
NAV_CAP_BTC = 0.15
NAV_CAP_UNVALIDATED = 0.02   # forward-test sources: no validated edge, small size


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
                  expectancy: float | None, variance: float | None,
                  is_btc: bool = False) -> tuple[float, str]:
    """The §7 sizing rule: min of the legs, as a fraction of NAV.

    Returns ``(size, basis)`` where basis is 'kelly_vol_cap' when a validated
    OOS expectancy priced the Kelly leg, or 'vol_parity_only' when there was
    none to price it with (see the module docstring: None ≠ 0.0). The basis is
    persisted on the position so the book can never later be read as if an
    unvalidated pick had been sized on a measured edge."""
    unvalidated = expectancy is None or variance is None
    if unvalidated:
        cap = NAV_CAP_UNVALIDATED
        legs = (vol_parity_weight(asset_vol_annual, n_concurrent), cap)
        return max(0.0, min(legs)), "vol_parity_only"
    cap = NAV_CAP_BTC if is_btc else NAV_CAP_EQUITY
    legs = (vol_parity_weight(asset_vol_annual, n_concurrent),
            quarter_kelly(expectancy, variance),
            cap)
    return max(0.0, min(legs)), "kelly_vol_cap"

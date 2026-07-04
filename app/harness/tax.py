"""Gross / net / after-tax expectancy (§5.4).

The ALPHA gate requires after-tax net > 0 — an edge that only survives pre-tax
is not an edge for a taxable individual. Convention:

- gains are taxed at the applicable rate; losses generate a credit at the SAME
  rate (assumes gains elsewhere to offset — standard expectancy treatment);
- **short-term** (held <= 365 days: every swing/event study here) uses
  ``st_rate`` (default 0.40); long-term uses ``lt_rate`` (0.24);
- **§1256 contracts** (the CME futures leg of btc_carry) get the blended 60/40
  treatment: 60% long-term + 40% short-term regardless of holding period.

Rates come from config (TAX_ST_RATE / TAX_LT_RATE, owner-set §9).
"""
from __future__ import annotations

DEFAULT_ST_RATE = 0.40
DEFAULT_LT_RATE = 0.24


def after_tax(net: float, *, st_rate: float = DEFAULT_ST_RATE,
              lt_rate: float = DEFAULT_LT_RATE,
              long_term: bool = False, section_1256: bool = False) -> float:
    """After-tax fractional return from a net (cost-adjusted) return."""
    if section_1256:
        rate = 0.60 * lt_rate + 0.40 * st_rate     # blended 60/40
    else:
        rate = lt_rate if long_term else st_rate
    return net * (1.0 - rate)


def expectancy_triplet(gross_returns: list[float], tier: str | None,
                       *, st_rate: float = DEFAULT_ST_RATE,
                       lt_rate: float = DEFAULT_LT_RATE,
                       long_term: bool = False, section_1256: bool = False,
                       cost_overrides: dict | None = None) -> dict:
    """Mean gross / net / after-tax expectancy over a list of per-event gross
    returns — the three numbers every study_results row carries."""
    from .costs import net_return
    if not gross_returns:
        return {"exp_gross": None, "exp_net": None, "exp_after_tax": None}
    nets = [net_return(g, tier, cost_overrides) for g in gross_returns]
    ats = [after_tax(n, st_rate=st_rate, lt_rate=lt_rate,
                     long_term=long_term, section_1256=section_1256) for n in nets]
    n = len(gross_returns)
    return {"exp_gross": sum(gross_returns) / n,
            "exp_net": sum(nets) / n,
            "exp_after_tax": sum(ats) / n}

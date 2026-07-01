"""Fundamental metric computation (pure, I/O-free) from Massive financial statements.

Turns two annual periods (current + prior, newest-first) + price/market cap into the
value / quality / growth signals the long-term engine ranks on. Field names are the
Massive/SEC-standardized statement keys (verified live).

Honest proxies where Massive doesn't break out a line item (SEC top-level statements):
- **OCF yield** used instead of FCF yield (no capex line) — operating cash flow / mktcap.
- **Shareholder yield ≈ net buyback yield** = −(YoY change in diluted share count); the
  strongest single payout component (dividends not separately broken out).
- **Altman RE≈equity** (no retained-earnings line) — distress SCREEN only, not precise.
- Market cap / mktcap-based yields (no cash/total-debt line for a clean EV).

All computations degrade to None on missing data rather than raising.
"""
from __future__ import annotations


def _v(period: dict, statement: str, field: str) -> float | None:
    """Value of financials[statement][field] from a Massive period dict, or None."""
    try:
        node = period["financials"][statement][field]
    except (KeyError, TypeError):
        return None
    val = node.get("value") if isinstance(node, dict) else node
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def _pct_change(cur: float | None, prev: float | None) -> float | None:
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / abs(prev)


def _line(p: dict) -> dict:
    """Extract the raw statement lines we use from one period."""
    return {
        "revenues": _v(p, "income_statement", "revenues"),
        "gross_profit": _v(p, "income_statement", "gross_profit"),
        "operating_income": _v(p, "income_statement", "operating_income_loss"),
        "net_income": _v(p, "income_statement", "net_income_loss"),
        "pretax": _v(p, "income_statement", "income_loss_from_continuing_operations_before_tax"),
        "tax": _v(p, "income_statement", "income_tax_expense_benefit"),
        "diluted_eps": _v(p, "income_statement", "diluted_earnings_per_share"),
        "diluted_shares": _v(p, "income_statement", "diluted_average_shares"),
        "assets": _v(p, "balance_sheet", "assets"),
        "current_assets": _v(p, "balance_sheet", "current_assets"),
        "current_liabilities": _v(p, "balance_sheet", "current_liabilities"),
        "liabilities": _v(p, "balance_sheet", "liabilities"),
        "equity": _v(p, "balance_sheet", "equity"),
        "long_term_debt": _v(p, "balance_sheet", "long_term_debt"),
        "cfo": _v(p, "cash_flow_statement", "net_cash_flow_from_operating_activities"),
    }


def piotroski(cur: dict, prev: dict) -> dict:
    """Piotroski F-score (0-9): profitability, leverage/liquidity, operating efficiency.
    Each unavailable comparison scores 0 (conservative)."""
    roa_c, roa_p = _div(cur["net_income"], cur["assets"]), _div(prev["net_income"], prev["assets"])
    lev_c, lev_p = _div(cur["long_term_debt"], cur["assets"]), _div(prev["long_term_debt"], prev["assets"])
    cr_c, cr_p = _div(cur["current_assets"], cur["current_liabilities"]), _div(prev["current_assets"], prev["current_liabilities"])
    gm_c, gm_p = _div(cur["gross_profit"], cur["revenues"]), _div(prev["gross_profit"], prev["revenues"])
    at_c, at_p = _div(cur["revenues"], cur["assets"]), _div(prev["revenues"], prev["assets"])
    checks = {
        "roa_positive": (roa_c is not None and roa_c > 0),
        "cfo_positive": (cur["cfo"] is not None and cur["cfo"] > 0),
        "roa_rising": (roa_c is not None and roa_p is not None and roa_c > roa_p),
        "accrual_quality": (cur["cfo"] is not None and cur["net_income"] is not None and cur["cfo"] > cur["net_income"]),
        "leverage_falling": (lev_c is not None and lev_p is not None and lev_c < lev_p),
        "liquidity_rising": (cr_c is not None and cr_p is not None and cr_c > cr_p),
        "no_dilution": (cur["diluted_shares"] is not None and prev["diluted_shares"] is not None and cur["diluted_shares"] <= prev["diluted_shares"]),
        "margin_rising": (gm_c is not None and gm_p is not None and gm_c > gm_p),
        "turnover_rising": (at_c is not None and at_p is not None and at_c > at_p),
    }
    return {"score": sum(1 for v in checks.values() if v), "checks": checks}


def altman_z(cur: dict, market_cap: float | None) -> dict | None:
    """Altman Z-score (1968), exact cutoffs >2.99 safe / 1.81-2.99 grey / <1.81 distress.

    Distress SCREEN only. Massive has no retained-earnings line, so X2 uses
    retained_earnings ~= total equity — a proxy that is biased UPWARD (equity >= RE),
    i.e. toward 'safe', and can miss the accumulated-deficit signature X2 is designed
    to catch. Therefore the gate trusts a 'distress' verdict (used only to EXCLUDE)
    and never relies on a proxy-driven 'safe' for inclusion. None if inputs missing."""
    ta = cur["assets"]
    if ta is None or ta == 0:
        return None
    wc = None
    if cur["current_assets"] is not None and cur["current_liabilities"] is not None:
        wc = cur["current_assets"] - cur["current_liabilities"]
    terms = [
        _div(wc, ta), _div(cur["equity"], ta), _div(cur["operating_income"], ta),
        _div(market_cap, cur["liabilities"]), _div(cur["revenues"], ta),
    ]
    coefs = [1.2, 1.4, 3.3, 0.6, 1.0]
    if any(t is None for t in terms):
        return None
    z = sum(c * t for c, t in zip(coefs, terms))
    band = "safe" if z > 2.99 else ("grey" if z >= 1.81 else "distress")
    return {"z": round(z, 2), "band": band}


def compute(periods: list[dict], price: float | None, market_cap: float | None = None,
            shares: float | None = None) -> dict | None:
    """All fundamental metrics from >=2 annual periods (newest first) + price. None if
    insufficient data. market_cap defaults to price*shares (diluted, current period)."""
    if not periods or len(periods) < 2:
        return None
    cur, prev = _line(periods[0]), _line(periods[1])
    if cur["assets"] is None or cur["revenues"] is None:
        return None
    sh = shares or cur["diluted_shares"]
    mktcap = market_cap or (_mult(price, sh))
    if not mktcap or mktcap <= 0:
        return None

    # Effective tax rate, guarded: in a loss year (pretax<=0) or tax-benefit year the
    # raw ratio can go <0 or >1 and flip NOPAT's sign — clamp to a sane range and fall
    # back to the ~statutory rate when pretax is non-positive.
    pretax = cur["pretax"]
    if pretax is not None and pretax > 0:
        tax_rate = min(max(_div(cur["tax"], pretax) or 0.21, 0.0), 0.35)
    else:
        tax_rate = 0.21
    nopat = (cur["operating_income"] * (1 - tax_rate)) if cur["operating_income"] is not None else None
    invested_capital = None
    if cur["equity"] is not None and cur["long_term_debt"] is not None:
        invested_capital = cur["equity"] + cur["long_term_debt"]

    metrics = {
        # value yields (higher = cheaper)
        "earnings_yield": _div(cur["net_income"], mktcap),
        "ocf_yield": _div(cur["cfo"], mktcap),          # FCF proxy (no capex line)
        "sales_yield": _div(cur["revenues"], mktcap),
        "book_yield": _div(cur["equity"], mktcap),
        "shareholder_yield": _neg(_pct_change(cur["diluted_shares"], prev["diluted_shares"])),  # net buyback proxy
        # quality
        "gross_profitability": _div(cur["gross_profit"], cur["assets"]),   # Novy-Marx
        "roe": _div(cur["net_income"], cur["equity"]),
        "roa": _div(cur["net_income"], cur["assets"]),
        "roic": _div(nopat, invested_capital),
        "gross_margin": _div(cur["gross_profit"], cur["revenues"]),
        "operating_margin": _div(cur["operating_income"], cur["revenues"]),
        "net_margin": _div(cur["net_income"], cur["revenues"]),
        "debt_to_equity": _div(cur["long_term_debt"], cur["equity"]),
        "current_ratio": _div(cur["current_assets"], cur["current_liabilities"]),
        "accruals": _div((cur["net_income"] - cur["cfo"]) if (cur["net_income"] is not None and cur["cfo"] is not None) else None, cur["assets"]),
        "asset_growth": _pct_change(cur["assets"], prev["assets"]),
        # growth
        "revenue_growth": _pct_change(cur["revenues"], prev["revenues"]),
        "eps_growth": _pct_change(cur["diluted_eps"], prev["diluted_eps"]),
        "margin_trend": (_sub(_div(cur["operating_income"], cur["revenues"]), _div(prev["operating_income"], prev["revenues"]))),
    }
    metrics["piotroski"] = piotroski(cur, prev)
    metrics["altman"] = altman_z(cur, mktcap)
    metrics["market_cap"] = mktcap
    metrics["fiscal_period"] = periods[0].get("fiscal_period")
    metrics["fiscal_year"] = periods[0].get("fiscal_year")
    metrics["end_date"] = periods[0].get("end_date")
    return metrics


def _mult(a, b):
    return (a * b) if (a is not None and b is not None) else None


def _neg(x):
    return (-x) if x is not None else None


def _sub(a, b):
    return (a - b) if (a is not None and b is not None) else None

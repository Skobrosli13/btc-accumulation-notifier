"""Pre-registered promotion gates (§5.5) — pure verdict functions.

Only these functions decide PROMOTED / EXTEND / KILLED; the UI renders whatever
they return (labels come from verdicts, never hardcoded — prime directive 3).
Every verdict carries its ``reasons`` so a KILLED study can always explain
itself. Changing ANY threshold here is a Class-C plan amendment (§9.5).

Codified EXTEND rule (ALPHA): hard failures — negative after-tax expectancy, a
dirty placebo, sign inconsistency across the structural split — are KILLED
outright (more data won't fix a broken premise). Soft misses — t or sample size
below bar with everything else intact — earn ONE extension; a study that
already extended and still misses is KILLED.
"""
from __future__ import annotations

# ALPHA thresholds (§5.5).
ALPHA_MIN_T = 3.0
ALPHA_MIN_MONTHS = 12
ALPHA_MIN_EVENTS = 100          # default cadence
ALPHA_MIN_EVENTS_QUARTERLY = 60  # quarterly-cadence studies (e.g. earnings)
ALPHA_MIN_EVENTS_BTC_TS = 40     # BTC ts-studies (de-correlated events)

# lt_factor (portfolio evaluator) thresholds.
LT_MIN_T = 2.0
LT_MIN_MONTHS = 36


def alpha_verdict(*, t_clustered: float | None, n_months: int, n_events: int,
                  exp_after_tax: float | None, sign_consistent: bool,
                  placebo_clean: bool, min_events: int = ALPHA_MIN_EVENTS,
                  already_extended: bool = False) -> dict:
    """ALPHA gate on the combined OOS+LIVE population at the primary horizon."""
    hard, soft = [], []
    if exp_after_tax is None or exp_after_tax <= 0:
        hard.append("after-tax expectancy not positive")
    if not placebo_clean:
        hard.append("placebo suite not clean")
    if not sign_consistent:
        hard.append("sign not consistent across structural split")
    if t_clustered is None or t_clustered < ALPHA_MIN_T:
        soft.append(f"clustered t {t_clustered} < {ALPHA_MIN_T}")
    if n_months < ALPHA_MIN_MONTHS:
        soft.append(f"n_months {n_months} < {ALPHA_MIN_MONTHS}")
    if n_events < min_events:
        soft.append(f"n_events {n_events} < {min_events}")

    if hard:
        return {"status": "KILLED", "reasons": hard + soft}
    if not soft:
        return {"status": "PROMOTED", "reasons": []}
    if already_extended:
        return {"status": "KILLED", "reasons": soft + ["already extended once"]}
    return {"status": "EXTEND", "reasons": soft}


def policy_verdict(*, overlay_return: float, baseline_return: float,
                   overlay_maxdd: float, baseline_maxdd: float,
                   forward_overlay_return: float | None = None,
                   forward_baseline_return: float | None = None,
                   forward_overlay_maxdd: float | None = None,
                   forward_baseline_maxdd: float | None = None) -> dict:
    """POLICY gate (§5.5): no harm vs the naive baseline + drawdown improvement,
    in BOTH the backtest and (when supplied) the rolling forward window.
    Drawdowns are magnitudes (positive = worse). Pass keeps the 'discipline
    overlay — not alpha' label; any failed leg demotes to unscored context."""
    reasons = []
    if overlay_return < baseline_return:
        reasons.append("backtest: overlay return < baseline")
    if overlay_maxdd >= baseline_maxdd:
        reasons.append("backtest: overlay max drawdown not smaller")
    if forward_overlay_return is not None:
        if forward_overlay_return < (forward_baseline_return or 0.0):
            reasons.append("forward: overlay return < baseline")
        if forward_overlay_maxdd is not None and \
                forward_overlay_maxdd >= (forward_baseline_maxdd or 0.0):
            reasons.append("forward: overlay max drawdown not smaller")
    return {"status": "PROMOTED" if not reasons else "WATCHLIST",
            "reasons": reasons}


def premium_verdict(*, net_annualized_carry: float, tbill_rate: float,
                    forced_liquidations: int, min_margin_ratio: float,
                    premium_over_tbill: float = 0.02,
                    required_margin_ratio: float = 2.0) -> dict:
    """PREMIUM gate (btc_carry, §5.5): realized net annualized carry — after
    costs, margin drag and §1256 tax — must beat T-bills by >= 2pp over the
    paper window, with zero forced liquidations and modeled margin >= 2×
    maintenance throughout. Fails ⇒ KILLED (T-bills win)."""
    reasons = []
    if net_annualized_carry < tbill_rate + premium_over_tbill:
        reasons.append(
            f"net carry {net_annualized_carry:.4f} < tbill+{premium_over_tbill:.2%}")
    if forced_liquidations > 0:
        reasons.append(f"{forced_liquidations} forced liquidation(s)")
    if min_margin_ratio < required_margin_ratio:
        reasons.append(f"margin ratio {min_margin_ratio:.2f} < {required_margin_ratio}")
    return {"status": "PROMOTED" if not reasons else "KILLED", "reasons": reasons}


def lt_factor_verdict(*, t_vs_universe: float | None, t_vs_etf: float | None,
                      n_months: int) -> dict:
    """lt_factor gate (§5.5): OOS active return clustered t >= 2 over >= 36
    months vs BOTH the equal-weight PIT universe AND the 50/50 value+quality ETF
    proxy ⇒ scored; else 'Watchlist (unscored factor screen)'. No third state."""
    reasons = []
    if n_months < LT_MIN_MONTHS:
        reasons.append(f"n_months {n_months} < {LT_MIN_MONTHS}")
    if t_vs_universe is None or t_vs_universe < LT_MIN_T:
        reasons.append(f"t vs universe {t_vs_universe} < {LT_MIN_T}")
    if t_vs_etf is None or t_vs_etf < LT_MIN_T:
        reasons.append(f"t vs ETF proxy {t_vs_etf} < {LT_MIN_T}")
    return {"status": "PROMOTED" if not reasons else "WATCHLIST", "reasons": reasons}

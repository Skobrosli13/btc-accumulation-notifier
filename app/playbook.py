"""Derived 'playbook' helpers — pure, display-only.

They translate a score/tier + short-term regime into an *illustrative* action plan
(a conviction-scaled accumulation ladder + a unified "what to do now" stance). They
NEVER change the score and never touch I/O. Every output carries a disclaimer.
"""
from __future__ import annotations

DISCLAIMER = ("Illustrative only — not financial advice. You decide whether, how "
              "much, and where.")

# Total share of an accumulation budget to deploy "now", by tier: (base%, conviction-range%).
# Conviction scales within the band; WATCH/NEUTRAL deploy nothing (not an accumulation zone yet).
_TIER_DEPLOY = {"ACCUMULATE": (25.0, 25.0), "DEEP_VALUE": (50.0, 50.0)}


def conviction(composite: float, tier: str, t_watch: float, t_acc: float,
               t_deep: float) -> float:
    """0..1 — how deep into the CURRENT tier's band the composite sits."""
    floor, ceil = {
        "NEUTRAL": (0.0, t_watch),
        "WATCH": (t_watch, t_acc),
        "ACCUMULATE": (t_acc, t_deep),
        "DEEP_VALUE": (t_deep, 100.0),
    }.get(tier, (0.0, 100.0))
    if ceil <= floor:
        return 1.0
    return max(0.0, min(1.0, (composite - floor) / (ceil - floor)))


def laddering_plan(*, composite: float, tier: str, conviction_: float,
                   price: float | None, wma200: float | None,
                   realized_price: float | None, atr_daily: float | None,
                   budget_label: str = "your accumulation budget") -> dict | None:
    """Illustrative accumulation ladder: deploy a conviction-scaled share now, then
    ladder the remainder at lower price anchors (200WMA, realized price, -1.5*ATR).
    Returns None for NEUTRAL/WATCH (no accumulation zone yet)."""
    if tier not in _TIER_DEPLOY or price is None:
        return None
    base, rng = _TIER_DEPLOY[tier]
    now_pct = round(base + rng * max(0.0, min(1.0, conviction_)), 0)

    # Lower-price anchors to ladder the remaining budget into (only those below spot).
    anchors = [("200-week MA", wma200), ("Realized price", realized_price)]
    if atr_daily:
        anchors.append(("-1.5×ATR", price - 1.5 * atr_daily))
    below = [(lab, p) for lab, p in anchors if p is not None and p < price]

    tranches = [{"label": "now", "price": round(price, 0), "pct": now_pct}]
    remaining = max(0.0, 100.0 - now_pct)
    if below and remaining > 0:
        slice_pct = round(remaining / len(below), 0)
        for lab, p in below:
            tranches.append({"label": lab, "price": round(p, 0), "pct": slice_pct})

    return {
        "tier": tier,
        "conviction": round(conviction_, 2),
        "deploy_now_pct": now_pct,
        "budget_label": budget_label,
        "tranches": tranches,
        "note": (f"Deploy ~{now_pct:.0f}% of {budget_label} now; ladder the rest into "
                 "lower anchors if price gets there."),
        "disclaimer": DISCLAIMER,
    }


# Short-term states considered oversold/bullish-timing vs hot/overbought.
_OVERSOLD = {"SELL", "STRONG_SELL"}
_OVERBOUGHT = {"BUY", "STRONG_BUY"}


def what_to_do_now(*, long_tier: str, long_conviction: float, st_state: str | None,
                   st_triggers: list[dict] | None) -> dict:
    """Unified stance combining the long-term tier with the short-term regime.

    The long-term thesis is buy-only accumulation; the short-term read only times
    the entry (it never invalidates the long thesis). Returns a stance + rationale.
    """
    triggers = st_triggers or []
    has_buy_trigger = any(t.get("direction") == "BUY" for t in triggers)
    st = st_state or "NEUTRAL"

    if long_tier in ("ACCUMULATE", "DEEP_VALUE"):
        if st in _OVERSOLD or has_buy_trigger:
            stance, action = "higher-conviction entry", "Deploy a tranche per the ladder."
            rationale = ("Long-term reads cheap (accumulation zone) AND short-term is "
                         "washed out — the two align.")
        elif st in _OVERBOUGHT:
            stance, action = "in the zone but hot", "Scale in slowly / wait for a dip."
            rationale = ("Long-term is in the accumulation zone but short-term is "
                         "extended — a pullback is likely a better entry.")
        else:
            stance, action = "accumulate", "Deploy a tranche per the ladder."
            rationale = "Long-term is in the accumulation zone; short-term is neutral."
    elif long_tier == "WATCH":
        stance, action = "prepare, don't deploy", "Set alerts; wait for ACCUMULATE."
        rationale = "Indicators are starting to align but the zone isn't confirmed yet."
    else:  # NEUTRAL
        stance, action = "no accumulation signal", "Nothing to do on the long-term thesis."
        rationale = "Long-term confluence is not in an accumulation zone."

    return {"stance": stance, "suggested_action": action, "rationale": rationale,
            "long_tier": long_tier, "short_term_state": st, "disclaimer": DISCLAIMER}

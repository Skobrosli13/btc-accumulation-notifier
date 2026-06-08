"""Composite "Accumulation Confidence" scoring (0-100).

Indicators are grouped into orthogonal categories. Each indicator maps to a
sub-score in [0,1] where 0 = "not in bottom zone" and 1 = "deep in bottom zone"
via ``linear_score``. Category scores are the mean of their available
sub-scores; the composite is a weight-renormalized sum scaled by a cycle-timing
multiplier and clamped to 0-100.

Thresholds below are *starting defaults* (n=3 cycles — expect imperfection).
They live here in one place so they are easy to tune and so ``scripts/backtest.py``
can import and sanity-check them against history.
"""
from __future__ import annotations

from datetime import date

# --- Per-indicator thresholds: (neutral, extreme) -----------------------------
# Read linear_score's docstring: for "lower = more bullish" metrics neutral > extreme;
# for "higher = more bullish" metrics neutral < extreme.
THRESHOLDS: dict[str, dict[str, float]] = {
    # On-chain valuation (lower = more bullish)
    "mvrv_z":          {"neutral": 2.0,   "extreme": 0.0},
    "realized_ratio":  {"neutral": 1.3,   "extreme": 0.8},
    "nupl":            {"neutral": 0.25,  "extreme": -0.1},
    "sopr":            {"neutral": 1.0,   "extreme": 0.95},
    "puell":           {"neutral": 0.6,   "extreme": 0.3},
    # Price structure (lower = more bullish)
    "price_to_wma200": {"neutral": 1.10,  "extreme": 0.85},
    "mayer":           {"neutral": 1.0,   "extreme": 0.5},
    # Macro / liquidity
    "m2_yoy":          {"neutral": 0.0,   "extreme": 8.0},    # % YoY; expanding -> bullish (higher)
    "hy_spread":       {"neutral": 3.5,   "extreme": 8.0},    # OAS %; wide risk-off -> capitulation (higher)
    "real_yield":      {"neutral": 2.0,   "extreme": 0.0},    # %; falling -> bullish (lower)
    "etf_flow":        {"neutral": 0.0,   "extreme": 5.0},    # $bn 30d net; persistent inflows -> bullish (higher)
    # Sentiment (lower = more bullish)
    "fng":             {"neutral": 40.0,  "extreme": 10.0},
    # Derivatives
    "funding":         {"neutral": 0.0,   "extreme": -0.0003},  # 8h funding fraction; negative -> bullish (lower)
    "oi_flush":        {"neutral": 0.0,   "extreme": -25.0},    # % OI change over window; big drop -> bullish (lower)
    "liq_magnitude":   {"neutral": 0.0,   "extreme": 2.0},      # $bn 24h aggregate liqs; large -> capitulation (higher)
}

# Which indicators belong to which category.
CATEGORY_INDICATORS: dict[str, list[str]] = {
    "onchain":   ["mvrv_z", "realized_ratio", "nupl", "sopr", "puell"],
    "price":     ["price_to_wma200", "mayer"],
    "macro":     ["m2_yoy", "hy_spread", "real_yield", "etf_flow"],
    "sentiment": ["fng"],
    "derivs":    ["funding", "oi_flush", "liq_magnitude"],
}

# Human-readable labels for alert text.
INDICATOR_LABELS: dict[str, str] = {
    "mvrv_z": "MVRV Z-Score",
    "realized_ratio": "Realized-price ratio",
    "nupl": "NUPL",
    "sopr": "SOPR (7d)",
    "puell": "Puell Multiple",
    "price_to_wma200": "Price / 200-week MA",
    "mayer": "Mayer Multiple",
    "m2_yoy": "M2 YoY",
    "hy_spread": "HY credit spread",
    "real_yield": "10Y real yield",
    "etf_flow": "ETF net flows",
    "fng": "Fear & Greed",
    "funding": "Funding rate (7d)",
    "oi_flush": "OI deleveraging",
    "liq_magnitude": "Liquidation cascade",
}

# A sub-score at or above this is reported as "in its bottom zone".
IN_ZONE_THRESHOLD = 0.6

# Cycle-timing multiplier: beyond this many days from the typical bottom window
# the multiplier bottoms out at 0.9.
CYCLE_WINDOW_HALFWIDTH_DAYS = 200


# --- Reference helpers (implemented as written in the spec) -------------------

def linear_score(value: float, neutral: float, extreme: float) -> float:
    """Map to [0,1]. For 'lower = more bullish' metrics, pass neutral > extreme.
    For 'higher = more bullish', pass neutral < extreme. Handles both via direction."""
    if neutral == extreme:
        return 0.0
    lo, hi = sorted((neutral, extreme))
    t = (value - lo) / (hi - lo)          # 0..1 across the band
    score = t if extreme > neutral else 1 - t
    return max(0.0, min(1.0, score))


def category_score(subscores: dict[str, float | None]) -> float | None:
    """Mean of available sub-scores; None if the whole category is unavailable."""
    vals = [s for s in subscores.values() if s is not None]
    return sum(vals) / len(vals) if vals else None


def composite(category_scores: dict[str, float | None],
              weights: dict[str, float],
              cycle_multiplier: float) -> tuple[float, list[str]]:
    """Weighted sum over AVAILABLE categories, weights renormalized. Returns (0-100, active list)."""
    active = {k: v for k, v in category_scores.items() if v is not None}
    if not active:
        return 0.0, []
    wsum = sum(weights[k] for k in active)
    score = sum((weights[k] / wsum) * active[k] for k in active) * cycle_multiplier
    return max(0.0, min(100.0, score * 100)), sorted(active.keys())


def tier(score: float, price: float, wma200: float | None,
         t_watch: float, t_acc: float, t_deep: float) -> str:
    if score >= t_deep and (wma200 is not None and price <= wma200):
        return "DEEP_VALUE"
    if score >= t_acc:
        return "ACCUMULATE"
    if score >= t_watch:
        return "WATCH"
    return "NEUTRAL"


# --- Built on top of the reference helpers -----------------------------------

def score_indicators(readings: dict[str, float | None]) -> dict[str, float | None]:
    """Map raw indicator readings to [0,1] sub-scores using THRESHOLDS.

    A missing key or a None value yields a None sub-score (treated as
    unavailable downstream). Unknown keys in ``readings`` are ignored.
    """
    out: dict[str, float | None] = {}
    for name, th in THRESHOLDS.items():
        value = readings.get(name)
        if value is None:
            out[name] = None
        else:
            out[name] = linear_score(float(value), th["neutral"], th["extreme"])
    return out


def category_scores(subscores: dict[str, float | None]) -> dict[str, float | None]:
    """Aggregate per-indicator sub-scores into per-category scores."""
    return {
        cat: category_score({ind: subscores.get(ind) for ind in inds})
        for cat, inds in CATEGORY_INDICATORS.items()
    }


def cycle_multiplier(today: date, ath_date: date, peak_to_trough_days: int,
                     halfwidth_days: int = CYCLE_WINDOW_HALFWIDTH_DAYS) -> float:
    """Context-only multiplier in [0.9, 1.1] from proximity to the typical bottom window.

    Closer to (ath_date + peak_to_trough_days) -> toward 1.1; far -> toward 0.9.
    This only *weights* confluence; it never creates a signal on its own.
    """
    days_since_peak = (today - ath_date).days
    distance = abs(days_since_peak - peak_to_trough_days)
    frac = min(1.0, distance / max(1, halfwidth_days))
    return 1.1 - 0.2 * frac


def indicators_in_zone(subscores: dict[str, float | None],
                       threshold: float = IN_ZONE_THRESHOLD) -> list[str]:
    """Labels of indicators whose sub-score is at/above the bottom-zone threshold."""
    return [
        INDICATOR_LABELS.get(name, name)
        for name, s in subscores.items()
        if s is not None and s >= threshold
    ]

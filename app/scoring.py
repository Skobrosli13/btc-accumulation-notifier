"""Composite "Accumulation Confidence" scoring (0-100).

Indicators are grouped into orthogonal categories. Each indicator maps to a
sub-score in [0,1] where 0 = "not in bottom zone" and 1 = "deep in bottom zone"
via ``linear_score``. Category scores are the mean of their available
sub-scores; the composite is a weight-renormalized sum scaled by a cycle-timing
multiplier and clamped to 0-100.

Sub-scores come from one of two mappings, per indicator:
  * **percentile-rank** against history when ``app/calibration.json`` carries
    breakpoints for that indicator (empirical, regime-adaptive); else
  * the fixed ``THRESHOLDS`` ``linear_score`` (economic-logic defaults).
``scripts/calibrate.py`` emits the calibration file offline; the live path only
does a pure interpolation lookup. With no calibration file present the scoring is
byte-identical to the legacy threshold behavior.
"""
from __future__ import annotations

import bisect
import json
from datetime import date
from pathlib import Path

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
    # % YoY; expanding -> bullish (higher). Post-2010 US M2 YoY median ~5-6%;
    # <2% (incl. 2023's negative prints) = tightening/risk-off; >10% = clear easing.
    "m2_yoy":          {"neutral": 2.0,   "extreme": 10.0},
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

# Direction of each indicator, derived from its threshold ordering (single source
# of truth, reused by the calibrator + the percentile mapping):
#   lower value = more bullish  <=> neutral > extreme.
DIRECTION: dict[str, str] = {
    name: ("lower_bullish" if th["neutral"] > th["extreme"] else "higher_bullish")
    for name, th in THRESHOLDS.items()
}

# Which indicators belong to which category.
CATEGORY_INDICATORS: dict[str, list[str]] = {
    "onchain":   ["mvrv_z", "realized_ratio", "nupl", "sopr", "puell"],
    "price":     ["price_to_wma200", "mayer"],
    "macro":     ["m2_yoy", "hy_spread", "real_yield", "etf_flow"],
    "sentiment": ["fng"],
    "derivs":    ["funding", "oi_flush", "liq_magnitude"],
}

# Highly-correlated indicators that measure the same thing — each group collapses
# to ONE term in its category mean (average-then-count-once) so the duplicated
# signal isn't double-counted. Members stay individually visible in the breakdown.
REDUNDANCY_GROUPS: dict[str, list[list[str]]] = {
    "onchain": [["mvrv_z", "nupl", "realized_ratio"]],  # realized-value valuation (~0.9 corr)
    "price":   [["price_to_wma200", "mayer"]],           # both price-vs-long-MA (~0.99 corr)
}

# Reverse lookup: indicator -> its group's representative key (for breakdown tagging).
INDICATOR_GROUP: dict[str, str] = {
    member: group[0]
    for groups in REDUNDANCY_GROUPS.values()
    for group in groups
    for member in group
}

# Human-readable labels for alert text.
INDICATOR_LABELS: dict[str, str] = {
    "mvrv_z": "MVRV Z-Score",
    "realized_ratio": "Realized-price ratio",
    "nupl": "NUPL",
    "sopr": "SOPR",
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
# the multiplier bottoms out. Widened to 300 — cycle timing is a soft prior, not a
# clock (the 4-year cadence is looser in the ETF era).
CYCLE_WINDOW_HALFWIDTH_DAYS = 300


# --- Calibration (offline-computed percentile breakpoints) -------------------

_CALIB: dict | None = None  # module cache so the live path stays pure (one read)


def _load_calibration() -> dict:
    """Load app/calibration.json once (cached). Missing/invalid -> {} (fallback)."""
    global _CALIB
    if _CALIB is None:
        try:
            _CALIB = json.loads(Path(__file__).with_name("calibration.json").read_text())
        except (OSError, json.JSONDecodeError):
            _CALIB = {}
    return _CALIB


def set_calibration(calib: dict | None) -> None:
    """Inject calibration (tests) or reset to re-read the file on next access (None)."""
    global _CALIB
    _CALIB = calib


def percentile_score(value: float, breakpoints: list[float], probs: list[float],
                     direction: str) -> float | None:
    """Map a raw value to [0,1] by its empirical percentile vs ``breakpoints``.

    ``breakpoints[i]`` is the historical value at quantile ``probs[i]`` (both
    ascending). Interpolates the percentile of ``value`` and flips it for
    'lower_bullish' metrics (low value -> high score). Pure arithmetic.
    """
    if not breakpoints or not probs or len(breakpoints) != len(probs):
        return None
    if value <= breakpoints[0]:
        p = probs[0]
    elif value >= breakpoints[-1]:
        p = probs[-1]
    else:
        i = bisect.bisect_right(breakpoints, value)  # breakpoints[i-1] <= value < breakpoints[i]
        lo_v, hi_v = breakpoints[i - 1], breakpoints[i]
        lo_p, hi_p = probs[i - 1], probs[i]
        frac = (value - lo_v) / (hi_v - lo_v) if hi_v > lo_v else 0.0
        p = lo_p + frac * (hi_p - lo_p)
    score = (1.0 - p) if direction == "lower_bullish" else p
    return max(0.0, min(1.0, score))


def rank_score(history: list[float], value: float, direction: str) -> float | None:
    """Percentile RANK of ``value`` within ``history`` (fraction <= value), flipped
    for direction. The calibration backtest passes the EXPANDING [start..t] slice so
    a historical day is scored only against its own past — no look-ahead bias."""
    if not history:
        return None
    p = sum(1 for h in history if h <= value) / len(history)
    score = (1.0 - p) if direction == "lower_bullish" else p
    return max(0.0, min(1.0, score))


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


_TIER_ORDER = ["NEUTRAL", "WATCH", "ACCUMULATE", "DEEP_VALUE"]
_TIER_FLOOR_KEY = {"WATCH": 0, "ACCUMULATE": 1, "DEEP_VALUE": 2}


def tier_hysteresis(score: float, price: float, wma200: float | None,
                    prev_tier: str, t_watch: float, t_acc: float, t_deep: float,
                    margin: float = 2.0, deep_exit_band: float = 0.02) -> str:
    """Tier with a dead-band so a composite hovering on a threshold doesn't whipsaw.

    A move to a HIGHER tier requires the composite to clear that tier's floor by
    ``margin``; a move LOWER requires it to fall ``margin`` below the previous
    tier's floor. Inside the band the previous tier holds. margin=0 reproduces the
    plain ``tier``.

    Special case — DEEP_VALUE is gated on ``price <= wma200``, not just score. If
    the only reason ``raw`` left DEEP_VALUE is that price rose back above the 200WMA
    (score still clears ``t_deep``), the COMPOSITE dead-band must not trap the tier
    in DEEP_VALUE while the defining price condition is false. Exit on a small PRICE
    band (``deep_exit_band``, default 2% above the 200WMA) instead, so the system
    can't keep reporting "heaviest tranches" 20% above the 200WMA.
    """
    raw = tier(score, price, wma200, t_watch, t_acc, t_deep)
    if margin <= 0 or raw == prev_tier or prev_tier not in _TIER_ORDER:
        return raw
    floors = [t_watch, t_acc, t_deep]
    raw_i, prev_i = _TIER_ORDER.index(raw), _TIER_ORDER.index(prev_tier)
    if raw_i > prev_i:  # upgrade: require clearing the NEW floor by margin
        floor = floors[_TIER_FLOOR_KEY[raw]]
        return raw if score >= floor + margin else prev_tier
    # downgrade.
    if (prev_tier == "DEEP_VALUE" and score >= t_deep
            and wma200 is not None and price > wma200):
        # Gate-driven exit (price above the 200WMA): use a price band, not the
        # composite margin, so a clear breach can't be held by a high composite.
        return raw if price > wma200 * (1 + deep_exit_band) else prev_tier
    # otherwise: require falling margin below the PREVIOUS tier's floor
    floor = floors[_TIER_FLOOR_KEY[prev_tier]]
    return raw if score <= floor - margin else prev_tier


def category_agreement(category_scores: dict[str, float | None]) -> dict | None:
    """Confidence proxy from how much the active categories AGREE. High spread
    (e.g. on-chain cheap but macro risk-off) = lower confidence. None if <2 active."""
    vals = [v for v in category_scores.values() if v is not None]
    if len(vals) < 2:
        return None
    spread = max(vals) - min(vals)
    label = "high" if spread <= 0.25 else ("medium" if spread <= 0.5 else "low")
    return {"active": len(vals), "spread": round(spread, 3),
            "agreement": round(1.0 - spread, 3), "confidence": label}


# --- Built on top of the reference helpers -----------------------------------

def score_indicators(readings: dict[str, float | None]) -> dict[str, float | None]:
    """Map raw indicator readings to [0,1] sub-scores.

    Uses the calibrated percentile mapping when ``calibration.json`` carries
    breakpoints for the indicator, else the fixed ``THRESHOLDS`` linear_score.
    A missing key or None value yields a None sub-score (unavailable downstream).
    Unknown keys in ``readings`` are ignored.
    """
    calib = _load_calibration()
    cal_inds = calib.get("indicators", {})
    default_probs = calib.get("probs")

    out: dict[str, float | None] = {}
    for name, th in THRESHOLDS.items():
        value = readings.get(name)
        if value is None:
            out[name] = None
            continue
        c = cal_inds.get(name)
        probs = (c.get("probs") if c else None) or default_probs
        if c and probs:
            out[name] = percentile_score(
                float(value), c["breakpoints"], probs,
                c.get("direction", DIRECTION.get(name, "higher_bullish")))
        else:
            out[name] = linear_score(float(value), th["neutral"], th["extreme"])
    return out


def _category_terms(cat: str, inds: list[str],
                    subscores: dict[str, float | None]) -> list[float]:
    """Per-category list of available terms, with each redundancy group collapsed
    to ONE term (mean of its available members) so correlated indicators don't
    double-count. Ungrouped indicators are each their own term."""
    groups = REDUNDANCY_GROUPS.get(cat, [])
    grouped = {m for g in groups for m in g}
    terms: list[float] = []
    for g in groups:
        vals = [subscores.get(m) for m in g if subscores.get(m) is not None]
        if vals:
            terms.append(sum(vals) / len(vals))
    for ind in inds:
        if ind not in grouped and subscores.get(ind) is not None:
            terms.append(subscores[ind])
    return terms


def category_scores(subscores: dict[str, float | None]) -> dict[str, float | None]:
    """Aggregate per-indicator sub-scores into per-category scores, collapsing
    redundancy groups to a single term (see REDUNDANCY_GROUPS)."""
    out: dict[str, float | None] = {}
    for cat, inds in CATEGORY_INDICATORS.items():
        terms = _category_terms(cat, inds, subscores)
        out[cat] = (sum(terms) / len(terms)) if terms else None
    return out


def cycle_multiplier(today: date, ath_date: date, peak_to_trough_days: int,
                     halfwidth_days: int = CYCLE_WINDOW_HALFWIDTH_DAYS,
                     swing: float = 0.1) -> float:
    """Context-only multiplier in [1-swing, 1+swing] from proximity to the typical
    bottom window.

    Closer to (ath_date + peak_to_trough_days) -> toward 1+swing; far -> toward
    1-swing. ``swing=0`` disables timing entirely (always 1.0) — the kill-switch.
    This only *weights* confluence; it never creates a signal on its own.
    """
    days_since_peak = (today - ath_date).days
    distance = abs(days_since_peak - peak_to_trough_days)
    frac = min(1.0, distance / max(1, halfwidth_days))
    return (1.0 + swing) - 2.0 * swing * frac


def indicators_in_zone(subscores: dict[str, float | None],
                       threshold: float = IN_ZONE_THRESHOLD) -> list[str]:
    """Labels of indicators whose sub-score is at/above the bottom-zone threshold."""
    return [
        INDICATOR_LABELS.get(name, name)
        for name, s in subscores.items()
        if s is not None and s >= threshold
    ]

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
import math
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
    # Holder conviction (lower = more bullish). Reserve Risk: low = strong-hand
    # conviction at a low price = attractive risk/reward. Band from the 2012+
    # static-file distribution: neutral≈median 0.0025, extreme≈p10 0.0012.
    "reserve_risk":    {"neutral": 0.0025, "extreme": 0.0012},
    # Miner cycle (higher = more bullish). hash_ribbon is a cooked 0..1 recovery
    # score from app/sources/miner.py (identity band — see that module).
    "hash_ribbon":     {"neutral": 0.0,   "extreme": 1.0},
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
    # Fed net liquidity YoY %; expanding -> bullish (higher). Captures the TGA/RRP
    # plumbing M2 misses; grouped with m2_yoy so the two don't double-count.
    "net_liq_yoy":     {"neutral": 0.0,   "extreme": 10.0},
    # NFCI financial conditions; positive = tighter/stress -> capitulation (higher).
    # Asymmetric tails (GFC ~+5, COVID ~+1.5); +0.6 is a provisional extreme.
    "nfci":            {"neutral": 0.0,   "extreme": 0.6},
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
    "onchain":   ["mvrv_z", "realized_ratio", "nupl", "sopr", "puell",
                  "reserve_risk", "hash_ribbon"],
    "price":     ["price_to_wma200", "mayer"],
    "macro":     ["m2_yoy", "hy_spread", "real_yield", "etf_flow",
                  "net_liq_yoy", "nfci"],
    "sentiment": ["fng"],
    "derivs":    ["funding", "oi_flush", "liq_magnitude"],
}

# Highly-correlated indicators that measure the same thing — each group collapses
# to ONE term in its category mean (average-then-count-once) so the duplicated
# signal isn't double-counted. Members stay individually visible in the breakdown.
REDUNDANCY_GROUPS: dict[str, list[list[str]]] = {
    "onchain": [["mvrv_z", "nupl", "realized_ratio"]],  # realized-value valuation (~0.9 corr)
    "price":   [["price_to_wma200", "mayer"]],           # both price-vs-long-MA (~0.99 corr)
    "macro":   [["m2_yoy", "net_liq_yoy"],               # broad liquidity (M2 vs Fed net liquidity)
                ["hy_spread", "nfci"]],                   # financial-stress gauges (credit/conditions)
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
    "reserve_risk": "Reserve Risk",
    "hash_ribbon": "Hash Ribbon (miners)",
    "price_to_wma200": "Price / 200-week MA",
    "mayer": "Mayer Multiple",
    "m2_yoy": "M2 YoY",
    "hy_spread": "HY credit spread",
    "real_yield": "10Y real yield",
    "etf_flow": "ETF net flows",
    "net_liq_yoy": "Fed net liquidity (YoY)",
    "nfci": "Financial conditions (NFCI)",
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

def _finite(value) -> float | None:
    """A reading is usable only if it's a finite number. NaN must map to None,
    not flow into the scorers: ``min(1.0, nan)`` silently returns the bound, so
    a NaN reading would otherwise clamp into a full 1.0 sub-score (a false
    maximal signal). pandas-derived readings can carry NaN through the stored
    JSON (Python's json emits/accepts bare ``NaN``)."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


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
        value = _finite(readings.get(name))
        if value is None:
            out[name] = None
            continue
        c = cal_inds.get(name)
        probs = (c.get("probs") if c else None) or default_probs
        if c and probs:
            out[name] = percentile_score(
                value, c["breakpoints"], probs,
                c.get("direction", DIRECTION.get(name, "higher_bullish")))
        else:
            out[name] = linear_score(value, th["neutral"], th["extreme"])
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


# --- Zone boundaries (inverse mapping, for "what flips this badge" display) ---

def _linear_boundary(th: dict[str, float], threshold: float) -> float:
    """Raw value where linear_score(value, neutral, extreme) == threshold."""
    lo, hi = sorted((th["neutral"], th["extreme"]))
    if lo == hi:
        return lo
    t = threshold if th["extreme"] > th["neutral"] else 1.0 - threshold
    return lo + t * (hi - lo)


def zone_boundary_raw(name: str, threshold: float = IN_ZONE_THRESHOLD) -> float | None:
    """Raw value at which ``name`` crosses into its BOTTOM zone (sub-score ==
    threshold), inverting the SAME mapping ``score_indicators`` would use — the
    calibrated percentile when calibration.json carries breakpoints for it, else
    the fixed linear threshold band. The dashboard shows this as "lights at X";
    a boundary derived from the wrong mapping would promise a delta the real
    scorer never honors.
    """
    calib = _load_calibration()
    c = calib.get("indicators", {}).get(name)
    probs = (c.get("probs") if c else None) or calib.get("probs")
    if c and probs and len(probs) == len(c.get("breakpoints", [])):
        d = c.get("direction", DIRECTION.get(name, "higher_bullish"))
        # Invert percentile_score: find the value whose quantile p yields the
        # threshold score (score = 1-p for lower_bullish, p otherwise).
        p = (1.0 - threshold) if d == "lower_bullish" else threshold
        bps, ps = c["breakpoints"], probs
        if p <= ps[0]:
            return float(bps[0])
        if p >= ps[-1]:
            return float(bps[-1])
        i = bisect.bisect_right(ps, p)
        lo_p, hi_p = ps[i - 1], ps[i]
        lo_v, hi_v = bps[i - 1], bps[i]
        frac = (p - lo_p) / (hi_p - lo_p) if hi_p > lo_p else 0.0
        return float(lo_v + frac * (hi_v - lo_v))
    th = THRESHOLDS.get(name)
    return _linear_boundary(th, threshold) if th else None


def top_zone_boundary_raw(name: str, threshold: float = IN_ZONE_THRESHOLD) -> float | None:
    """Raw value at which ``name`` crosses into its TOP (froth) zone. The froth
    side is never calibrated, so this is always the linear inversion."""
    th = TOP_THRESHOLDS.get(name)
    return _linear_boundary(th, threshold) if th else None


# --- Sell-side "froth" (overheat) score ---------------------------------------
# Mirror of the buy-side mapping: the same VALUE indicators read at their
# cycle-top extremes, plus crowded-positive funding. Thresholds were revised
# against history via ``scripts/backtest_tops.py`` (Coinbase daily 2015+,
# alternative.me F&G 2018+, bitcoin-data on-chain 2022+): cycle extremes
# COMPRESS every cycle (Mayer top 3.66 in 2017 -> 2.46 in 2021 -> 1.53 in 2025),
# so "extreme" anchors near the MOST RECENT cycle-top window's p95/max — values
# the older, hotter tops still saturate. With these levels the Oct-2025 top
# window reads frothy/overheated, the 2017/2021 tops max out, and the
# 2018/2020/2022 bottoms (and post-top today) read 0. Still a small-sample
# heuristic (1-3 cycles per indicator), not a proven edge — keep it labeled.
TOP_THRESHOLDS: dict[str, dict[str, float]] = {
    # On-chain valuation (higher = more frothy); free history starts 2022-06,
    # so these are anchored on the 2024-2025 top window only.
    "mvrv_z":          {"neutral": 2.0,   "extreme": 3.2},
    "realized_ratio":  {"neutral": 1.7,   "extreme": 2.6},
    "nupl":            {"neutral": 0.45,  "extreme": 0.62},
    "sopr":            {"neutral": 1.005, "extreme": 1.04},
    "puell":           {"neutral": 1.0,   "extreme": 1.6},
    # Price structure (multi-cycle history)
    "price_to_wma200": {"neutral": 1.6,   "extreme": 2.4},
    "mayer":           {"neutral": 1.2,   "extreme": 1.55},
    # Sentiment (2018+)
    "fng":             {"neutral": 60.0,  "extreme": 85.0},
    # Derivatives: sustained positive 7d funding = crowded longs. No free deep
    # history, so this one stays an economic-logic level (0.01%/8h baseline ->
    # 0.04%/8h heavily crowded), excluded from the backtest.
    "funding":         {"neutral": 0.0001, "extreme": 0.0004},
}

# Correlated families collapsed to one term in the froth mean — same families as
# REDUNDANCY_GROUPS, listed flat because froth has no category layer.
_TOP_GROUPS: list[list[str]] = [
    ["mvrv_z", "nupl", "realized_ratio"],
    ["price_to_wma200", "mayer"],
]


def froth_subscores(readings: dict[str, float | None]) -> dict[str, float | None]:
    """[0,1] overheat sub-scores via TOP_THRESHOLDS (no calibration on this side)."""
    out: dict[str, float | None] = {}
    for name, th in TOP_THRESHOLDS.items():
        value = _finite(readings.get(name))
        out[name] = None if value is None else linear_score(value, th["neutral"], th["extreme"])
    return out


# Overheat bands (floors). The label is computed server-side with a dead-band so
# a froth score hovering on a cutoff can't whipsaw the label (and the alert)
# every 6h run — same idea as tier_hysteresis.
FROTH_BANDS: tuple[tuple[str, float], ...] = (
    ("COOL", 0.0), ("WARMING", 25.0), ("FROTHY", 50.0), ("OVERHEATED", 75.0),
)
_BAND_ORDER = [name for name, _ in FROTH_BANDS]
_BAND_FLOOR = dict(FROTH_BANDS)


def froth_band(score: float | None, prev_band: str | None = None,
               margin: float = 3.0) -> str | None:
    """Overheat band with a ±``margin`` dead-band: moving UP requires clearing
    the new band's floor by the margin; moving DOWN requires falling the margin
    below the previous band's floor; inside the band the previous label holds.
    ``prev_band=None`` (or margin<=0) returns the raw band."""
    if score is None:
        return None
    raw = "COOL"
    for name, floor in FROTH_BANDS:
        if score >= floor:
            raw = name
    if prev_band not in _BAND_ORDER or margin <= 0 or raw == prev_band:
        return raw
    raw_i, prev_i = _BAND_ORDER.index(raw), _BAND_ORDER.index(prev_band)
    if raw_i > prev_i:
        # Upgrade: step to the HIGHEST band whose floor the score clears by the
        # margin. A multi-band jump that lands inside the top band's dead-band
        # must stop at the intermediate band it clearly cleared — falling all
        # the way back to prev_band would let e.g. a 76 score keep a COOL label
        # (and suppress the overheat alert) indefinitely.
        for cand in reversed(_BAND_ORDER[prev_i + 1:raw_i + 1]):
            if score >= _BAND_FLOOR[cand] + margin:
                return cand
        return prev_band
    # downgrade: fall margin below the PREVIOUS band's floor
    return raw if score <= _BAND_FLOOR[prev_band] - margin else prev_band


def froth_score(readings: dict[str, float | None]) -> dict:
    """Sell-side overheat read: 0 = no froth, 100 = historic-top conditions.

    Flat mean over available sub-scores with each correlated family collapsed to
    one term (same de-duplication as the buy-side category means). Returns
    {"score": 0-100|None, "subscores", "in_zone" labels, "active" term count}.
    """
    subs = froth_subscores(readings)
    grouped = {m for g in _TOP_GROUPS for m in g}
    terms: list[float] = []
    for g in _TOP_GROUPS:
        vals = [subs[m] for m in g if subs.get(m) is not None]
        if vals:
            terms.append(sum(vals) / len(vals))
    terms += [s for name, s in subs.items() if name not in grouped and s is not None]
    score = max(0.0, min(100.0, (sum(terms) / len(terms)) * 100)) if terms else None
    return {
        "score": score,
        "subscores": subs,
        "in_zone": indicators_in_zone(subs),
        "active": len(terms),
    }

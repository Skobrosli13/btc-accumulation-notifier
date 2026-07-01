"""Confidence model (pure, I/O-free).

Confidence must be *measured then earned*, not asserted. Two layers:

1. **Base rate** — the archetype's historical win-rate + expectancy (avg R) from
   ``stock_st_winrates.json`` (emitted offline by ``scripts/stock_calibrate.py``).
   Absent → a conservative built-in PRIOR, explicitly flagged not-live-confirmed.
2. **Modifiers** — signal-strength, cross-sectional relative strength, regime
   alignment and context confluence nudge the base rate within a bounded band.

The output probability is capped at 0.80 (an honesty ceiling — we never claim near
certainty) and, until the live position tracker has enough closed trades, is
labelled a "backtested prior". Win-rate is reported ALONGSIDE expectancy because a
sub-50% win-rate with big winners is still profitable — expectancy (avg R) is the
real target, not raw accuracy.
"""
from __future__ import annotations

# Conservative built-in priors used until the live/backtest win-rates exist.
# Deliberately humble — PEAD's documented edge is mild and decaying; the others
# are near coin-flips whose expectancy comes from the R-frame, not the hit-rate.
PRIOR = {
    "pead_drift":     {"win_rate": 0.55, "expectancy_r": 0.20},
    "momentum":       {"win_rate": 0.50, "expectancy_r": 0.15},
    "mean_reversion": {"win_rate": 0.52, "expectancy_r": 0.10},
}
_LIVE_CONFIRM_N = 30   # closed trades before a base rate is trusted over the prior
_CAP = 0.80            # honesty ceiling on displayed confidence
_FLOOR = 0.30


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _cell(archetype: str, winrates: dict | None) -> dict | None:
    """The archetype's win-rates cell, or None when absent OR invalid.

    A ``pead_drift`` cell is only valid when it carries
    ``alignment == 'announcement_date'`` — older seeds measured drift from the
    fiscal period-end, not the announcement, so their rates are meaningless for
    the live announcement-anchored setup and fall back to the built-in PRIOR."""
    rec = ((winrates or {}).get("archetypes", {}) or {}).get(archetype) if winrates else None
    if not rec or not rec.get("n"):
        return None
    if archetype == "pead_drift" and rec.get("alignment") != "announcement_date":
        return None
    return rec


def archetype_maturity(archetype: str, winrates: dict | None) -> str:
    """Honesty rung for an archetype: 'edge' or 'forward', derived from the loaded
    win-rates cell — never hardcoded. 'edge' requires a VALID cell (see ``_cell``)
    that is explicitly significant (``not_significant == False``); anything else —
    no cell, invalid alignment, unmarked/insignificant — stays a forward-test."""
    rec = _cell(archetype, winrates)
    if rec is not None and rec.get("not_significant") is False:
        return "edge"
    return "forward"


def base_rate(archetype: str, winrates: dict | None) -> dict:
    """The calibrated base rate for an archetype, shrunk toward the prior by sample
    size. Returns {win_rate, expectancy_r, n, live_confirmed}.

    ``live_confirmed`` is True ONLY when the win-rates come from LIVE, out-of-sample
    trades (``source == 'live'``) with enough n — a backtest seed is still a prior
    (in-sample, survivorship-biased), so it informs the rate but keeps the honest
    'backtested prior' label."""
    prior = PRIOR.get(archetype, {"win_rate": 0.50, "expectancy_r": 0.0})
    rec = _cell(archetype, winrates)
    if rec is None:
        return {**prior, "n": 0, "live_confirmed": False}
    n = int(rec["n"])
    is_live = (winrates or {}).get("source") == "live"
    # Shrink the empirical rates toward the prior with a pseudo-count so a handful
    # of trades can't swing the number around (Bayesian-ish smoothing). Expectancy
    # gets the SAME shrinkage — it is the noisier statistic (unbounded, fat-tailed
    # in R) and the one that actually drives ranking, so a small-n archetype must
    # not buy priority with an unstable average.
    k = _LIVE_CONFIRM_N
    wr_emp = rec.get("win_rate")
    exp_emp = rec.get("expectancy_r")
    wr = ((wr_emp if wr_emp is not None else prior["win_rate"]) * n
          + prior["win_rate"] * k) / (n + k)
    exp = ((exp_emp if exp_emp is not None else prior["expectancy_r"]) * n
           + prior["expectancy_r"] * k) / (n + k)
    return {"win_rate": wr, "expectancy_r": exp, "n": n,
            "live_confirmed": is_live and n >= _LIVE_CONFIRM_N}


def confidence(candidate, winrates: dict | None) -> dict:
    """Calibrated confidence for a ranked candidate. ``candidate`` has .archetype,
    .primary, .rel, .regime, .context (0..1 each)."""
    br = base_rate(candidate.archetype, winrates)
    base = br["win_rate"]
    mods = {
        # strong archetype signal: up to +/-0.08 around base
        "signal": (candidate.primary - 0.5) * 0.16,
        # cross-sectional relative strength: up to +/-0.06
        "rel": (candidate.rel - 0.5) * 0.12,
        # regime alignment (regime is 1.0 aligned / 0.25 counter): +/-~0.05
        "regime": (candidate.regime - 0.6) * 0.12,
        # context confluence (insider/short-vol/revision agreeing): up to +0.05
        "context": candidate.context * 0.05,
    }
    prob = _clamp(base + sum(mods.values()), _FLOOR, _CAP)
    return {
        "prob": round(prob, 3),
        "base_rate": round(base, 3),
        "expectancy_r": round(br["expectancy_r"], 3),
        "n": br["n"],
        "live_confirmed": br["live_confirmed"],
        "modifiers": {k: round(v, 3) for k, v in mods.items()},
        "label": ("live-confirmed" if br["live_confirmed"] else "backtested prior"),
    }

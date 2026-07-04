"""Placebo suite — the harness's self-test (§5.4).

If the machinery finds "significance" in shuffled events, the machinery is
broken (look-ahead, bad matching, autocorrelation leakage) — stop and fix before
trusting ANY real result.

CRITERION (dof-aware; a deliberate correction to the plan's literal
"95th pct |t| < 2", flagged to the owner as a Class-C note): the clustered t of
a placebo has ~(n_months − 1) degrees of freedom, and the NULL 95th percentile
of |t| at 5 dof is ≈2.57 — even at infinite dof it is 1.96, so a fixed 2.0 bar
flags CORRECT machinery as dirty roughly half the time. The operating rule is
therefore an exceedance count: each shuffle's |t| is compared against the
two-sided 95% critical value for ITS OWN dof; under a correct null ~5% exceed,
so the suite is dirty when more than EXCEEDANCE_MAX_FRAC (15%) do — a >3σ
binomial signal of genuine bias, robust at 50 shuffles. The literal
``p95_abs_t`` (and ``p95_lt_2``) are still reported for transparency.

Shuffle modes preserve the study's real structure except the signal:
  * **per-ticker (CAR)** — permute the event DATES across events; each ticker
    keeps its event count, the calendar keeps its date multiset, only the
    ticker↔date alignment (the claimed information) is destroyed.
  * **per-regime (ts)** — redraw event bar-indices uniformly WITHIN each 200-DMA
    regime, preserving the bull/bear event counts, so a placebo can't "win" by
    simply landing in the friendlier regime.

Pure and seeded.
"""
from __future__ import annotations

import random

N_SHUFFLES = 50
EXCEEDANCE_MAX_FRAC = 0.15    # >15% of shuffles beyond their dof-critical |t| = dirty

# Two-sided 95% critical values of Student's t by dof (standard table);
# interpolated between entries, 1.96 beyond 120 dof.
_T_CRIT_95 = [
    (1, 12.706), (2, 4.303), (3, 3.182), (4, 2.776), (5, 2.571), (6, 2.447),
    (7, 2.365), (8, 2.306), (9, 2.262), (10, 2.228), (12, 2.179), (15, 2.131),
    (20, 2.086), (25, 2.060), (30, 2.042), (40, 2.021), (60, 2.000), (120, 1.980),
]


def t_crit_95(dof: int) -> float:
    """Two-sided 95% critical |t| for ``dof`` (linear interp; 1.96 past 120)."""
    if dof <= 0:
        return float("inf")
    prev_d, prev_c = _T_CRIT_95[0]
    if dof <= prev_d:
        return prev_c
    for d, c in _T_CRIT_95[1:]:
        if dof <= d:
            frac = (dof - prev_d) / (d - prev_d)
            return prev_c + frac * (c - prev_c)
        prev_d, prev_c = d, c
    return 1.96


def shuffle_dates_per_ticker(events: list[dict], rnd: random.Random) -> list[dict]:
    """Permute event_ts across events (tickers/directions/attributes stay)."""
    ts = [e["event_ts"] for e in events]
    rnd.shuffle(ts)
    return [{**e, "event_ts": t} for e, t in zip(events, ts)]


def redraw_within_regimes(event_bar_regimes: list[str],
                          bars_by_regime: dict[str, list[int]],
                          ts_ms: list[int], rnd: random.Random) -> list[int]:
    """Random event timestamps preserving per-regime counts (ts studies).

    ``event_bar_regimes``: the observed events' regime labels;
    ``bars_by_regime``: candidate bar indices per regime (e.g. every bar with a
    known regime). Draws without replacement within each regime."""
    out: list[int] = []
    for regime in set(event_bar_regimes):
        n = sum(1 for r in event_bar_regimes if r == regime)
        pool = bars_by_regime.get(regime, [])
        if not pool:
            continue
        take = rnd.sample(pool, min(n, len(pool)))
        out.extend(ts_ms[i] for i in take)
    return sorted(out)


def suite(eval_t_fn, *, n: int = N_SHUFFLES, seed: int = 42,
          max_exceed_frac: float = EXCEEDANCE_MAX_FRAC) -> dict:
    """Run ``eval_t_fn(rnd)`` over n seeded shuffles.

    ``eval_t_fn`` returns ``(t, n_months)`` (preferred — enables the dof-aware
    criterion), a bare ``t`` (treated as infinite dof, the STRICTEST critical
    value), or None (that shuffle failed to evaluate).

    Returns {n, n_valid, t_values, exceedances, exceed_frac, p95_abs_t,
    p95_lt_2, clean}. ``clean`` is False when the exceedance fraction is above
    ``max_exceed_frac`` OR fewer than half the shuffles produced a defined t
    (a placebo that mostly fails to evaluate proves nothing)."""
    t_values: list[float] = []
    exceed = 0
    for k in range(n):
        res = eval_t_fn(random.Random(seed + k))
        if res is None:
            continue
        if isinstance(res, tuple):
            t, n_months = res
        else:
            t, n_months = res, 10 ** 6
        if t is None:
            continue
        t_values.append(float(t))
        if abs(t) > t_crit_95(max(1, int(n_months) - 1)):
            exceed += 1
    out = {"n": n, "n_valid": len(t_values), "t_values": t_values,
           "exceedances": exceed, "exceed_frac": None,
           "p95_abs_t": None, "p95_lt_2": None, "clean": False}
    if len(t_values) < max(2, n // 2):
        return out
    out["exceed_frac"] = exceed / len(t_values)
    abs_sorted = sorted(abs(t) for t in t_values)
    idx = min(len(abs_sorted) - 1, int(round(0.95 * (len(abs_sorted) - 1))))
    out["p95_abs_t"] = abs_sorted[idx]
    out["p95_lt_2"] = out["p95_abs_t"] < 2.0          # the plan's literal metric
    out["clean"] = out["exceed_frac"] <= max_exceed_frac
    return out

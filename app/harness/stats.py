"""Shared statistics for the harness — the honesty machinery (§5.4).

Ports the codebase's audited patterns (kept per the M0 keep-list) into one pure
module:

- **Month-clustered t** (from ``scripts/stock_backtest``): trades/events within a
  calendar month are serially correlated, so the month — not the event — is the
  independent unit. Every gate's ``t_clustered`` comes from here.
- **Spaced de-correlation** (from ``app/perf._spaced``): greedy subset of event
  timestamps ≥ one horizon apart, so forward windows never overlap. This is the
  episode-collapse rule the BTC ts-studies reuse.
- **Winsorize** at percentile bounds (CAR outliers, §5.2).
- **Bootstrap CI** on binary outcomes (from ``app/perf._bootstrap_ci``) and the
  Wilson interval for win-rates.

No I/O, no config; deterministic (seeded) where randomness is involved.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timezone

_DAY_MS = 86_400_000


def month_key(ts_ms: int) -> str:
    """Epoch-ms -> 'YYYY-MM' (UTC) — the clustering unit."""
    d = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return f"{d.year:04d}-{d.month:02d}"


def monthly_means(values: list[float], ts_ms: list[int]) -> list[float]:
    """Per-month mean of ``values`` grouped by their timestamps' calendar month.
    The returned list (one entry per distinct month, insertion-ordered) is the
    clustered sample every significance test runs on."""
    months: dict[str, list[float]] = {}
    for v, ts in zip(values, ts_ms):
        months.setdefault(month_key(ts), []).append(float(v))
    return [sum(vs) / len(vs) for vs in months.values()]


def clustered_t(values: list[float], ts_ms: list[int]) -> dict:
    """One-sample month-clustered t of mean(values) vs 0.

    Returns {t, n_months, mean, monthly_mean} — ``t`` is None below 2 months or
    under zero dispersion (no fabricated infinities; a degenerate sample simply
    has no defined t). ``mean`` is the event-weighted mean (display);
    ``monthly_mean`` is the clustered mean the t is computed on.
    """
    out = {"t": None, "n_months": 0, "mean": None, "monthly_mean": None}
    if not values:
        return out
    out["mean"] = sum(values) / len(values)
    mm = monthly_means(values, ts_ms)
    out["n_months"] = len(mm)
    if len(mm) < 2:
        return out
    m = sum(mm) / len(mm)
    out["monthly_mean"] = m
    var = sum((x - m) ** 2 for x in mm) / (len(mm) - 1)
    if var <= 0:
        return out
    out["t"] = m / math.sqrt(var / len(mm))
    return out


def clustered_delta_t(a_values: list[float], a_ts: list[int],
                      b_values: list[float], b_ts: list[int]) -> dict:
    """Two-sample month-clustered t of mean(a) − mean(b) (event arm vs control/
    baseline arm), the ``scripts/stock_backtest`` significance rule: each month
    counts once per arm; SE = sqrt(var_a/n_a + var_b/n_b) over monthly means."""
    out = {"t": None, "delta": None, "n_months_a": 0, "n_months_b": 0}
    ma, mb = monthly_means(a_values, a_ts), monthly_means(b_values, b_ts)
    out["n_months_a"], out["n_months_b"] = len(ma), len(mb)
    if len(ma) < 2 or len(mb) < 2:
        return out
    mean_a, mean_b = sum(ma) / len(ma), sum(mb) / len(mb)
    out["delta"] = mean_a - mean_b
    var_a = sum((x - mean_a) ** 2 for x in ma) / (len(ma) - 1)
    var_b = sum((x - mean_b) ** 2 for x in mb) / (len(mb) - 1)
    se = math.sqrt(var_a / len(ma) + var_b / len(mb))
    if se <= 0:
        return out
    out["t"] = out["delta"] / se
    return out


def spaced_subset(ts_ms: list[int], min_gap_ms: int) -> list[int]:
    """Greedy subset of INDICES whose timestamps are >= ``min_gap_ms`` apart
    (each vs the last KEPT one). ``ts_ms`` must be ascending. This is the
    de-correlation / episode-collapse rule: overlapping forward windows resampled
    i.i.d. overstate evidence; the spaced subset is the honest sample
    (port of app/perf._spaced, generalized to a millisecond gap)."""
    kept: list[int] = []
    last: int | None = None
    for i, ts in enumerate(ts_ms):
        if last is None or ts - last >= min_gap_ms:
            kept.append(i)
            last = ts
    return kept


def winsorize(values: list[float], lo: float = 0.01, hi: float = 0.99) -> list[float]:
    """Clip to the [lo, hi] empirical percentiles (linear interpolation, the
    numpy default). Fewer than 3 values pass through unchanged."""
    n = len(values)
    if n < 3:
        return list(values)
    s = sorted(values)

    def pct(q: float) -> float:
        pos = q * (n - 1)
        i = int(pos)
        frac = pos - i
        return s[i] if i + 1 >= n else s[i] * (1 - frac) + s[i + 1] * frac

    lo_v, hi_v = pct(lo), pct(hi)
    return [min(max(v, lo_v), hi_v) for v in values]


def bootstrap_ci(outcomes: list[int], iters: int = 2000, seed: int = 42,
                 alpha: float = 0.10) -> list[float] | None:
    """(1-alpha) bootstrap CI on 0/1 outcomes (port of app/perf._bootstrap_ci).
    None below 3 samples — a CI on 1-2 observations would be theater."""
    if len(outcomes) < 3:
        return None
    rnd = random.Random(seed)
    n = len(outcomes)
    rates = sorted(sum(outcomes[rnd.randrange(n)] for _ in range(n)) / n
                   for _ in range(iters))
    lo_i = int((alpha / 2) * iters)
    hi_i = int((1 - alpha / 2) * iters)
    return [round(rates[lo_i], 3), round(rates[min(hi_i, iters - 1)], 3)]


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson score interval for a binomial proportion (k successes of n).
    The codebase's standard win-rate interval. None when n == 0."""
    if n <= 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))

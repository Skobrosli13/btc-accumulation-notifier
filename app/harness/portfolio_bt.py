"""Portfolio-policy backtester (§5.5 POLICY gate math) — pure.

Two simulators, both strictly causal (a decision made at close t earns return
t→t+1; nothing sees the future):

- :func:`equity_curve` — an exposure series (0..1, decided AT each close) applied
  to the next day's return, with a per-switch cost. For btc_trend_policy vs
  buy-and-hold.
- :func:`dca_simulate` — periodic $ contributions with a causal spend-scale:
  each period's budget B is scaled by s_t ∈ [0, s_max]; unspent budget BANKS as
  cash (earning 0) and an s_t > 1 spends from the bank only as available —
  total contributed capital is identical across arms by construction, so the
  comparison is pure timing discipline, never "more money". For
  btc_accum_policy vs plain DCA.

POLICY gate (Appendix C): overlay total return ≥ baseline AND
maxDD(overlay) < maxDD(baseline), on total equity (cash + position value).
"""
from __future__ import annotations


def max_drawdown(equity: list[float]) -> float:
    """Peak-to-trough max drawdown of an equity series, as a POSITIVE fraction
    (0.25 = a 25% drawdown). 0.0 for len < 2 or a non-decreasing series."""
    peak, mdd = float("-inf"), 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def equity_curve(closes: list[float], exposure: list[float], *,
                 switch_cost_bps: float = 10.0, start_equity: float = 1.0
                 ) -> list[float]:
    """Equity of an exposure policy over a close series (pure, causal).

    ``exposure[t]`` (0..1) is decided AT close t and earns the t→t+1 return.
    Each CHANGE in exposure pays ``switch_cost_bps`` × |Δexposure| (a round trip
    from 0→1→0 costs 2× the half-turn — pass the ROUND-TRIP bps halved if that
    is the convention wanted; btc uses 10bps RT so 5bps per half-turn is
    conservative at 10 here). Returns the equity series aligned to closes.
    """
    n = len(closes)
    if n == 0 or len(exposure) != n:
        return []
    eq = [start_equity]
    prev_exp = 0.0
    for t in range(n - 1):
        e = max(0.0, min(1.0, exposure[t]))
        cost = abs(e - prev_exp) * switch_cost_bps / 10_000.0
        ret = closes[t + 1] / closes[t] - 1.0
        eq.append(eq[-1] * (1.0 - cost) * (1.0 + e * ret))
        prev_exp = e
    return eq


def dca_simulate(closes: list[float], contribution_idx: list[int],
                 budget: float = 1.0, scales: list[float] | None = None,
                 *, s_max: float = 2.0) -> dict:
    """Contribution-timing policy with banked cash (pure, causal).

    ``contribution_idx``: bar indices where a budget of ``budget`` arrives.
    ``scales`` (aligned with contribution_idx, default all-1 = plain DCA): the
    policy's spend multiplier decided AT that bar's close; the actual spend is
    min(scale, s_max) × budget capped by available cash (arriving budget +
    bank). Unspent cash banks at 0%.

    Returns {"equity": [...], "units": float, "cash": float, "contributed":
    float, "final_value": float, "max_drawdown": float, "total_return": float}
    where equity is marked at every close (cash + units × close) and
    total_return = final_value / contributed − 1.
    """
    n = len(closes)
    scales = scales if scales is not None else [1.0] * len(contribution_idx)
    contrib_at = {i: s for i, s in zip(contribution_idx, scales)}
    units = 0.0
    cash = 0.0
    contributed = 0.0
    equity: list[float] = []
    for t in range(n):
        if t in contrib_at:
            cash += budget
            contributed += budget
            want = max(0.0, min(float(contrib_at[t]), s_max)) * budget
            spend = min(want, cash)
            if closes[t] > 0:
                units += spend / closes[t]
                cash -= spend
        equity.append(cash + units * closes[t])
    final = equity[-1] if equity else 0.0
    return {"equity": equity, "units": units, "cash": cash,
            "contributed": contributed, "final_value": final,
            "max_drawdown": max_drawdown(equity),
            "total_return": (final / contributed - 1.0) if contributed else 0.0}


def rebalance_backtest(port_ret: list[float], bench_ret: list[float]) -> dict:
    """Monthly-rebalance long-portfolio vs a benchmark from aligned per-period
    return series (pure). ``port_ret[m]`` / ``bench_ret[m]`` are the realized
    fractional returns of the portfolio and benchmark over rebalance period m.

    Returns {n_periods, port_curve, bench_curve, active, port_total, bench_total,
    active_total, port_maxdd, bench_maxdd} where ``active[m]`` = port − bench is
    the monthly active-return series the gate's clustered t runs on, and the
    curves compound from 1.0. Missing (None) periods are dropped pairwise so a
    gap in one series never desynchronizes the other.
    """
    pairs = [(p, b) for p, b in zip(port_ret, bench_ret)
             if p is not None and b is not None]
    if not pairs:
        return {"n_periods": 0, "port_curve": [], "bench_curve": [], "active": [],
                "port_total": None, "bench_total": None, "active_total": None,
                "port_maxdd": None, "bench_maxdd": None}
    port_curve, bench_curve = [1.0], [1.0]
    active = []
    for p, b in pairs:
        port_curve.append(port_curve[-1] * (1.0 + p))
        bench_curve.append(bench_curve[-1] * (1.0 + b))
        active.append(p - b)
    return {
        "n_periods": len(pairs),
        "port_curve": port_curve, "bench_curve": bench_curve, "active": active,
        "port_total": port_curve[-1] - 1.0, "bench_total": bench_curve[-1] - 1.0,
        "active_total": port_curve[-1] - bench_curve[-1],
        "port_maxdd": max_drawdown(port_curve), "bench_maxdd": max_drawdown(bench_curve),
    }


def policy_vs_baseline(overlay: dict, baseline: dict) -> dict:
    """The two POLICY-gate legs from two simulator outputs (either simulator)."""
    o_ret = overlay.get("total_return")
    b_ret = baseline.get("total_return")
    return {"overlay_return": o_ret, "baseline_return": b_ret,
            "overlay_maxdd": overlay.get("max_drawdown"),
            "baseline_maxdd": baseline.get("max_drawdown"),
            "return_ok": o_ret >= b_ret,
            "drawdown_ok": overlay.get("max_drawdown") < baseline.get("max_drawdown")}

"""Live forward-testing — how past signals actually played out.

Out-of-sample by construction: each past run/alert is scored against what BTC
actually did N days later (priced from the stored 1d candles), and the stats
accumulate as time passes. This can't overfit — it's the real track record the
system earns going forward. Pure functions given their DB inputs.
"""
from __future__ import annotations

from datetime import datetime

_DAY_MS = 86_400_000


def _price_at(cc: list[tuple[int, float]], target_ms: int) -> float | None:
    """Close of the newest candle at/before target_ms (cc ascending by ts)."""
    best = None
    for ts, close in cc:
        if ts <= target_ms:
            best = close
        else:
            break
    return best


def long_term_performance(runs: list[dict], candles: list[dict],
                          horizons_days=(30, 90, 180)) -> dict:
    """Forward return of each long-term run, bucketed by whether it was a signal
    (ACCUMULATE/DEEP_VALUE). runs: [{run_ts iso, price, tier}] asc; candles:
    [{ts ms, close}] asc. Only runs old enough to have an N-day-forward price count."""
    cc = [(c["ts"], c["close"]) for c in candles if c.get("close") is not None]
    last_ms = cc[-1][0] if cc else 0
    out: dict = {}
    for h in horizons_days:
        sig, allr = [], []
        for r in runs:
            entry = r.get("price")
            if not entry:
                continue
            try:
                t0 = int(datetime.fromisoformat(r["run_ts"]).timestamp() * 1000)
            except (ValueError, TypeError, KeyError):
                continue
            t_h = t0 + h * _DAY_MS
            if t_h > last_ms:
                continue  # not old enough yet
            fwd = _price_at(cc, t_h)
            if not fwd:
                continue
            ret = fwd / entry - 1.0
            allr.append(ret)
            if r.get("tier") in ("ACCUMULATE", "DEEP_VALUE"):
                sig.append(ret)
        out[f"{h}d"] = {
            "n_signal": len(sig),
            "n_total": len(allr),
            "signal_hit_rate": round(sum(1 for x in sig if x > 0) / len(sig), 3) if sig else None,
            "base_rate": round(sum(1 for x in allr if x > 0) / len(allr), 3) if allr else None,
            "signal_avg_return": round(sum(sig) / len(sig), 4) if sig else None,
        }
    return out


def short_term_performance(alerts: list[dict], candles: list[dict],
                           horizon_days: int = 7) -> dict:
    """Did each fired swing alert move its way over the next ``horizon_days``?
    alerts: [{ts ms, direction, price}]."""
    cc = [(c["ts"], c["close"]) for c in candles if c.get("close") is not None]
    last_ms = cc[-1][0] if cc else 0
    wins = n = 0
    for a in alerts:
        entry, t0 = a.get("price"), a.get("ts")
        if not entry or not t0:
            continue
        t_h = t0 + horizon_days * _DAY_MS
        if t_h > last_ms:
            continue
        fwd = _price_at(cc, t_h)
        if not fwd:
            continue
        n += 1
        up = fwd > entry
        if (a["direction"] == "BUY" and up) or (a["direction"] == "SELL" and not up):
            wins += 1
    return {"n": n, "win_rate": round(wins / n, 3) if n else None, "horizon_days": horizon_days}

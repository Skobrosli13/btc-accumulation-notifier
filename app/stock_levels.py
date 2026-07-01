"""Entry / stop / take-profit engine (pure, I/O-free).

Everything is an ATR/R-multiple frame so a $40 stock and a $400 stock are treated
consistently. Each archetype gets its own stop width, targets and time-stop —
one-size-fits-all stops are how swing systems bleed out:

- **pead_drift** — wide stop (gap volatility) or the earnings-day low (thesis
  invalidation), time-boxed to the drift window.
- **momentum** — wider trailing frame, let the runner run.
- **mean_reversion** — tight stop, quick first target, fast time-stop.

Illustrative risk frame for an alert-only system — NOT advice.
"""
from __future__ import annotations

from .config import Config

# (k_stop, k_t1, k_t2, time_stop_days) in ATR multiples.
ARCHETYPE_LEVELS = {
    "pead_drift":     (2.0, 1.5, 2.75, 12),
    "momentum":       (2.5, 2.0, 3.5, 20),
    "mean_reversion": (1.2, 1.2, 2.0, 5),
}


def compute(direction: str, entry: float | None, atr: float | None, archetype: str,
            cfg: Config, structure_stop: float | None = None) -> dict | None:
    """R-multiple stop/targets for a setup. None if entry/ATR unavailable.

    ``structure_stop`` (e.g. the earnings-day low for a PEAD long) tightens the
    stop when it is closer than the ATR stop — the natural thesis-invalidation
    level — but is never used to LOOSEN risk."""
    if entry is None or atr is None or atr <= 0:
        return None
    k_stop, k_t1, k_t2, tdays = ARCHETYPE_LEVELS.get(
        archetype, (cfg.stock_atr_k_stop, cfg.stock_atr_k_t1, cfg.stock_atr_k_t2,
                    cfg.stock_time_stop_days))
    if direction == "BUY":
        stop = entry - k_stop * atr
        if structure_stop is not None and structure_stop < entry:
            stop = max(stop, structure_stop)   # tighter (higher) stop only
        t1, t2 = entry + k_t1 * atr, entry + k_t2 * atr
    else:
        stop = entry + k_stop * atr
        if structure_stop is not None and structure_stop > entry:
            stop = min(stop, structure_stop)   # tighter (lower) stop only
        t1, t2 = entry - k_t1 * atr, entry - k_t2 * atr
    risk = abs(entry - stop)
    rr = round(abs(t2 - entry) / risk, 2) if risk else None
    rd = 2 if entry >= 20 else 4  # price-appropriate rounding
    return {
        "entry": round(entry, rd), "stop": round(stop, rd),
        "t1": round(t1, rd), "t2": round(t2, rd),
        "atr": round(atr, rd), "risk": round(risk, rd), "rr": rr,
        "risk_pct": round(risk / entry * 100, 2) if entry else None,
        "time_stop_days": tdays,
    }

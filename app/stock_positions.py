"""Position lifecycle re-pricer (pure, I/O-free) — this IS the forward-test.

Each fired setup becomes a ``stock_positions`` row; every EOD run walks the new
bars since entry and resolves it (stop / t2 / time-stop) or updates its running
excursion. The closed rows are the out-of-sample track record that ``stock_calibrate``
turns into live win-rates. Intrabar convention is deliberately conservative: if a
bar's range touches BOTH the stop and the target, the stop is assumed hit first.

R accounting uses a single full-position exit at the stop, the runner target (t2),
or the time-stop — t1 is shown as a scale-out suggestion but the tracker measures
the honest worst-case single-exit R.
"""
from __future__ import annotations


def _r(direction: str, price: float, entry: float, risk: float) -> float:
    """Signed R of ``price`` relative to entry (positive = favorable)."""
    if risk <= 0:
        return 0.0
    return (price - entry) / risk if direction == "BUY" else (entry - price) / risk


def reprice(position: dict, new_bars: list[dict], run_ts: str,
            time_stop_days: int) -> dict:
    """Resolve/advance one open position against bars AFTER its entry.

    ``new_bars`` = [{ts,high,low,close}, ...] oldest->newest, ts > opened_ts.
    Returns a dict describing the update: either
      {status:'CLOSED', exit_price, realized_r, exit_reason, closed_ts, mfe_r, mae_r}
    or {status:'OPEN', mfe_r, mae_r}."""
    direction = position["direction"]
    entry, stop, t2 = position["entry"], position["stop"], position["t2"]
    risk = abs(entry - stop)
    mfe = float(position.get("mfe_r") or 0.0)
    mae = float(position.get("mae_r") or 0.0)

    for i, b in enumerate(new_bars):
        hi, lo, close = b["high"], b["low"], b["close"]
        # running excursion in R
        if direction == "BUY":
            mfe = max(mfe, _r(direction, hi, entry, risk))
            mae = min(mae, _r(direction, lo, entry, risk))
            stop_hit, t2_hit = lo <= stop, hi >= t2
        else:
            mfe = max(mfe, _r(direction, lo, entry, risk))
            mae = min(mae, _r(direction, hi, entry, risk))
            stop_hit, t2_hit = hi >= stop, lo <= t2
        if stop_hit:   # conservative: stop wins a same-bar stop+target tie
            return {"status": "CLOSED", "exit_price": stop,
                    "realized_r": round(_r(direction, stop, entry, risk), 3),
                    "exit_reason": "stop", "closed_ts": b["ts"],
                    "mfe_r": round(mfe, 3), "mae_r": round(mae, 3)}
        if t2_hit:
            return {"status": "CLOSED", "exit_price": t2,
                    "realized_r": round(_r(direction, t2, entry, risk), 3),
                    "exit_reason": "t2", "closed_ts": b["ts"],
                    "mfe_r": round(mfe, 3), "mae_r": round(mae, 3)}
        # time-stop: exit at the close of the Nth bar since entry
        if i + 1 >= time_stop_days:
            return {"status": "CLOSED", "exit_price": close,
                    "realized_r": round(_r(direction, close, entry, risk), 3),
                    "exit_reason": "time", "closed_ts": b["ts"],
                    "mfe_r": round(mfe, 3), "mae_r": round(mae, 3)}
    return {"status": "OPEN", "mfe_r": round(mfe, 3), "mae_r": round(mae, 3)}


def summarize(closed: list[dict]) -> dict:
    """Aggregate closed positions into a track record (overall + per archetype)."""
    def agg(rows: list[dict]) -> dict:
        n = len(rows)
        if not n:
            return {"n": 0, "win_rate": None, "expectancy_r": None,
                    "avg_win_r": None, "avg_loss_r": None}
        rs = [float(r.get("realized_r") or 0.0) for r in rows]
        wins = [x for x in rs if x > 0]
        losses = [x for x in rs if x <= 0]
        return {
            "n": n,
            "win_rate": round(len(wins) / n, 3),
            "expectancy_r": round(sum(rs) / n, 3),
            "avg_win_r": round(sum(wins) / len(wins), 3) if wins else None,
            "avg_loss_r": round(sum(losses) / len(losses), 3) if losses else None,
        }

    by_arch: dict[str, list[dict]] = {}
    for r in closed:
        by_arch.setdefault(r.get("archetype", "?"), []).append(r)
    return {"overall": agg(closed),
            "archetypes": {k: agg(v) for k, v in by_arch.items()}}

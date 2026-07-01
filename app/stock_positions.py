"""Position lifecycle re-pricer (pure, I/O-free) — this IS the forward-test.

Each fired setup becomes a ``stock_positions`` row; every EOD run walks the new
bars since entry and resolves it (stop / t2 / time-stop) or updates its running
excursion. The closed rows are the out-of-sample track record that ``stock_calibrate``
turns into live win-rates. Intrabar conventions are deliberately honest: if a
bar's range touches BOTH the stop and the target, the stop is assumed hit first,
and a bar that OPENS through the stop (a gap) fills at the open — not at the
untouchable stop price — so gap-through losses are measured at what a subscriber
could actually get.

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
            time_stop_days: int, cost_bps: float = 0.0) -> dict:
    """Resolve/advance one open position against bars AFTER its entry.

    ``new_bars`` = [{ts,open,high,low,close}, ...] oldest->newest, ts > entry bar.
    ``cost_bps`` is the round-trip commission+slippage charged in R terms (so the
    forward-test measures NET, not gross, expectancy — costs bite tight-stop setups
    much harder than wide-stop ones). A stop exit fills at min(open, stop) for longs
    (max for shorts): a gap through the stop books the achievable open, not the stop.
    Returns either
      {status:'CLOSED', exit_price, realized_r(net), gross_r, cost_r, exit_reason, closed_ts, mfe_r, mae_r}
    or {status:'OPEN', mfe_r, mae_r}."""
    direction = position["direction"]
    entry, stop, t2 = position["entry"], position["stop"], position["t2"]
    risk = abs(entry - stop)
    # Cost in R = round-trip cost fraction / risk fraction (risk/entry).
    cost_r = ((cost_bps / 10000.0) / (risk / entry)) if (risk > 0 and entry) else 0.0
    mfe = float(position.get("mfe_r") or 0.0)
    mae = float(position.get("mae_r") or 0.0)

    def _close(exit_price, reason, ts):
        gross = _r(direction, exit_price, entry, risk)
        return {"status": "CLOSED", "exit_price": exit_price,
                "realized_r": round(gross - cost_r, 3), "gross_r": round(gross, 3),
                "cost_r": round(cost_r, 3), "exit_reason": reason, "closed_ts": ts,
                "mfe_r": round(mfe, 3), "mae_r": round(mae, 3)}

    for i, b in enumerate(new_bars):
        hi, lo, close = b["high"], b["low"], b["close"]
        opn = b.get("open")   # older bar dicts may lack it -> fall back to stop-price fill
        # running excursion in R
        if direction == "BUY":
            mfe = max(mfe, _r(direction, hi, entry, risk))
            mae = min(mae, _r(direction, lo, entry, risk))
            stop_hit, t2_hit = lo <= stop, hi >= t2
            stop_fill = min(opn, stop) if opn is not None else stop
        else:
            mfe = max(mfe, _r(direction, lo, entry, risk))
            mae = min(mae, _r(direction, hi, entry, risk))
            stop_hit, t2_hit = hi >= stop, lo <= t2
            stop_fill = max(opn, stop) if opn is not None else stop
        if stop_hit:   # conservative: stop wins a same-bar stop+target tie
            return _close(stop_fill, "stop", b["ts"])
        if t2_hit:
            return _close(t2, "t2", b["ts"])
        if i + 1 >= time_stop_days:   # time-stop: exit at the close of the Nth bar
            return _close(close, "time", b["ts"])
    return {"status": "OPEN", "mfe_r": round(mfe, 3), "mae_r": round(mae, 3)}


def summarize(closed: list[dict]) -> dict:
    """Aggregate closed positions into a track record (overall + per archetype).

    Voided rows — ``exit_reason='rebased'`` (unverifiable price basis after a
    split/adjustment) or a NULL realized_r — are excluded: they carry no honest
    R and must never count as wins or losses."""
    closed = [r for r in closed
              if r.get("exit_reason") != "rebased" and r.get("realized_r") is not None]

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

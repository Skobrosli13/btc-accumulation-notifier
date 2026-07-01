"""Live forward-testing — how past signals actually played out.

Out-of-sample by construction: each past run/alert is scored against what BTC
actually did N days later (priced from the stored 1d candles), and the stats
accumulate as time passes. Pure functions given their DB inputs.

Honest-sample-size rules (mirrors scripts/calibrate.py):

* Long-term runs are a 6h cadence, so consecutive signal runs share almost all
  of their forward window — one multi-week ACCUMULATE stretch is one market
  outcome, not 80 samples. Consecutive same-tier signal runs therefore collapse
  into EPISODES (sampled at the episode's first run) with a 90% bootstrap CI;
  the raw run-level counts are still served, but the episode counts are the
  honest n.
* Swing alerts are recorded one row per trigger per timeframe per batch, so a
  single market event is deduped to one sample per (direction, candle ts); the
  win is net of the same round-trip cost the offline calibration charges; and
  the unconditional cost-adjusted base rate over the same candles is served
  alongside — a BUY "win rate" on an up-drifting asset means nothing without it.
"""
from __future__ import annotations

import bisect
import random
from datetime import datetime

_DAY_MS = 86_400_000
# Same 10 bps round-trip cost the offline calibration charges (scripts/st_validation).
ROUND_TRIP_COST = 0.001
_SIGNAL_TIERS = ("ACCUMULATE", "DEEP_VALUE")
# A forward price is rejected when the nearest candle at/before the target is
# older than this — a collector-outage gap must not price an "N-day" return
# with a weeks-old close.
_MAX_PRICE_STALE_MS = 2 * _DAY_MS


def _price_at(cc: list[tuple[int, float]], target_ms: int,
              max_stale_ms: int | None = None) -> float | None:
    """Close of the newest candle at/before target_ms (cc ascending by ts).
    With ``max_stale_ms``, a candle more than that much older than the target
    is rejected (returns None) instead of silently pricing across a data gap."""
    i = bisect.bisect_right(cc, (target_ms, float("inf"))) - 1
    if i < 0:
        return None
    ts, close = cc[i]
    if max_stale_ms is not None and target_ms - ts > max_stale_ms:
        return None
    return close


def _bootstrap_ci(outcomes: list[int], iters: int = 2000,
                  seed: int = 42) -> list[float] | None:
    """90% bootstrap CI on 0/1 outcomes (same method as scripts/calibrate.py).
    None below 3 samples — a CI on 1-2 episodes would be theater."""
    if len(outcomes) < 3:
        return None
    rnd = random.Random(seed)
    n = len(outcomes)
    rates = sorted(sum(outcomes[rnd.randrange(n)] for _ in range(n)) / n
                   for _ in range(iters))
    return [round(rates[int(0.05 * iters)], 3), round(rates[int(0.95 * iters)], 3)]


def _iso_ms(iso: str | None) -> int | None:
    try:
        return int(datetime.fromisoformat(iso).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def long_term_performance(runs: list[dict], candles: list[dict],
                          horizons_days=(30, 90, 180)) -> dict:
    """Forward return of each long-term run, bucketed by whether it was a signal
    (ACCUMULATE/DEEP_VALUE), plus the episode-collapsed honest sample.

    runs: [{run_ts iso, price, tier}] asc; candles: [{ts ms, close}] asc. Only
    runs old enough to have an N-day-forward price count. Returns
    {"horizons": {"30": {n_runs, n_signal, signal_hit_rate, base_rate,
    signal_avg_return, episodes, episode_hit_rate, ci}, ...},
    "window": {from, to}} — episode counts (one per consecutive same-tier
    signal stretch) with a bootstrap CI are the numbers to trust; the run-level
    n overstates independent evidence by ~2 orders of magnitude at 6h cadence.
    """
    cc = [(c["ts"], c["close"]) for c in candles if c.get("close") is not None]
    last_ms = cc[-1][0] if cc else 0

    parsed: list[tuple[int, float | None, str | None, str | None]] = []
    for r in runs:
        t0 = _iso_ms(r.get("run_ts"))
        if t0 is None:
            continue
        parsed.append((t0, r.get("price"), r.get("tier"), r.get("run_ts")))

    # Episode starts: a signal run whose predecessor was not the same signal tier.
    episode_starts: list[int] = []
    prev_tier: str | None = None
    for i, (_t0, _entry, tier_, _ts) in enumerate(parsed):
        if tier_ in _SIGNAL_TIERS and tier_ != prev_tier:
            episode_starts.append(i)
        prev_tier = tier_

    def _fwd_return(idx: int, h: int) -> float | None:
        t0, entry, _tier, _ts = parsed[idx]
        if not entry:
            return None
        t_h = t0 + h * _DAY_MS
        if t_h > last_ms:
            return None  # not old enough yet
        fwd = _price_at(cc, t_h, _MAX_PRICE_STALE_MS)
        if not fwd:
            return None
        return fwd / entry - 1.0

    horizons: dict = {}
    for h in horizons_days:
        sig, allr = [], []
        for i, (_t0, _entry, tier_, _ts) in enumerate(parsed):
            ret = _fwd_return(i, h)
            if ret is None:
                continue
            allr.append(ret)
            if tier_ in _SIGNAL_TIERS:
                sig.append(ret)
        ep_outcomes = []
        for i in episode_starts:
            ret = _fwd_return(i, h)
            if ret is not None:
                ep_outcomes.append(1 if ret > 0 else 0)
        horizons[str(h)] = {
            "n_runs": len(allr),
            "n_signal": len(sig),
            "signal_hit_rate": round(sum(1 for x in sig if x > 0) / len(sig), 3) if sig else None,
            "base_rate": round(sum(1 for x in allr if x > 0) / len(allr), 3) if allr else None,
            "signal_avg_return": round(sum(sig) / len(sig), 4) if sig else None,
            "episodes": len(ep_outcomes),
            "episode_hit_rate": (round(sum(ep_outcomes) / len(ep_outcomes), 3)
                                 if ep_outcomes else None),
            "ci": _bootstrap_ci(ep_outcomes),
        }
    return {
        "horizons": horizons,
        "window": {"from": parsed[0][3] if parsed else None,
                   "to": parsed[-1][3] if parsed else None},
    }


def short_term_performance(alerts: list[dict], candles: list[dict],
                           horizon_days: int = 7) -> dict:
    """Cost-adjusted forward outcome of the fired swing alerts vs the
    unconditional base rate.

    alerts: [{ts ms, direction, price, trigger_key?, created_at?}]. One market
    event counts ONCE per (direction, candle ts) — the collector records a row
    per trigger per timeframe per batched email, so raw rows overcount a single
    event 2+x (the confluence gate guarantees >=2 same-direction rows). The
    window is anchored at the alert's actionable moment (``created_at`` wall
    clock when present; the candle open otherwise — the trigger candle's open
    predates the actionable close by up to a bar). The outcome is close-to-close
    at the horizon NET of ROUND_TRIP_COST; the alert's ATR stop/target plan is
    NOT simulated. ``base_rate`` is the unconditional cost-adjusted N-day
    BUY-side up-rate over the same candles: a win_rate that does not beat it is
    drift, not edge.
    """
    cc = [(c["ts"], c["close"]) for c in candles if c.get("close") is not None]
    last_ms = cc[-1][0] if cc else 0

    def _anchor_ms(a: dict) -> int | None:
        return _iso_ms(a.get("created_at")) or a.get("ts")

    def _outcome(a: dict) -> float | None:
        """Cost-adjusted signed return, or None while immature/unpriceable."""
        entry, t0 = a.get("price"), _anchor_ms(a)
        if not entry or not t0:
            return None
        t_h = t0 + horizon_days * _DAY_MS
        if t_h > last_ms:
            return None
        fwd = _price_at(cc, t_h, _MAX_PRICE_STALE_MS)
        if not fwd:
            return None
        ret = fwd / entry - 1.0
        return (ret if a.get("direction") == "BUY" else -ret) - ROUND_TRIP_COST

    # One event per (direction, candle ts); one per (key, direction, ts) for the
    # per-trigger scoreboard (a key still fires separately on 4h vs 1d candles).
    events: dict[tuple, dict] = {}
    key_events: dict[tuple, dict] = {}
    for a in alerts:
        if a.get("ts") is None or a.get("direction") not in ("BUY", "SELL"):
            continue
        events.setdefault((a["direction"], a["ts"]), a)
        if a.get("trigger_key"):
            key_events.setdefault((a["trigger_key"], a["direction"], a["ts"]), a)

    def _stats(rows) -> dict:
        outs = [o for o in (_outcome(a) for a in rows) if o is not None]
        n = len(outs)
        return {"n": n,
                "win_rate": round(sum(1 for o in outs if o > 0) / n, 3) if n else None}

    overall = _stats(events.values())
    by_direction = {d: _stats([a for (dd, _ts), a in events.items() if dd == d])
                    for d in ("BUY", "SELL")}
    # Per-trigger-key forward-test scoreboard — this is the ONLY live measurement
    # the FORWARD-TEST flow keys get (st_winrates.json structurally can't carry
    # them), so keep the split so a promote/retire call can eventually be made
    # from out-of-sample data.
    by_key = {k: _stats([a for (kk, _d, _ts), a in key_events.items() if kk == k])
              for k in sorted({k for (k, _d, _ts) in key_events})}

    # Unconditional cost-adjusted BUY-side base rate over the same candle span.
    base: list[int] = []
    for ts, close in cc:
        if not close:
            continue
        t_h = ts + horizon_days * _DAY_MS
        if t_h > last_ms:
            continue
        fwd = _price_at(cc, t_h, _MAX_PRICE_STALE_MS)
        if fwd is None:
            continue
        base.append(1 if (fwd / close - 1.0 - ROUND_TRIP_COST) > 0 else 0)

    return {
        "n_events": overall["n"],
        "win_rate": overall["win_rate"],
        "base_rate": round(sum(base) / len(base), 3) if base else None,
        "horizon_days": horizon_days,
        "by_direction": by_direction,
        "by_key": by_key,
        "note": (f"One event per (direction, candle); win rates net of a "
                 f"{ROUND_TRIP_COST * 100:.1f}% round-trip cost, measured "
                 f"close-to-close at {horizon_days}d (the alert's ATR stop/target "
                 "plan is not simulated). Compare against base_rate — the swing "
                 "layer has no demonstrated edge."),
    }

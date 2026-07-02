"""Live forward-testing — how past signals actually played out.

Out-of-sample by construction: each past run/alert is scored against what BTC
actually did N days later (priced from the stored 1d candles), and the stats
accumulate as time passes. Pure functions given their DB inputs.

Honest-sample-size rules (mirrors scripts/calibrate.py):

* Long-term runs are a 6h cadence, so consecutive signal runs share almost all
  of their forward window — one multi-week ACCUMULATE stretch is one market
  outcome, not 80 samples. Each CONTIGUOUS stretch of signal-tier runs
  (regardless of ACCUMULATE<->DEEP_VALUE steps inside it) therefore collapses
  into ONE episode, sampled at its first run — the same collapse rule as
  scripts/calibrate.py. Even episodes overlap at the longer horizons, so the
  90% bootstrap CI is computed over the SPACED subset (episode starts >= the
  horizon apart, mirroring calibrate._spaced) served as episodes_effective;
  the raw run/episode counts are still served, but episodes_effective is the
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


def _spaced(starts: list[int], ts_ms: list[int], h_days: int) -> list[int]:
    """Greedy subset of episode-start indices whose timestamps are >= ``h_days``
    apart (each compared against the last KEPT start), so the CI sample's
    forward windows do NOT overlap — mirrors scripts/calibrate.py::_spaced.
    Adjacent overlapping windows i.i.d.-resampled would overstate the evidence;
    this small subset is the honest per-horizon sample."""
    kept: list[int] = []
    last: int | None = None
    h_ms = h_days * _DAY_MS
    for i in starts:
        if last is None or ts_ms[i] - last >= h_ms:
            kept.append(i)
            last = ts_ms[i]
    return kept


def long_term_performance(runs: list[dict], candles: list[dict],
                          horizons_days=(30, 90, 180)) -> dict:
    """Forward return of each long-term run, bucketed by whether it was a signal
    (ACCUMULATE/DEEP_VALUE), plus the episode-collapsed honest sample.

    runs: [{run_ts iso, price, tier}] asc; candles: [{ts ms, close}] asc. Only
    runs old enough to have an N-day-forward price count. Returns
    {"horizons": {"30": {n_runs, n_signal, signal_hit_rate, base_rate,
    signal_avg_return, episodes, episode_hit_rate, episodes_effective,
    episode_hit_rate_effective, ci}, ...}, "window": {from, to}} —
    episodes_effective (episode starts spaced >= the horizon, the population
    the ci is bootstrapped over) is the number to trust; the run-level n
    overstates independent evidence by ~2 orders of magnitude at 6h cadence,
    and even raw episode counts overlap at the longer horizons.
    """
    cc = [(c["ts"], c["close"]) for c in candles if c.get("close") is not None]
    last_ms = cc[-1][0] if cc else 0

    parsed: list[tuple[int, float | None, str | None, str | None]] = []
    for r in runs:
        t0 = _iso_ms(r.get("run_ts"))
        if t0 is None:
            continue
        parsed.append((t0, r.get("price"), r.get("tier"), r.get("run_ts")))

    # Episode starts: a signal run whose predecessor was NOT a signal run — one
    # contiguous signal stretch is one episode regardless of tier steps inside
    # it (an ACCUMULATE->DEEP_VALUE->ACCUMULATE wobble within a single dip is
    # one market outcome, not three). Same collapse rule as scripts/calibrate.py.
    episode_starts: list[int] = []
    in_episode = False
    for i, (_t0, _entry, tier_, _ts) in enumerate(parsed):
        is_signal = tier_ in _SIGNAL_TIERS
        if is_signal and not in_episode:
            episode_starts.append(i)
        in_episode = is_signal
    ts_list = [p[0] for p in parsed]

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
        # Non-overlapping subset: episode starts spaced >= the horizon apart
        # (mirrors scripts/calibrate.py), so the bootstrap below never resamples
        # near-duplicate forward windows as independent evidence.
        eff_outcomes = []
        for i in _spaced(episode_starts, ts_list, h):
            ret = _fwd_return(i, h)
            if ret is not None:
                eff_outcomes.append(1 if ret > 0 else 0)
        horizons[str(h)] = {
            "n_runs": len(allr),
            "n_signal": len(sig),
            "signal_hit_rate": round(sum(1 for x in sig if x > 0) / len(sig), 3) if sig else None,
            "base_rate": round(sum(1 for x in allr if x > 0) / len(allr), 3) if allr else None,
            "signal_avg_return": round(sum(sig) / len(sig), 4) if sig else None,
            "episodes": len(ep_outcomes),
            "episode_hit_rate": (round(sum(ep_outcomes) / len(ep_outcomes), 3)
                                 if ep_outcomes else None),
            "episodes_effective": len(eff_outcomes),
            "episode_hit_rate_effective": (round(sum(eff_outcomes) / len(eff_outcomes), 3)
                                           if eff_outcomes else None),
            # 90% bootstrap CI over the NON-OVERLAPPING (spaced) episode outcomes.
            "ci": _bootstrap_ci(eff_outcomes),
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

"""Shared, network-free validation logic for the short-term swing backtests.

Both ``scripts/backtest_shortterm.py`` and ``scripts/st_calibrate.py`` import
from here so they measure what the live collector (``app/collect_once``) does —
gate-for-gate, with one residual gap: the replay's composite state carries no
funding component (see ``replay_alerts``), so is_counter_trend gating can differ
marginally from live. Everything in this module is pure (no I/O) and works on plain pandas
frames + python lists, so ``tests/test_st_calibrate.py`` exercises it over
synthetic data with no network.

The core fix this module exists to deliver: the old backtests scored EVERY raw
``shortterm.detect_triggers`` fire. Live, a fired trigger only ALERTS if it
clears regime suppression, the confluence gate, and per-(trigger_key, timeframe)
cooldown / same-candle dedup. The win-rates we surface to users as "conviction"
must measure the *alerted* population, not the raw fires. ``replay_alerts``
reproduces the collector's decision path candle-by-candle; the helpers below add
costs, honest expectancy, and statistical rigor (Wilson CI + base rate).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from app import alerting, shortterm
from app.config import Config

# Round-trip transaction cost deducted from every trade's return (entry+exit).
# 10 bps is a deliberately conservative all-in retail estimate (taker fees +
# typical slippage on a liquid BTC pair). Subtracted from raw forward returns and
# baked into the R-multiple expectancy (as a fraction of the per-R risk).
ROUND_TRIP_COST = 0.001  # = 10 bps

# Live recompute window: app/sources/exchange.klines() pulls limit=300 candles,
# so the collector recomputes EWM indicators (EMA/RSI/MACD/ATR) over the trailing
# ~300 bars — NOT the whole history. We must mirror that exact window per step or
# the EWM seeds drift and the backtest fires on different candles than production.
LIVE_WINDOW = 300

# Bars of history an indicator window needs before triggers can be defined.
MIN_LOOKBACK = 35

# Below this many alerted events a cell is statistically meaningless; flag it.
MIN_N = 20


# --- statistics --------------------------------------------------------------

def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% CI for a binomial proportion (z=1.96).

    Far better than the normal approximation at the small n / extreme p typical
    here (a handful of fires, win-rate near 0 or 1). Returns (lo, hi) in [0,1].
    n==0 -> (0.0, 1.0): a no-information interval rather than a divide-by-zero.
    """
    if n <= 0:
        return 0.0, 1.0
    p = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def base_rate(closes: list[float], direction: str, horizon: int) -> float | None:
    """Unconditional P(a random ``horizon``-bar move goes the trade's way).

    The honest yardstick for a win-rate: a BUY signal that "wins" 55% of the time
    is worthless if BTC simply rose over 55% of all windows of that length in the
    sample. Computed from the SAME closes the signals are scored on, after the
    same round-trip cost so it is an apples-to-apples comparison.
    """
    n = len(closes)
    wins = 0
    total = 0
    for i in range(0, n - horizon):
        fwd = closes[i + horizon] / closes[i] - 1.0
        adj = (fwd if direction == "BUY" else -fwd) - ROUND_TRIP_COST
        total += 1
        if adj > 0:
            wins += 1
    return wins / total if total else None


def cell_stats(wins: int, n: int, base: float | None) -> dict:
    """Pack a win-rate cell with its Wilson CI, base rate and significance flags."""
    win_rate = round(wins / n, 3) if n else None
    lo, hi = wilson_interval(wins, n)
    not_significant = None
    if base is not None and n:
        # "Not significant" = the CI straddles the base rate, i.e. we can't
        # distinguish the signal's hit-rate from a coin-weighted-by-drift flip.
        not_significant = lo <= base <= hi
    return {
        "n": n,
        "win_rate": win_rate,
        "wilson_lo": round(lo, 3),
        "wilson_hi": round(hi, 3),
        "base_rate": round(base, 3) if base is not None else None,
        "low_n": n < MIN_N,
        "not_significant": not_significant,
    }


# --- stop / target race + expectancy -----------------------------------------

def race_R(direction: str, lv: dict, highs, lows, closes, i: int, fwd: int) -> float:
    """R-multiple of the ATR stop/target frame over the next ``fwd`` bars.

    Returns the realized R, ALWAYS (never None): if neither stop nor target is hit
    within the horizon the trade is MARKED TO MARKET at the horizon close — dropping
    unresolved trades (the old behavior) is selection bias that flatters expectancy
    by discarding the chop that never reaches either level.

    Pessimism assumption: when a single bar's range spans BOTH stop and target we
    assume the STOP filled first (we only have OHLC, not the intrabar path), so a
    tie resolves against us. This understates expectancy slightly — the honest
    direction to err.

    Costs: ROUND_TRIP_COST is charged as a fraction of the per-R dollar risk so the
    deduction is expressed in the same R units as the payoff.
    """
    stop, target = lv["stop"], lv["target"]
    rr = lv["rr"] or 0.0
    entry = closes[i]
    risk = abs(entry - stop)
    cost_R = (ROUND_TRIP_COST * entry / risk) if risk else 0.0
    end = min(i + 1 + fwd, len(highs))
    for j in range(i + 1, end):
        hi, lo = highs[j], lows[j]
        if direction == "BUY":
            # Stop checked first => same-candle stop-before-target pessimism.
            if lo <= stop:
                return -1.0 - cost_R
            if hi >= target:
                return rr - cost_R
        else:
            if hi >= stop:
                return -1.0 - cost_R
            if lo <= target:
                return rr - cost_R
    # Unresolved within the horizon: mark to market at the horizon close.
    last = closes[end - 1] if end - 1 < len(closes) else closes[-1]
    move = (last - entry) if direction == "BUY" else (entry - last)
    return (move / risk if risk else 0.0) - cost_R


# --- alerted-population replay (mirrors app/collect_once.run) -----------------

@dataclass
class _Cursor:
    """Per-(trigger_key, timeframe) last-alert memory, mirroring store.last_st_alert
    (sent=1 rows only). Used to enforce same-candle dedup + cooldown in the replay."""
    last_ts: int | None = None        # candle open_time (ms) of the last alert
    last_created_ms: int | None = None  # wall-clock (ms) we 'sent' the last alert


@dataclass
class AlertEvent:
    """One alerted trigger fire — the population the win-rates are computed over."""
    key: str
    direction: str
    timeframe: str
    index: int          # row index in the (closed-only) frame at the closed candle
    candle_ts: int      # candle open_time in ms


@dataclass
class ReplayResult:
    raw: list[AlertEvent] = field(default_factory=list)       # every detect_triggers fire
    alerted: list[AlertEvent] = field(default_factory=list)   # post regime+confluence+cooldown


def _cooldown_ok(cursor: _Cursor | None, candle_ts: int, now_ms: int,
                 cooldown_hours: float) -> bool:
    """Pure re-implementation of alerting.decide_st_alert over the replay cursor."""
    if cursor is None or cursor.last_ts is None:
        return True
    if cursor.last_ts == candle_ts:           # same closed candle -> no repeat
        return False
    if cursor.last_created_ms is not None:
        elapsed_h = (now_ms - cursor.last_created_ms) / 3_600_000.0
        if elapsed_h < cooldown_hours:
            return False
    return True


def replay_alerts(df, cfg: Config, timeframe: str, *,
                  regime_series=None, maxh: int = 0,
                  full_warm: bool = True) -> ReplayResult:
    """Walk the closed-candle frame the way the collector does and return the RAW
    fires and the ALERTED subset.

    For each step ``i`` we recompute on ``df.iloc[max(0, i-(LIVE_WINDOW-1)) : i+1]``
    (the live ~300-bar window, not an expanding one), detect triggers on that closed
    candle, then apply the SAME gates as ``app/collect_once.run``:

      1. regime suppression  (cfg.st_regime_suppress + shortterm.regime_aligned)
      2. confluence gate      (cfg.st_require_confluence + shortterm.confluence_ok with
                               dirs.count(direction), regime_aligned, is_counter_trend)
      3. cooldown / same-candle dedup per (trigger_key, timeframe)

    The replay detects CANDLE triggers only (no funding/OI/flow inputs). The
    confluence COUNT matches production exactly: collect_once counts only candle
    triggers toward the direction count (funding_spike_*/oi_surge_*/flow triggers
    are context, excluded from the gate). One RESIDUAL divergence remains: live
    computes the composite state via ``shortterm.evaluate(..., funding=...)``,
    whose st_composite includes a funding component this replay never sees (no
    historical funding series). Near the st_state band edges that component can
    flip ``alerting.is_counter_trend`` and thereby admit/suppress a LONE
    regime-aligned trigger through the confluence gate differently than live —
    so the alerted set here matches production only up to that funding-state
    margin, not exactly. Those non-candle trigger TYPES are not measured here at
    all; st_calibrate lists them under ``unmeasured_keys``.

    Cooldown 'now' is the candle's own close time (open_time + one timeframe), which
    is when the collector would have evaluated it — deterministic and network-free.

    ``regime_series`` is the daily-close series used for the 200DMA regime, sliced
    at each event's evaluation time by daily-candle CLOSE (see ``_regime_at``) so
    the tag has no look-ahead; when None (e.g. a 4h-only synthetic frame) the
    regime is 'unknown' and the regime gates behave exactly as they do live with
    an unknown regime.

    ``maxh`` (max forward horizon) trims the tail so every alerted event has a full
    forward window to score; pass 0 to replay the whole frame.

    ``full_warm`` (default True) starts scoring only once a full LIVE_WINDOW of
    bars precedes the candle: live always recomputes the EWM indicators on ~300
    bars, so events scored on shorter warm-up windows are events live would never
    fire — they are DROPPED, not scored short. Pass False only for short synthetic
    test frames exercising the gate logic.
    """
    closed = df[df["confirmed"]].reset_index(drop=True) if "confirmed" in df.columns else df.reset_index(drop=True)
    n = len(closed)
    tf_ms = _tf_ms(timeframe)
    cursors: dict[str, _Cursor] = {}
    out = ReplayResult()
    stop_at = n - maxh if maxh else n
    first = max(MIN_LOOKBACK, LIVE_WINDOW - 1) if full_warm else MIN_LOOKBACK
    for i in range(first, stop_at):
        window = closed.iloc[max(0, i - (LIVE_WINDOW - 1)): i + 1]
        triggers = shortterm.detect_triggers(window, cfg)
        if not triggers:
            continue
        candle_ts = int(closed["open_time"].iloc[i].timestamp() * 1000)
        now_ms = candle_ts + tf_ms  # collector evaluates after the candle closes
        eval_ts = closed["open_time"].iloc[i] + pd.Timedelta(milliseconds=tf_ms)
        regime = _regime_at(regime_series, eval_ts) if regime_series is not None else "unknown"
        # Deliberately funding-less: live st_composite gets a funding component via
        # shortterm.evaluate(..., funding=...) that this replay has no history for.
        # This state feeds is_counter_trend below — the residual-parity caveat in
        # the docstring (and in st_winrates.json's caveats) documents the skew.
        score, _ = shortterm.st_composite(window, cfg)
        state = shortterm.st_state(score, cfg)
        dirs = [t.direction for t in triggers]
        for trig in triggers:
            out.raw.append(AlertEvent(trig.key, trig.direction, timeframe, i, candle_ts))
            # 1. regime suppression
            if cfg.st_regime_suppress and shortterm.regime_aligned(trig.direction, regime) is False:
                continue
            # 2. confluence gate
            if cfg.st_require_confluence and not shortterm.confluence_ok(
                    dirs.count(trig.direction),
                    shortterm.regime_aligned(trig.direction, regime),
                    alerting.is_counter_trend(trig.direction, state)):
                continue
            # 3. cooldown / same-candle dedup
            cur = cursors.get(trig.key)
            if not _cooldown_ok(cur, candle_ts, now_ms, cfg.st_cooldown_hours):
                continue
            cursors[trig.key] = _Cursor(last_ts=candle_ts, last_created_ms=now_ms)
            out.alerted.append(AlertEvent(trig.key, trig.direction, timeframe, i, candle_ts))
    return out


def collapse_episodes(events: list[AlertEvent], gap_bars: int) -> list[AlertEvent]:
    """Collapse same-key same-direction fires into de-correlated episodes: keep an
    event only when it starts >= ``gap_bars`` after the last KEPT event of that
    key — the same semantics as ``scripts.calibrate._spaced`` (compare against the
    last kept episode, inclusive ``>=``, so two fires exactly one forward horizon
    apart have disjoint close-to-close windows and both count).

    Comparing against the last kept event (NOT the last seen one) matters: a slow
    drip of fires every k < gap_bars bars must still open a new episode once the
    distance from the last kept fire reaches the horizon — chaining last_idx across
    dropped events would collapse arbitrarily long spans of independent forward
    windows into a single episode and understate n.

    Overlapping forward windows are autocorrelated, so counting every fire inflates
    the effective sample size and tightens the CI dishonestly. After cooldown the
    alerted events are already spaced, but episode-collapsing is the belt-and-braces
    de-correlation for cells where the cooldown window is shorter than the horizon.
    """
    by_key: dict[tuple[str, str], list[AlertEvent]] = {}
    for e in events:
        by_key.setdefault((e.key, e.direction), []).append(e)
    kept: list[AlertEvent] = []
    for evs in by_key.values():
        evs = sorted(evs, key=lambda e: e.index)
        last_kept = None
        for e in evs:
            if last_kept is None or (e.index - last_kept) >= gap_bars:
                kept.append(e)
                last_kept = e.index
    return sorted(kept, key=lambda e: e.index)


# --- small time helpers (no exchange import needed) --------------------------

_TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000,
          "4h": 4 * 3_600_000, "1d": 86_400_000, "1w": 7 * 86_400_000}


def _tf_ms(timeframe: str) -> int:
    return _TF_MS.get(timeframe, 4 * 3_600_000)


def _regime_at(daily_close, eval_ts) -> str:
    """200DMA regime as of ``eval_ts`` (the evaluating candle's CLOSE time), using
    only daily candles already CLOSED by then.

    The daily series is indexed by candle OPEN time; a daily candle opened at day D
    closes at D+1 00:00, so only rows with ``open + 1d <= eval_ts`` are known.
    Slicing by open time alone would let an intraday (4h) event see its own day's
    close up to ~20h early — look-ahead exactly at the 200DMA crossings where
    triggers cluster. Mirrors shortterm.current_regime over the closed subset;
    'unknown' when fewer than 200 closed daily candles precede ``eval_ts``.
    """
    if daily_close is None:
        return "unknown"
    try:
        cutoff = eval_ts - pd.Timedelta(days=1)
        sub = daily_close[daily_close.index <= cutoff] if hasattr(daily_close, "index") else None
    except TypeError:
        sub = None
    if sub is None:
        return "unknown"
    return shortterm.current_regime(sub.reset_index(drop=True))

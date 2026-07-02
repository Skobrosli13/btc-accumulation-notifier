"""Offline short-term validation (run MANUALLY) -> app/st_winrates.json.

For each swing trigger, over OKX history, this measures the ALERTED population —
the events that actually reach the user after the live collector's gates (regime
suppression + confluence + per-(key,tf) cooldown / same-candle dedup), recomputed
on the same ~300-candle window production uses. One residual gap remains: the
replay's composite state has no funding component while live's does, so the
is_counter_trend leg of the confluence gate can diverge marginally from
production (see CAVEATS / st_validation.replay_alerts). For each alerted cell it reports:
n, win_rate, a Wilson 95% CI, the unconditional base_rate, and the EXPECTANCY
(avg R-multiple) of the ATR stop/target frame (stop=1.5xATR, target=2.5xATR), net
of a 10 bps round-trip cost and with unresolved trades marked to market.

This is what the dashboard surfaces next to live triggers as "conviction", so it
MUST measure what the system actually alerts — not every raw trigger fire (the old
behavior, which scored a different, larger, more flattering population).

    python -m scripts.st_calibrate        # from the project root (NEEDS network)

Small-sample caveat applies (a few years of one venue); a sanity check, not a
promise. The live services only READ the committed st_winrates.json.

JSON SCHEMA NOTE: each cell carries {direction, n, fires, horizon_bars, win_rate,
wilson_lo, wilson_hi, base_rate, expectancy_R, resolved, low_n, not_significant}.
``n`` counts DE-CORRELATED EPISODES — a same-key alerted fire starting fewer than
horizon_bars after the last KEPT episode is collapsed into it
(st_validation.collapse_episodes, same spacing semantics as calibrate._spaced),
because overlapping return windows counted as independent trials would tighten
the Wilson CI dishonestly. ``fires`` keeps the pre-collapse alerted count. The top
level carries "population":"alerted" plus "unmeasured_keys": alertable trigger
types (funding/OI/flow) that have NO cell here — the API serves them as
{"unmeasured": true} so the dashboard can label them instead of staying silent.
A "raw" mirror is included per timeframe for transparency.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import shortterm                     # noqa: E402
from app.config import load_config            # noqa: E402
from app.sources import exchange              # noqa: E402
from scripts import st_validation as stv      # noqa: E402
from scripts.st_history import deep_klines, daily_regime_series  # noqa: E402

APP_DIR = Path(__file__).resolve().parents[1] / "app"
# candles to pull + forward horizon (candles) for win-rate / stop-target race.
# totals include a LIVE_WINDOW warm-up head: the replay drops events that lack a
# full live-sized window, so we fetch extra to keep the scored sample intact.
PLAN = {"4h": {"total": 14000 + stv.LIVE_WINDOW, "fwd": 24},
        "1d": {"total": 1200 + stv.LIVE_WINDOW, "fwd": 10}}

# Alertable trigger types with NO cell in this artifact: the funding/OI candle
# triggers need funding+OI history the replay doesn't have, and the flow.py
# triggers ride Coinalyze series (~11 months free — see scripts.backtest_flow).
# Served by the API as stats={"unmeasured": true}; alerts for them carry no
# backtested win-rate whatsoever.
UNMEASURED_KEYS = [
    "funding_spike_bull", "funding_spike_bear", "oi_surge_long", "oi_surge_short",
    "cvd_bull_divergence", "cvd_bear_divergence", "oi_new_longs", "oi_new_shorts",
    "liq_long_flush", "liq_short_flush",
]

# Honesty caveats written verbatim into st_winrates.json (module-level so tests can
# pin their presence without running the network regen).
CAVEATS = [
    "Win-rates are the ALERTED population (post regime+confluence+cooldown), "
    "not every raw trigger fire. The two differ; the alerted set is what users get.",
    "n counts de-correlated episodes (each kept fire starts >= horizon_bars after "
    "the last kept one, mirroring calibrate._spaced); overlapping forward windows "
    "are never counted as independent trials.",
    "Cells with n<min_n (low_n=true) are not statistically meaningful.",
    "Cells whose Wilson CI includes base_rate (not_significant=true) are "
    "indistinguishable from the unconditional move rate.",
    "Horizons differ by timeframe (horizon_bars per cell: 4h races 24 bars "
    "= 4 days, 1d races 10 bars = 10 days) and differ from the 7d "
    "live-performance line — different measurements of different things.",
    "Trigger thresholds and the confluence gate were TUNED on this same "
    "sample (in-sample). A cell that ever turns significant must also hold "
    "on a walk-forward holdout (scripts.backtest_shortterm splits at "
    "2024-01-01) before being treated as edge.",
    "funding/OI/flow trigger types (unmeasured_keys) have NO cells here — "
    "no backtest coverage at all.",
    "Residual replay gap: live st_composite includes a funding component the "
    "replay never sees, which can shift the composite state and flip "
    "is_counter_trend at the state-band edges — lone-trigger confluence "
    "decisions can differ marginally, so the alerted population here matches "
    "production only up to that funding-state margin.",
    "One venue (OKX), a few years; past behavior is not a forecast.",
]


def _atr_at(frame, i: int) -> float | None:
    """Live-window ATR as of row ``i`` (recompute on the trailing ~300 bars)."""
    window = frame.iloc[max(0, i - (stv.LIVE_WINDOW - 1)): i + 1]
    return shortterm.compute_indicators(window).get("atr")


def _score_population(events, frame, fwd: int, *, collapse: bool = True) -> dict:
    """Build the per-trigger cells for a population of alerted/raw events.

    ``collapse=True`` first de-correlates the events via
    ``stv.collapse_episodes(gap_bars=fwd)``: same-key fires closer than the forward
    horizon (12h cooldown vs a 96h/240h horizon = up to ~8-10x window overlap)
    would inflate n and dishonestly tighten the Wilson CI. Each cell's ``n``
    therefore counts EPISODES; ``fires`` keeps the pre-collapse count and
    ``horizon_bars`` records the horizon the win-rate races over."""
    pre_counts = Counter(e.key for e in events)
    if collapse:
        events = stv.collapse_episodes(events, gap_bars=fwd)
    closes = frame["close"].tolist()
    highs = frame["high"].tolist()
    lows = frame["low"].tolist()
    by_key: dict[str, list] = {}
    dir_by_key: dict[str, str] = {}
    for e in events:
        by_key.setdefault(e.key, []).append(e)
        dir_by_key[e.key] = e.direction

    out: dict[str, dict] = {}
    for key, evs in by_key.items():
        direction = dir_by_key[key]
        wins = 0
        scored = 0
        rs: list[float] = []
        for e in evs:
            i = e.index
            if i + fwd >= len(closes):
                continue
            scored += 1
            fwd_ret = closes[i + fwd] / closes[i] - 1.0
            adj = (fwd_ret if direction == "BUY" else -fwd_ret) - stv.ROUND_TRIP_COST
            if adj > 0:
                wins += 1
            lv = shortterm.trade_levels(direction, closes[i], _atr_at(frame, i))
            if lv:
                rs.append(stv.race_R(direction, lv, highs, lows, closes, i, fwd))
        base = stv.base_rate(closes, direction, fwd)
        cell = stv.cell_stats(wins, scored, base)
        cell["direction"] = direction
        cell["fires"] = pre_counts[key]     # pre-collapse events for this key
        cell["horizon_bars"] = fwd          # the window the win-rate races over
        exp = round(sum(rs) / len(rs), 3) if rs else None
        cell["expectancy_R"] = exp
        # Back-compat alias: the live API/dashboard still read "atr_expectancy_R".
        cell["atr_expectancy_R"] = exp
        cell["resolved"] = len(rs)  # all marked-to-market now, so == scored when ATR available
        out[key] = cell
    return out


def main() -> int:
    cfg = load_config()
    out: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "population": "alerted",
        "method": ("Replays the live alert path (regime+confluence+cooldown/same-candle "
                   "dedup) on the ~300-candle live window; win-rates are the ALERTED "
                   "events only, DE-CORRELATED to episodes (same-key fires within the "
                   "forward horizon collapsed to the first — n counts episodes, 'fires' "
                   "the pre-collapse count). Forward returns net of a "
                   f"{stv.ROUND_TRIP_COST*100:.1f}% round-trip cost; unresolved ATR "
                   "stop/target trades marked to market at the horizon close; "
                   "same-candle stop-before-target tie resolved against us."),
        "round_trip_cost": stv.ROUND_TRIP_COST,
        "min_n": stv.MIN_N,
        "live_window": stv.LIVE_WINDOW,
        "unmeasured_keys": UNMEASURED_KEYS,
        "timeframes": {},
        "caveats": list(CAVEATS),
    }

    regime_series = None
    try:
        regime_series = daily_regime_series(cfg.symbol)
    except Exception as exc:  # noqa: BLE001
        print(f"(no daily regime series: {exc})")

    for tf, spec in PLAN.items():
        try:
            df = exchange.closed_only(deep_klines(tf, spec["total"], cfg.symbol))
        except Exception as exc:  # noqa: BLE001
            print(f"{tf}: fetch failed: {exc}")
            continue
        fwd = spec["fwd"]
        replay = stv.replay_alerts(df, cfg, tf, regime_series=regime_series, maxh=fwd)
        alerted_cells = _score_population(replay.alerted, df, fwd)
        raw_cells = _score_population(replay.raw, df, fwd)
        # IMPORTANT: per-trigger ALERTED cells live DIRECTLY under timeframes[tf] so
        # the read-only API (app/api.py: wr.get(t.key)) and the dashboard keep working
        # unchanged. Metadata + the raw mirror go under "_"-prefixed reserved keys
        # that can never collide with a real trigger_key.
        tf_block = dict(alerted_cells)
        tf_block["_meta"] = {
            "population": "alerted",
            "candles": len(df),
            "from": str(df["open_time"].iloc[0].date()),
            "to": str(df["open_time"].iloc[-1].date()),
            "raw_fires": len(replay.raw),
            "alerted_fires": len(replay.alerted),
            "alerted_episodes": len(stv.collapse_episodes(replay.alerted, gap_bars=fwd)),
            "horizon_candles": fwd,
        }
        tf_block["_raw"] = raw_cells  # pre-gate cells, for comparison (NOT what users see)
        out["timeframes"][tf] = tf_block
        print(f"{tf} ({len(df)} candles, {len(replay.alerted)} alerted / "
              f"{len(replay.raw)} raw; horizon {fwd} bars):")
        for k, v in sorted(alerted_cells.items()):
            flag = " [low-n]" if v["low_n"] else (" [ns]" if v["not_significant"] else "")
            print(f"  {k:<24} {v['direction']:<4} n={v['n']:<4} (ep of {v['fires']} fires) "
                  f"win={v['win_rate']} CI=[{v['wilson_lo']},{v['wilson_hi']}] "
                  f"base={v['base_rate']} R={v['expectancy_R']} "
                  f"h={v['horizon_bars']}bars{flag}")

    (APP_DIR / "st_winrates.json").write_text(json.dumps(out, indent=2))
    print("wrote app/st_winrates.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

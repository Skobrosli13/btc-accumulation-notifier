"""Offline short-term validation (run MANUALLY) -> app/st_winrates.json.

For each swing trigger, over OKX history, this measures the ALERTED population —
the events that actually reach the user after the live collector's gates (regime
suppression + confluence + per-(key,tf) cooldown / same-candle dedup), recomputed
on the same ~300-candle window production uses. For each alerted cell it reports:
n, win_rate, a Wilson 95% CI, the unconditional base_rate, and the EXPECTANCY
(avg R-multiple) of the ATR stop/target frame (stop=1.5xATR, target=2.5xATR), net
of a 10 bps round-trip cost and with unresolved trades marked to market.

This is what the dashboard surfaces next to live triggers as "conviction", so it
MUST measure what the system actually alerts — not every raw trigger fire (the old
behavior, which scored a different, larger, more flattering population).

    python -m scripts.st_calibrate        # from the project root (NEEDS network)

Small-sample caveat applies (a few years of one venue); a sanity check, not a
promise. The live services only READ the committed st_winrates.json.

JSON SCHEMA NOTE: this version changed the schema. Each cell now carries
{direction, n, win_rate, wilson_lo, wilson_hi, base_rate, expectancy_R, resolved,
low_n, not_significant}, and the top level carries "population":"alerted" so
readers know the win-rates are the alerted (not raw) population. A "raw" mirror is
included per timeframe for transparency.
"""
from __future__ import annotations

import json
import sys
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
PLAN = {"4h": {"total": 14000, "fwd": 24}, "1d": {"total": 1200, "fwd": 10}}


def _atr_at(frame, i: int) -> float | None:
    """Live-window ATR as of row ``i`` (recompute on the trailing ~300 bars)."""
    window = frame.iloc[max(0, i - (stv.LIVE_WINDOW - 1)): i + 1]
    return shortterm.compute_indicators(window).get("atr")


def _score_population(events, frame, fwd: int) -> dict:
    """Build the per-trigger cells for a population of alerted/raw events."""
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
                   "events only. Forward returns net of a "
                   f"{stv.ROUND_TRIP_COST*100:.1f}% round-trip cost; unresolved ATR "
                   "stop/target trades marked to market at the horizon close; "
                   "same-candle stop-before-target tie resolved against us."),
        "round_trip_cost": stv.ROUND_TRIP_COST,
        "min_n": stv.MIN_N,
        "live_window": stv.LIVE_WINDOW,
        "timeframes": {},
        "caveats": [
            "Win-rates are the ALERTED population (post regime+confluence+cooldown), "
            "not every raw trigger fire. The two differ; the alerted set is what users get.",
            "Cells with n<min_n (low_n=true) are not statistically meaningful.",
            "Cells whose Wilson CI includes base_rate (not_significant=true) are "
            "indistinguishable from the unconditional move rate.",
            "One venue (OKX), a few years; past behavior is not a forecast.",
        ],
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
            "horizon_candles": fwd,
        }
        tf_block["_raw"] = raw_cells  # pre-gate cells, for comparison (NOT what users see)
        out["timeframes"][tf] = tf_block
        print(f"{tf} ({len(df)} candles, {len(replay.alerted)} alerted / "
              f"{len(replay.raw)} raw):")
        for k, v in sorted(alerted_cells.items()):
            flag = " [low-n]" if v["low_n"] else (" [ns]" if v["not_significant"] else "")
            print(f"  {k:<24} {v['direction']:<4} n={v['n']:<4} win={v['win_rate']} "
                  f"CI=[{v['wilson_lo']},{v['wilson_hi']}] base={v['base_rate']} "
                  f"R={v['expectancy_R']}{flag}")

    (APP_DIR / "st_winrates.json").write_text(json.dumps(out, indent=2))
    print("wrote app/st_winrates.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

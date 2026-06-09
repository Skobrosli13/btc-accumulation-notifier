"""Offline short-term validation (run MANUALLY) -> app/st_winrates.json.

For each swing trigger, over OKX history: how often it fired, its forward win-rate
at a horizon, and the EXPECTANCY (avg R-multiple) of the ATR stop/target frame the
playbook shows (stop=1.5xATR, target=2.5xATR). Surfaced live next to triggers so
users see conviction. Also validates whether that ATR frame is positive-expectancy.

    python -m scripts.st_calibrate        # from the project root

Small-sample caveat applies (a few years of one venue); a sanity check, not a promise.
The live services only READ the committed st_winrates.json.
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

APP_DIR = Path(__file__).resolve().parents[1] / "app"
# candles to pull + the forward horizon (candles) for the win-rate / stop-target race.
PLAN = {"4h": {"total": 1500, "fwd": 24}, "1d": {"total": 1000, "fwd": 10}}
MIN_LOOKBACK = 35


def _race(direction: str, lv: dict, highs, lows, i: int, fwd: int) -> float | None:
    """R-multiple if the ATR stop or target is hit first within the horizon, else None."""
    stop, target, rr = lv["stop"], lv["target"], (lv["rr"] or 0.0)
    for j in range(i + 1, min(i + 1 + fwd, len(highs))):
        hi, lo = highs[j], lows[j]
        if direction == "BUY":
            if lo <= stop:
                return -1.0
            if hi >= target:
                return rr
        else:
            if hi >= stop:
                return -1.0
            if lo <= target:
                return rr
    return None


def _walk(df, cfg, fwd: int) -> dict:
    closes = df["close"].tolist()
    highs = df["high"].tolist()
    lows = df["low"].tolist()
    n = len(df)
    res: dict[str, dict] = {}
    for i in range(MIN_LOOKBACK, n - fwd):
        window = df.iloc[: i + 1]
        trigs = shortterm.detect_triggers(window, cfg)
        if not trigs:
            continue
        atr = shortterm.compute_indicators(window).get("atr")
        entry = closes[i]
        for trig in trigs:
            rec = res.setdefault(trig.key, {"dir": trig.direction, "n": 0, "wins": 0, "R": []})
            rec["n"] += 1
            fwd_ret = closes[i + fwd] / entry - 1.0
            if (trig.direction == "BUY" and fwd_ret > 0) or (trig.direction == "SELL" and fwd_ret < 0):
                rec["wins"] += 1
            lv = shortterm.trade_levels(trig.direction, entry, atr)
            if lv:
                r = _race(trig.direction, lv, highs, lows, i, fwd)
                if r is not None:
                    rec["R"].append(r)
    return res


def main() -> int:
    cfg = load_config()
    out: dict = {"generated_at": datetime.now(timezone.utc).isoformat(), "timeframes": {}}
    for tf, spec in PLAN.items():
        try:
            df = exchange.closed_only(exchange.klines_history(tf, spec["total"], cfg.symbol))
        except Exception as exc:  # noqa: BLE001
            print(f"{tf}: fetch failed: {exc}")
            continue
        res = _walk(df, cfg, spec["fwd"])
        tf_out = {}
        for key, rec in res.items():
            win = round(rec["wins"] / rec["n"], 3) if rec["n"] else None
            exp = round(sum(rec["R"]) / len(rec["R"]), 3) if rec["R"] else None
            tf_out[key] = {"direction": rec["dir"], "n": rec["n"], "win_rate": win,
                           "atr_expectancy_R": exp, "resolved": len(rec["R"])}
        out["timeframes"][tf] = tf_out
        print(f"{tf} ({len(df)} candles): " +
              ", ".join(f"{k} {v['win_rate']}/{v['n']} (R={v['atr_expectancy_R']})"
                        for k, v in sorted(tf_out.items())))
    (APP_DIR / "st_winrates.json").write_text(json.dumps(out, indent=2))
    print("wrote app/st_winrates.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

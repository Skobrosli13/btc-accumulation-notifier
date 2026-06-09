"""Short-term swing scoring (two-sided, 4h/1d).

Mirrors the spirit of ``app/scoring.py``: pure, testable functions, no I/O. Where
the long-term score is one-directional accumulation confidence (0..100), the
short-term score is **signed** (-100..+100): positive = swing BUY, negative =
swing SELL/exit/short. Triggers are discrete swing events evaluated on the latest
**closed** candle; the composite is a blended signed read of momentum + mean
reversion + positioning.

Indicators are plain pandas (no TA-Lib). Thresholds come from Config so they are
tunable and exercised by ``scripts/backtest_shortterm.py``.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .config import Config


# --- Indicator primitives (return full Series, oldest->newest) ---------------

def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    out = 100 - 100 / (1 + rs)
    # Edge cases at the window seams:
    out = out.mask((avg_loss == 0) & (avg_gain > 0), 100.0)   # only gains -> 100
    out = out.mask((avg_gain == 0) & (avg_loss > 0), 0.0)     # only losses -> 0
    out = out.mask((avg_gain == 0) & (avg_loss == 0), 50.0)   # flat -> neutral 50
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def bollinger(close: pd.Series, period: int = 20, mult: float = 2.0):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = mid + mult * std
    lower = mid - mult * std
    pctb = (close - lower) / (upper - lower)
    return mid, upper, lower, pctb


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


# --- Trigger model -----------------------------------------------------------

@dataclass(frozen=True)
class Trigger:
    key: str            # stable trigger_key for cooldown/debounce
    direction: str      # "BUY" | "SELL"
    label: str          # human-readable
    detail: str = ""


def _closed(df: pd.DataFrame) -> pd.DataFrame:
    """Use confirmed candles only if the frame carries a 'confirmed' column."""
    if "confirmed" in df.columns:
        return df[df["confirmed"]].reset_index(drop=True)
    return df.reset_index(drop=True)


def compute_indicators(df: pd.DataFrame) -> dict:
    """Latest + previous values of every indicator from a CLOSED-candle frame.

    Returns None-valued fields when there is insufficient history rather than
    raising, so callers degrade gracefully.
    """
    df = _closed(df)
    if len(df) < 2:
        return {"n": len(df)}
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
    ema9, ema21 = ema(close, 9), ema(close, 21)
    rsi14 = rsi(close, 14)
    macd_line, signal_line, hist = macd(close)
    _mid, bb_up, bb_lo, pctb = bollinger(close)
    atr14 = atr(high, low, close, 14)
    vol_avg = vol.rolling(20).mean()

    def pair(s: pd.Series):
        return (float(s.iloc[-1]) if pd.notna(s.iloc[-1]) else None,
                float(s.iloc[-2]) if pd.notna(s.iloc[-2]) else None)

    price = float(close.iloc[-1])
    atr_now = float(atr14.iloc[-1]) if pd.notna(atr14.iloc[-1]) else None
    return {
        "n": len(df),
        "ts": int(df["open_time"].iloc[-1].timestamp() * 1000),
        "price": price,
        "ema9": pair(ema9), "ema21": pair(ema21),
        "rsi": pair(rsi14),
        "macd_hist": pair(hist), "macd": pair(macd_line), "signal": pair(signal_line),
        "bb_pctb": pair(pctb), "bb_upper": float(bb_up.iloc[-1]) if pd.notna(bb_up.iloc[-1]) else None,
        "bb_lower": float(bb_lo.iloc[-1]) if pd.notna(bb_lo.iloc[-1]) else None,
        "close": pair(close),
        "atr": atr_now,
        "atr_pct": (atr_now / price * 100) if atr_now else None,
        "volume": float(vol.iloc[-1]),
        "vol_avg": float(vol_avg.iloc[-1]) if pd.notna(vol_avg.iloc[-1]) else None,
    }


def _crossed_up(cur, prev) -> bool:
    return cur is not None and prev is not None and prev[1] is not None and cur[0] is not None


def detect_triggers(df: pd.DataFrame, cfg: Config,
                    funding: float | None = None,
                    oi_chg_pct: float | None = None) -> list[Trigger]:
    """Swing events on the latest closed candle. Each returns a stable key so the
    alerting layer can cooldown/debounce per (key, timeframe)."""
    ind = compute_indicators(df)
    if ind.get("n", 0) < 2:
        return []
    out: list[Trigger] = []

    def cross_up(a, b) -> bool:   # a crosses above b
        return (a[1] is not None and b[1] is not None
                and a[1] <= b[1] and a[0] > b[0])

    def cross_dn(a, b) -> bool:
        return (a[1] is not None and b[1] is not None
                and a[1] >= b[1] and a[0] < b[0])

    ema9, ema21 = ind["ema9"], ind["ema21"]
    if ema9[0] is not None and ema21[0] is not None:
        if cross_up(ema9, ema21):
            out.append(Trigger("ema_cross_bull", "BUY", "EMA 9/21 bullish cross"))
        elif cross_dn(ema9, ema21):
            out.append(Trigger("ema_cross_bear", "SELL", "EMA 9/21 bearish cross"))

    hist = ind["macd_hist"]
    if hist[0] is not None and hist[1] is not None:
        if hist[1] <= 0 < hist[0]:
            out.append(Trigger("macd_bull_cross", "BUY", "MACD bullish cross"))
        elif hist[1] >= 0 > hist[0]:
            out.append(Trigger("macd_bear_cross", "SELL", "MACD bearish cross"))

    r = ind["rsi"]
    if r[0] is not None and r[1] is not None:
        if r[1] < cfg.st_rsi_oversold <= r[0]:
            out.append(Trigger("rsi_oversold_bounce", "BUY",
                               f"RSI reclaimed {cfg.st_rsi_oversold:.0f} (oversold bounce)"))
        elif r[1] > cfg.st_rsi_overbought >= r[0]:
            out.append(Trigger("rsi_overbought_rollover", "SELL",
                               f"RSI lost {cfg.st_rsi_overbought:.0f} (overbought rollover)"))

    pctb, close = ind["bb_pctb"], ind["close"]
    if pctb[0] is not None and pctb[1] is not None:
        if pctb[1] <= 0 < pctb[0]:
            out.append(Trigger("bb_lower_reclaim", "BUY", "Reclaimed lower Bollinger band"))
        elif pctb[1] >= 1 > pctb[0]:
            out.append(Trigger("bb_upper_reject", "SELL", "Rejected at upper Bollinger band"))

    if funding is not None:
        if funding <= -cfg.st_funding_spike:
            out.append(Trigger("funding_spike_bull", "BUY",
                               f"Funding deeply negative ({funding:+.4f}) — shorts crowded"))
        elif funding >= cfg.st_funding_spike:
            out.append(Trigger("funding_spike_bear", "SELL",
                               f"Funding elevated ({funding:+.4f}) — longs crowded"))

    # Volume flush: a volume spike on a decisive candle.
    if ind["vol_avg"] and ind["volume"] >= cfg.st_vol_spike_mult * ind["vol_avg"]:
        c = ind["close"]
        if c[0] is not None and c[1] is not None:
            if c[0] < c[1]:
                out.append(Trigger("vol_flush_down", "BUY",
                                   "Volume spike on a down candle — capitulation flush"))
            elif c[0] > c[1]:
                out.append(Trigger("vol_flush_up", "SELL",
                                   "Volume spike on an up candle — possible blow-off"))

    if oi_chg_pct is not None and abs(oi_chg_pct) >= cfg.st_oi_surge_pct and funding is not None:
        # OI surge meaning depends on positioning: crowded longs -> SELL risk, etc.
        if funding >= cfg.st_funding_spike:
            out.append(Trigger("oi_surge_long", "SELL", "OI surge with crowded longs"))
        elif funding <= -cfg.st_funding_spike:
            out.append(Trigger("oi_surge_short", "BUY", "OI surge with crowded shorts"))

    return out


def st_composite(df: pd.DataFrame, cfg: Config, funding: float | None = None) -> tuple[float, dict]:
    """Signed swing *bias* in -100..+100 (positive = bullish regime).

    This is deliberately a **momentum/positioning** read — trend (EMA spread),
    MACD histogram, and funding — NOT a mean-reversion read. RSI/Bollinger
    extremes are counter-trend timing signals; they drive the discrete triggers
    (oversold bounce, band reclaim) rather than the bias sign, so a strong uptrend
    reads bullish (don't fight the trend) while the triggers time the entries.
    Flat/insufficient data -> ~0 (NEUTRAL).
    """
    ind = compute_indicators(df)
    comps: dict[str, float] = {}
    if ind.get("n", 0) >= 2:
        ema9, ema21 = ind["ema9"][0], ind["ema21"][0]
        if ema9 is not None and ema21 and ema21 != 0:
            comps["trend"] = max(-1.0, min(1.0, ((ema9 - ema21) / ema21) / 0.03))

        hist = ind["macd_hist"][0]
        if hist is not None and ind["price"]:
            comps["macd"] = max(-1.0, min(1.0, hist / (ind["price"] * 0.005)))

    if funding is not None and cfg.st_funding_spike:
        comps["funding"] = max(-1.0, min(1.0, -funding / cfg.st_funding_spike))

    if not comps:
        return 0.0, comps
    score = sum(comps.values()) / len(comps) * 100.0
    return max(-100.0, min(100.0, score)), comps


def st_state(score: float, cfg: Config) -> str:
    if score >= cfg.st_strong_buy_threshold:
        return "STRONG_BUY"
    if score >= cfg.st_buy_threshold:
        return "BUY"
    if score <= cfg.st_strong_sell_threshold:
        return "STRONG_SELL"
    if score <= cfg.st_sell_threshold:
        return "SELL"
    return "NEUTRAL"


def trade_levels(direction: str, price: float | None, atr: float | None,
                 k_stop: float = 1.5, k_target: float = 2.5) -> dict | None:
    """ATR-based stop/target for a swing trigger (uses the otherwise-unused ATR).

    BUY: stop = price - k_stop*ATR, target = price + k_target*ATR (SELL mirrored).
    Returns None when price/ATR are unavailable. Illustrative risk frame, not advice.
    """
    if price is None or atr is None or atr <= 0:
        return None
    if direction == "BUY":
        stop, target = price - k_stop * atr, price + k_target * atr
    else:
        stop, target = price + k_stop * atr, price - k_target * atr
    risk = abs(price - stop)
    rr = round(abs(target - price) / risk, 2) if risk else None
    return {"stop": round(stop, 2), "target": round(target, 2), "rr": rr,
            "atr": round(atr, 2)}


def evaluate(df: pd.DataFrame, cfg: Config, funding: float | None = None,
             oi_chg_pct: float | None = None) -> dict:
    """One-call evaluation for the collector: indicators + composite + state +
    triggers, all on the latest CLOSED candle."""
    ind = compute_indicators(df)
    score, comps = st_composite(df, cfg, funding)
    triggers = detect_triggers(df, cfg, funding, oi_chg_pct)
    return {
        "ts": ind.get("ts"),
        "price": ind.get("price"),
        "score": score,
        "state": st_state(score, cfg),
        "components": comps,
        "indicators": ind,
        "triggers": triggers,
    }

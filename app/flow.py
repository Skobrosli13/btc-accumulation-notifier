"""Order-flow signals derived from Coinalyze (CVD, OI participant, liquidations).

Pure and I/O-free, mirroring ``app/scoring.py`` / ``app/shortterm.py`` so it is
directly unit-testable. The collector fetches the raw Coinalyze series
(``app/sources/coinalyze.py``) and passes them here; this turns them into the
SAME ``shortterm.Trigger`` objects, so they ride the existing
confluence / cooldown / regime alert machinery unchanged.

Three reads, all on CLOSED bars of the primary short-term timeframe:

* **CVD divergence** — taker buy/sell imbalance (``delta = 2*buyvol - volume``)
  cumulated into CVD; a regular price/CVD divergence flags hidden buying/selling
  (the free-tier proxy for footprint absorption — see ORDERFLOW notes).
* **OI participant** — the price-vs-OI quadrant: new longs / short-covering /
  new shorts / long-liquidation. Only the high-conviction quadrants, and only
  when OI actually moved (>= ``st_oi_surge_pct``), become triggers.
* **Liquidation flush** — the last bar's liquidations vs the recent mean; a
  long-liq flush is capitulation (BUY the washout), a short-liq flush is a
  squeeze (SELL the exhaustion).

Honesty: these are FORWARD-TEST grade. There is no free historical tick data to
backtest CVD/liquidations against, so they earn trust through confluence (the
existing ``st_require_confluence`` gate) and out-of-sample observation, not a
prior hit-rate. See the established BTC short-term no-edge finding.
"""
from __future__ import annotations

import pandas as pd

from .config import Config
from .shortterm import Trigger

# Stable keys of the order-flow triggers. Used by alerting to caveat them as
# FORWARD-TEST (unbacktested) — distinct from the swing triggers that carry
# st_validation hit-rates.
FLOW_TRIGGER_KEYS = frozenset({
    "cvd_bull_divergence", "cvd_bear_divergence",
    "oi_new_longs", "oi_new_shorts",
    "liq_long_flush", "liq_short_flush",
})


def build_cvd(ohlcv_rows: list[dict]) -> pd.DataFrame:
    """Coinalyze OHLCV rows (oldest->newest) -> frame with per-bar ``delta`` and
    cumulative ``cvd``. Empty frame for empty input.

    ``delta = 2*buyvol - volume`` because sell volume = volume - buyvol, so
    buy - sell = buyvol - (volume - buyvol).
    """
    if not ohlcv_rows:
        return pd.DataFrame()
    df = pd.DataFrame(ohlcv_rows)
    df["delta"] = 2.0 * df["buyvol"] - df["volume"]
    df["cvd"] = df["delta"].cumsum()
    return df


def cvd_divergence(df: pd.DataFrame, lookback: int = 14) -> str | None:
    """Regular CVD/price divergence on the latest closed bar.

    'bullish' — price prints a lower low than the window's trough but CVD is
    higher (sellers can't push the tape down → absorption / hidden buying).
    'bearish' — price prints a higher high than the window's peak but CVD is
    lower (buyers can't lift the tape → distribution / hidden selling).
    None when there is insufficient history or no divergence.
    """
    if df is None or df.empty or not {"cvd", "low", "high"}.issubset(df.columns):
        return None
    w = df.tail(lookback).reset_index(drop=True)
    if len(w) < 4:
        return None
    last = w.iloc[-1]
    prior = w.iloc[:-1]
    trough = prior["low"].idxmin()
    if last["low"] < prior["low"].loc[trough] and last["cvd"] > prior["cvd"].loc[trough]:
        return "bullish"
    peak = prior["high"].idxmax()
    if last["high"] > prior["high"].loc[peak] and last["cvd"] < prior["cvd"].loc[peak]:
        return "bearish"
    return None


def participant(price_chg_pct: float | None, oi_chg_pct: float | None,
                oi_surge_pct: float) -> dict | None:
    """The price-vs-OI participant quadrant on the last closed bar. None if either
    change is unavailable. ``significant`` flags an OI move worth alerting on."""
    if price_chg_pct is None or oi_chg_pct is None:
        return None
    up = price_chg_pct >= 0
    oi_up = oi_chg_pct >= 0
    if up and oi_up:
        state, bias, desc = "new_longs", "BUY", "Price up + OI up — new longs (trend)"
    elif up and not oi_up:
        state, bias, desc = "short_covering", "FADE", "Price up + OI down — short covering (weak)"
    elif (not up) and oi_up:
        state, bias, desc = "new_shorts", "SELL", "Price down + OI up — new shorts (trend)"
    else:
        state, bias, desc = "long_liquidation", "FADE", "Price down + OI down — long liquidation (flush)"
    return {"state": state, "bias": bias, "desc": desc,
            "price_chg_pct": round(price_chg_pct, 3),
            "oi_chg_pct": round(oi_chg_pct, 3),
            "significant": abs(oi_chg_pct) >= oi_surge_pct}


def participant_from_series(closes: list[float], ois: list[float],
                            oi_surge_pct: float) -> dict | None:
    """Convenience wrapper: derive the last-bar price% and OI% change from two
    closed series and classify. None if either series is too short / zero-based.

    NOTE: pairs by POSITION — only safe when both series are known to be aligned.
    The production collector uses ``participant_aligned`` (joins by timestamp)."""
    if len(closes) < 2 or len(ois) < 2:
        return None
    pc = (closes[-1] / closes[-2] - 1.0) * 100.0 if closes[-2] else None
    oc = (ois[-1] / ois[-2] - 1.0) * 100.0 if ois[-2] else None
    return participant(pc, oc, oi_surge_pct)


def participant_aligned(cvd_rows: list[dict], oi_rows: list[dict],
                        oi_surge_pct: float) -> dict | None:
    """Classify the last closed bar's price-vs-OI quadrant, joining the OHLCV and
    OI series BY TIMESTAMP (not by position).

    The two series are fetched in independent Coinalyze calls and trimmed for
    closed bars separately, so they can differ in length / trailing bar. Pairing
    positionally (closes[-1] vs ois[-1]) would then compare bars from different
    times and emit a bogus new_longs/new_shorts trigger. This uses the last two
    bars present in BOTH series. None if fewer than two common bars."""
    oi_by_ts = {r["ts"]: r["oi"] for r in oi_rows
                if r.get("ts") is not None and r.get("oi") is not None}
    common = [(r["ts"], r["close"]) for r in cvd_rows
              if r.get("ts") in oi_by_ts and r.get("close") is not None]
    if len(common) < 2:
        return None
    common.sort(key=lambda x: x[0])
    (ts_prev, c_prev), (ts_last, c_last) = common[-2], common[-1]
    oi_prev, oi_last = oi_by_ts[ts_prev], oi_by_ts[ts_last]
    pc = (c_last / c_prev - 1.0) * 100.0 if c_prev else None
    oc = (oi_last / oi_prev - 1.0) * 100.0 if oi_prev else None
    return participant(pc, oc, oi_surge_pct)


def liquidation_flush(liq_rows: list[dict], mult: float = 3.0,
                      min_usd: float = 0.0) -> tuple[str, float] | None:
    """Detect a liquidation flush on the last closed bar.

    Returns ('long'|'short', usd) when the dominant side of the last bar is at
    least ``mult`` x the mean of the prior bars (self-calibrating, so no brittle
    absolute USD threshold). ``min_usd`` is an absolute floor so a near-zero
    baseline during a quiet/sparse-liquidation stretch can't let a tiny bar clear
    the multiple and manufacture a phantom flush. None otherwise. Long-liq flush =
    capitulation (BUY); short-liq flush = squeeze exhaustion (SELL).
    """
    if not liq_rows or len(liq_rows) < 4:
        return None
    last = liq_rows[-1]
    prior = liq_rows[:-1]
    mean_long = sum(r["long"] for r in prior) / len(prior)
    mean_short = sum(r["short"] for r in prior) / len(prior)
    if (mean_long > 0 and last["long"] >= mult * mean_long
            and last["long"] >= last["short"] and last["long"] >= min_usd):
        return ("long", last["long"])
    if (mean_short > 0 and last["short"] >= mult * mean_short
            and last["short"] > last["long"] and last["short"] >= min_usd):
        return ("short", last["short"])
    return None


def detect_flow_triggers(cvd_df: pd.DataFrame, participant_read: dict | None,
                         liq_flush: tuple[str, float] | None,
                         cfg: Config) -> list[Trigger]:
    """Turn the three flow reads into swing triggers (same Trigger contract as
    shortterm), so they merge into the collector's confluence/cooldown loop."""
    out: list[Trigger] = []

    div = cvd_divergence(cvd_df, cfg.flow_cvd_lookback)
    if div == "bullish":
        out.append(Trigger("cvd_bull_divergence", "BUY", "CVD bullish divergence",
                           "Price lower low but CVD higher — absorption / hidden buying"))
    elif div == "bearish":
        out.append(Trigger("cvd_bear_divergence", "SELL", "CVD bearish divergence",
                           "Price higher high but CVD lower — distribution / hidden selling"))

    if participant_read and participant_read.get("significant"):
        st = participant_read.get("state")
        if st == "new_longs":
            out.append(Trigger("oi_new_longs", "BUY", "New longs (price up + OI up)",
                               participant_read.get("desc", "")))
        elif st == "new_shorts":
            out.append(Trigger("oi_new_shorts", "SELL", "New shorts (price down + OI up)",
                               participant_read.get("desc", "")))

    if liq_flush:
        side, usd = liq_flush
        if side == "long":
            out.append(Trigger("liq_long_flush", "BUY", "Long-liquidation flush",
                               f"${usd / 1e6:.0f}M of longs liquidated — capitulation washout"))
        else:
            out.append(Trigger("liq_short_flush", "SELL", "Short-liquidation flush",
                               f"${usd / 1e6:.0f}M of shorts liquidated — squeeze exhaustion"))

    return out

"""Cross-sectional swing scoring for the stock screener (pure, I/O-free).

Mirrors ``scoring.py``/``shortterm.py`` in spirit: table-driven, testable, no
network. The shape differs from the BTC side — instead of one asset's absolute
0..100, this ranks a *universe* of tickers each close and surfaces the strongest
setups. Reuses the shared indicator primitives (``core.indicators``).

Archetypes (Phase 0 §0.4): only **pead_drift** now surfaces a live setup — the
one documented free edge (fresh earnings surprise whose price reaction confirms
the sign; needs an earnings feed). **momentum** and **mean_reversion** are
DEMOTED to feature-only (measured no-edge): their computations survive in
``features()`` and the ``*_candidate`` functions remain for reference/tests, but
``pick_candidate`` no longer emits them until the harness validates them.

Insider cluster, short-volume and revision reads are *context* modifiers only —
they nudge the composite/confidence but never create a setup on their own (Phase-2
/ forward-test discipline; weak in mega-cap). The composite blends the archetype's
own strength with cross-sectional relative strength, regime alignment and context.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .config import Config
from .core import indicators as ind

# Per-archetype composite weights: (primary strength, cross-sectional rel-strength,
# regime alignment, context bonus). Normalized, so they read as a blend.
ARCHETYPE_WEIGHTS = {
    "pead_drift":     {"primary": 0.55, "rel": 0.15, "regime": 0.15, "context": 0.15},
    "momentum":       {"primary": 0.45, "rel": 0.30, "regime": 0.15, "context": 0.10},
    "mean_reversion": {"primary": 0.55, "rel": 0.10, "regime": 0.20, "context": 0.15},
}

ARCHETYPE_LABELS = {
    "pead_drift": "Post-earnings drift",
    "momentum": "Momentum continuation",
    "mean_reversion": "Mean-reversion (oversold dip)",
}

# Which archetypes carry a *documented* edge in the literature. Kept for reference/
# tests only — the LIVE edge/forward labelling derives from the measured win-rates
# cells via ``stock_confidence.archetype_maturity`` (valid + significant -> edge),
# never from this hardcoded set.
EDGE_ARCHETYPES = {"pead_drift"}


def is_edge(archetype: str) -> bool:
    return archetype in EDGE_ARCHETYPES


def priority_score(composite: float, expectancy_r: float | None) -> float:
    """Rank by EXPECTED VALUE, not raw signal strength: a high-expectancy archetype
    (PEAD, ~+0.45R) outranks a strong-but-low-edge momentum name (~+0.17R) at equal
    composite, so the screener leads with the edge instead of burying it under
    whatever's trending. ``expectancy_r`` is the archetype's calibrated avg-R."""
    return composite * (1.0 + max(-0.5, expectancy_r or 0.0))


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _df(bars: list[dict]) -> pd.DataFrame:
    """bars = [{ts,open,high,low,close,volume}, ...] oldest->newest -> DataFrame."""
    df = pd.DataFrame(bars)
    df["open_time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def features(bars: list[dict]) -> dict | None:
    """Per-ticker technical features from daily bars (closed). None if too short."""
    if not bars or len(bars) < 60:
        return None
    df = _df(bars)
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
    price = float(close.iloc[-1])
    atr14 = ind.atr(high, low, close, 14)
    atr = float(atr14.iloc[-1]) if pd.notna(atr14.iloc[-1]) else None
    rsi14 = ind.rsi(close, 14)
    _mid, _up, _lo, pctb = ind.bollinger(close, 20, 2.0)
    dma50 = float(close.tail(50).mean())
    dma200 = float(close.tail(200).mean()) if len(close) >= 200 else float(close.mean())
    dollar_vol = float((close * vol).tail(20).mean())

    def ret(n: int) -> float | None:
        return (price / float(close.iloc[-1 - n]) - 1.0) if len(close) > n else None

    high_252 = float(close.tail(252).max())
    return {
        "price": price,
        "atr": atr,
        "atr_pct": (atr / price * 100) if atr else None,
        "dollar_vol": dollar_vol,
        "rsi": float(rsi14.iloc[-1]) if pd.notna(rsi14.iloc[-1]) else None,
        "bb_pctb": float(pctb.iloc[-1]) if pd.notna(pctb.iloc[-1]) else None,
        "dma50": dma50, "dma200": dma200,
        "above_50": price >= dma50, "above_200": price >= dma200,
        "ret_5": ret(5), "ret_20": ret(20), "ret_63": ret(63),
        "from_52w_high": (price / high_252 - 1.0) if high_252 else None,
        "last_ts": int(df["ts"].iloc[-1]),
    }


def _earnings_reaction(bars: list[dict], report_ts: int, hour: str | None
                       ) -> tuple[float | None, float | None, int, int | None]:
    """(reaction_return, drift_since_reaction, bars_since_report, reaction_idx).

    reaction = the confirming move on the first session on/after the report
    (next session if reported after-hours), as close/prev_close-1. drift = the
    move from that reaction close to the latest close. reaction_idx = the bar
    index of the reaction session (for volume confirmation)."""
    idx = next((i for i, b in enumerate(bars) if b["ts"] >= report_ts), None)
    if idx is None:
        return None, None, 0, None
    # amc (after market close) -> the market's reaction is the NEXT session.
    if (hour or "").lower() == "amc":
        idx = min(idx + 1, len(bars) - 1)
    if idx < 1 or idx >= len(bars):
        return None, None, len(bars) - 1 - min(idx, len(bars) - 1), None
    reaction = bars[idx]["close"] / bars[idx - 1]["close"] - 1.0
    drift = bars[-1]["close"] / bars[idx]["close"] - 1.0
    return reaction, drift, len(bars) - 1 - idx, idx


@dataclass
class Candidate:
    ticker: str
    direction: str            # BUY | SELL
    archetype: str
    primary: float            # 0..1 archetype strength
    detail: dict = field(default_factory=dict)
    # filled during ranking:
    rel: float = 0.0
    regime: float = 0.0
    context: float = 0.0
    composite: float = 0.0


def pead_candidate(ticker: str, feat: dict, bars: list[dict], earnings: dict | None,
                   cfg: Config) -> Candidate | None:
    """Post-earnings drift setup: fresh surprise whose reaction confirms the sign.

    Strength is a SUE-flavored *quality* read, not just raw surprise %:
    - **SUE proxy** — the earnings-day reaction measured in units of the stock's own
      daily volatility (reaction / ATR%). A 10% pop on a 2%-ATR name is a far bigger
      surprise than 10% on a 12%-ATR name; drift scales with the standardized shock.
    - **Revenue confluence** — a beat on BOTH EPS and revenue drifts more reliably;
      an EPS beat with a revenue miss (low-quality) is damped.
    - **Volume confirmation** — drift holds when the reaction is on heavy volume
      (institutional participation), not a thin pop.
    """
    if not earnings or earnings.get("surprise_pct") is None:
        return None
    sp = earnings["surprise_pct"]
    if abs(sp) < cfg.stock_pead_min_surprise:
        return None
    reaction, drift, bars_since, idx = _earnings_reaction(
        bars, earnings["report_ts"], earnings.get("hour"))
    if reaction is None or bars_since < 1 or bars_since > cfg.stock_pead_lookback_days:
        return None
    # Drift thesis requires the market reaction to AGREE with the surprise sign.
    up = sp > 0 and reaction > 0
    down = sp < 0 and reaction < 0
    if not (up or down):
        return None
    direction = "BUY" if up else "SELL"

    # SUE proxy: reaction standardized by the stock's typical daily range (ATR%).
    atr_pct = feat.get("atr_pct") or 3.0
    reaction_sigma = abs(reaction) / max(atr_pct / 100.0, 0.005)  # in "ATR units"
    conviction = 0.4 * _clamp01(abs(sp) / 12.0) + 0.6 * _clamp01(reaction_sigma / 4.0)

    # Revenue confluence: agree -> boost, diverge -> damp, missing -> neutral.
    rev_sp = earnings.get("rev_surprise_pct")
    if rev_sp is not None and abs(rev_sp) > 0.5:
        rev_factor = 1.15 if (rev_sp > 0) == (sp > 0) else 0.85
    else:
        rev_factor = 1.0

    # Volume confirmation on the reaction bar vs its prior-20d average.
    vol_ratio = None
    if idx is not None and idx >= 20:
        prior = [b["volume"] for b in bars[idx - 20:idx]]
        avg = sum(prior) / len(prior) if prior else 0
        if avg > 0:
            vol_ratio = bars[idx]["volume"] / avg
    vol_factor = 1.1 if (vol_ratio and vol_ratio >= 2.0) else (
        0.9 if (vol_ratio is not None and vol_ratio < 1.0) else 1.0)

    freshness = 1.0 - (bars_since / (cfg.stock_pead_lookback_days + 1))
    strength = _clamp01(conviction * rev_factor * vol_factor * (0.6 + 0.4 * freshness))

    rev_tag = ("" if rev_sp is None else
               (f", rev {'beat' if rev_sp > 0 else 'miss'} {abs(rev_sp):.0f}%"))
    return Candidate(ticker, direction, "pead_drift", strength, detail={
        "surprise_pct": round(sp, 2), "reaction_pct": round(reaction * 100, 2),
        "reaction_sigma": round(reaction_sigma, 2), "rev_surprise_pct":
            (round(rev_sp, 2) if rev_sp is not None else None),
        "vol_ratio": (round(vol_ratio, 2) if vol_ratio is not None else None),
        "drift_since_pct": round((drift or 0) * 100, 2), "bars_since": bars_since,
        "report_ts": earnings["report_ts"], "actual": earnings.get("actual"),
        "estimate": earnings.get("estimate"),
        "catalyst": f"EPS {'beat' if sp > 0 else 'miss'} {abs(sp):.0f}%{rev_tag}, "
                    f"day-1 {reaction*100:+.1f}% ({reaction_sigma:.1f}sd), {bars_since}d into drift",
    })


def momentum_candidate(ticker: str, feat: dict, cfg: Config) -> Candidate | None:
    """Trend-aligned relative strength (long-only). Keyless (prices only)."""
    if not feat.get("above_200") or not feat.get("above_50"):
        return None
    r63 = feat.get("ret_63")
    if r63 is None or r63 <= 0.05:
        return None
    rsi = feat.get("rsi") or 50
    if rsi >= 82:  # too extended to chase
        return None
    strength = _clamp01(r63 / 0.35) * (0.85 if rsi > 70 else 1.0)
    return Candidate(ticker, "BUY", "momentum", strength, detail={
        "ret_63_pct": round(r63 * 100, 1), "ret_20_pct": round((feat.get("ret_20") or 0) * 100, 1),
        "from_52w_high_pct": round((feat.get("from_52w_high") or 0) * 100, 1),
        "catalyst": f"+{r63*100:.0f}% over 3mo, trend-aligned",
    })


def meanrev_candidate(ticker: str, feat: dict, cfg: Config) -> Candidate | None:
    """Oversold dip inside an uptrend (buy the pullback). Tight/fast archetype."""
    rsi = feat.get("rsi")
    if rsi is None or rsi >= cfg.st_rsi_oversold:
        return None
    if not feat.get("above_200"):   # only fade dips WITH the primary trend
        return None
    pctb = feat.get("bb_pctb")
    strength = _clamp01((cfg.st_rsi_oversold - rsi) / cfg.st_rsi_oversold)
    if pctb is not None and pctb < 0:
        strength = _clamp01(strength + 0.15)
    return Candidate(ticker, "BUY", "mean_reversion", strength, detail={
        "rsi": round(rsi, 1), "bb_pctb": (round(pctb, 2) if pctb is not None else None),
        "catalyst": f"RSI {rsi:.0f} oversold in an uptrend",
    })


def pick_candidate(ticker: str, feat: dict, bars: list[dict], earnings: dict | None,
                   cfg: Config) -> Candidate | None:
    """The one archetype that surfaces a tradeable setup for this ticker (or None).

    Phase 0 (§0.4): ``momentum`` and ``mean_reversion`` are DEMOTED from alert
    generators — measured no-edge (momentum +0.004R vs random-entry, ns;
    mean_reversion n=16). Their feature computations survive (``features()``
    keeps ret_5/20/63, dma50/200, rsi, from_52w_high; the ``*_candidate``
    functions stay for reference/tests), but only ``pead_drift`` — the one
    documented free edge — produces a live setup, until the harness validates
    others (Phase 3). This is deliberately narrow; the screener surfaces little
    until SRW-SUE PEAD (§4.5) and the event studies come online.
    """
    return pead_candidate(ticker, feat, bars, earnings, cfg)


def pick_candidate_all(ticker: str, feat: dict, bars: list[dict],
                       earnings: dict | None, cfg: Config) -> Candidate | None:
    """All-archetype selector (PEAD > momentum > mean-reversion) — the pre-§0.4
    behaviour, retained ONLY for the offline backtest (``scripts/stock_backtest``,
    retired at M2). The LIVE screener uses :func:`pick_candidate` (pead-only); this
    lets the backtest keep measuring the demoted archetypes' historical mechanics
    until the harness supersedes it."""
    for fn in (lambda: pead_candidate(ticker, feat, bars, earnings, cfg),
               lambda: momentum_candidate(ticker, feat, cfg),
               lambda: meanrev_candidate(ticker, feat, cfg)):
        c = fn()
        if c is not None:
            return c
    return None


def liquid(feat: dict, cfg: Config) -> bool:
    return (feat.get("price") or 0) >= cfg.stock_min_price and \
           (feat.get("dollar_vol") or 0) >= cfg.stock_min_dollar_vol


def context_score(insider: dict | None,
                  revision: dict | None) -> tuple[float, dict]:
    """0..1 context bonus + a breakdown, from the Phase-2/forward-test layers.
    Absent layers contribute 0 (renormalize-away discipline).

    (Short-volume context was removed in Phase 0 — the off-exchange short-volume
    ratio is market-maker mechanics, not positioning; measured noise.)"""
    parts: dict[str, float] = {}
    if insider and insider.get("buyers"):
        parts["insider"] = _clamp01(insider["buyers"] / 3.0) * 0.6 + \
            _clamp01((insider.get("usd") or 0) / 2_000_000.0) * 0.4
    if revision and revision.get("net_delta") is not None:
        parts["revision"] = _clamp01(revision["net_delta"] / 5.0)
    score = min(1.0, sum(parts.values())) if parts else 0.0
    return score, {k: round(v, 3) for k, v in parts.items()}


def _percentile_ranks(values: list[float]) -> list[float]:
    """Ordinal percentile [0,1] of each value within the list (ties share rank)."""
    n = len(values)
    if n <= 1:
        return [0.5] * n
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    for pos, i in enumerate(order):
        ranks[i] = pos / (n - 1)
    return ranks


def rank(candidates: list[Candidate], regime: str,
         universe_ret63: dict[str, float]) -> list[Candidate]:
    """Assign cross-sectional rel-strength, regime alignment, composite; sort desc.

    ``universe_ret63`` maps ticker->3-month return across the FULL scored universe
    so a candidate's relative strength is measured against all names, not just
    other candidates."""
    if not candidates:
        return []
    tickers = list(universe_ret63.keys())
    rets = [universe_ret63[t] for t in tickers]
    pct = dict(zip(tickers, _percentile_ranks(rets)))
    for c in candidates:
        c.rel = pct.get(c.ticker, 0.5)
        if c.direction == "BUY":
            c.regime = 1.0 if regime == "bull" else 0.4 if regime == "unknown" else 0.25
        else:
            c.regime = 1.0 if regime == "bear" else 0.4 if regime == "unknown" else 0.25
        w = ARCHETYPE_WEIGHTS[c.archetype]
        c.composite = 100.0 * _clamp01(
            w["primary"] * c.primary + w["rel"] * c.rel +
            w["regime"] * c.regime + w["context"] * c.context)
    candidates.sort(key=lambda c: c.composite, reverse=True)
    return candidates

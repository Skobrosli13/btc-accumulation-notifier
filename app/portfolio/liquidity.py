"""Execution liquidity caps for the LIVE paper-broker track (§7) — pure.

The deterministic replay book assumes frictionless next-open fills. The live
Alpaca paper track applies the two pre-registered §7 execution constants the
replay never did:

  - **ADV participation cap**: never order more than ``ADV_PARTICIPATION_CAP`` of
    a name's average daily *dollar* volume, so the paper account can't pretend to
    fill size a real book couldn't.
  - **Marketable-limit cap**: only ever cross with a limit at ``ref ± LIMIT_CAP_BPS``,
    so a runaway fill can't be booked at a price the tape never offered.

"Never size what you can't price": too few volume bars to estimate ADV ⇒ the cap
is ``None`` ⇒ the caller records no order (an honest skip), never an uncapped one.

Bars are oldest→newest dicts carrying ``close`` and ``volume`` (the lake's
adjusted book bars). ADV uses ``close × volume``; over a ≤20-session lookback a
split is rare, so adjusted-close turnover is a close proxy for raw dollar volume
(a stricter caller may pass ``closeunadj``-based bars).
"""
from __future__ import annotations

ADV_LOOKBACK = 20
ADV_PARTICIPATION_CAP = 0.01     # <= 1% of average daily dollar volume
LIMIT_CAP_BPS = 10.0             # marketable limit at ref +/- 10 bps


def adv_dollar(bars: list[dict], *, lookback: int = ADV_LOOKBACK) -> float | None:
    """Average daily dollar volume over the last ``lookback`` sessions, or None
    if fewer than ``lookback`` priced+volumed sessions exist (can't estimate)."""
    vals = [b["close"] * b["volume"]
            for b in bars[-lookback:]
            if b.get("close") and b.get("volume")]
    if len(vals) < lookback:
        return None
    return sum(vals) / len(vals)


def adv_share_cap(bars: list[dict], *, participation: float = ADV_PARTICIPATION_CAP,
                  lookback: int = ADV_LOOKBACK) -> float | None:
    """Max shares tradable = participation × ADV$ / latest close. None if ADV
    can't be estimated or the latest close is missing."""
    adv = adv_dollar(bars, lookback=lookback)
    if adv is None:
        return None
    last = bars[-1].get("close") if bars else None
    if not last:
        return None
    return participation * adv / last


def limit_price(ref_px: float, side: str, *, bps: float = LIMIT_CAP_BPS) -> float:
    """Marketable limit: a BUY crosses UP to ref×(1+bps), a SELL DOWN to
    ref×(1−bps) — the furthest we'll chase. A fill needing more than this is
    refused by the venue (recorded no_fill), which is the honest liquidity limit."""
    edge = bps / 10_000.0
    up = side.upper() in ("BUY", "LONG")
    return ref_px * (1 + edge) if up else ref_px * (1 - edge)


def cap_order_shares(desired_shares: float, bars: list[dict], *,
                     participation: float = ADV_PARTICIPATION_CAP,
                     lookback: int = ADV_LOOKBACK) -> tuple[float, str]:
    """Clamp ``desired_shares`` to the ADV participation ceiling.

    Returns ``(shares, reason)``:
      - ``(capped_shares, 'adv_capped')``  the ADV ceiling binds,
      - ``(desired_shares, '')``           it doesn't bind,
      - ``(0.0, 'adv_unpriced')``          ADV can't be estimated ⇒ skip the order.
    """
    cap = adv_share_cap(bars, participation=participation, lookback=lookback)
    if cap is None:
        return 0.0, "adv_unpriced"
    if desired_shares > cap:
        return cap, "adv_capped"
    return desired_shares, ""

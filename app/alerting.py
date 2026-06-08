"""Alert decisioning: tier transitions + the acute-capitulation flash.

Tier alerts fire only on a tier *change* (no repeat spam). The flash is
independent of tier and has its own debounce window. Both decisions are pure
functions of the inputs so they are easy to unit-test.
"""
from __future__ import annotations

from datetime import datetime

from . import scoring
from .config import Config

# Free-tier flash proxies (the spec's "sharp funding + OI flush proxy").
_FLASH_LIQ_BN = 1.0          # $bn 24h aggregate liquidations (paid signal)
_FLASH_FUNDING_NEG = -0.0003  # 8h funding fraction, persistently negative
_FLASH_OI_DROP_PCT = -10.0    # % OI change over window (deleveraging flush)

TIER_LABELS = {
    "NEUTRAL": "Neutral",
    "WATCH": "Watch",
    "ACCUMULATE": "Accumulate",
    "DEEP_VALUE": "Deep Value",
}
TIER_HEADLINES = {
    "WATCH": "indicators starting to align",
    "ACCUMULATE": "meaningful confluence - begin laddering",
    "DEEP_VALUE": "strongest confluence - heaviest tranches",
}
ALERT_TIERS = ("WATCH", "ACCUMULATE", "DEEP_VALUE")


def evaluate_flash(readings: dict, cfg: Config, *,
                   acute_funding: float | None = None,
                   acute_oi_chg_pct: float | None = None) -> bool:
    """True when an acute capitulation event is present (independent of tier).

    Requires ALL of: a capitulation signal (large liquidations, or the free
    funding/OI-flush proxy), Fear & Greed <= FLASH_FNG_MAX, and a price drop
    exceeding FLASH_DROP_PCT over 24-48h. Missing F&G or drop -> no flash
    (conservative).

    ``acute_funding`` / ``acute_oi_chg_pct`` are the *instantaneous* funding and
    ~1h OI change captured by the short-term collector (the ``derivs`` table).
    The long-term ``readings`` only carry a 7-day funding AVERAGE and a paid
    (Coinglass) OI flush — both of which a one-day washout barely moves — so on
    the free tier the flash would otherwise almost never fire. Feeding the fresh
    acute values in as additional capitulation legs makes the flash actually
    responsive without touching the scored readings.
    """
    fng = readings.get("fng")
    drop = readings.get("drop_24_48h_pct")
    if fng is None or drop is None:
        return False
    if fng > cfg.flash_fng_max:
        return False
    if drop < cfg.flash_drop_pct:
        return False

    liq = readings.get("liq_magnitude")
    funding = readings.get("funding")
    oi_flush = readings.get("oi_flush")
    capitulation = (
        (liq is not None and liq >= _FLASH_LIQ_BN)
        or (funding is not None and funding <= _FLASH_FUNDING_NEG)
        or (oi_flush is not None and oi_flush <= _FLASH_OI_DROP_PCT)
        or (acute_funding is not None and acute_funding <= _FLASH_FUNDING_NEG)
        or (acute_oi_chg_pct is not None and acute_oi_chg_pct <= _FLASH_OI_DROP_PCT)
    )
    return bool(capitulation)


def decide_alerts(current_tier: str, last_tier: str,
                  flash_now: bool, last_flash_at, debounce_days: int, now) -> dict:
    out = {"tier_alert": False, "flash_alert": False}
    if current_tier != last_tier and current_tier in ("WATCH", "ACCUMULATE", "DEEP_VALUE"):
        out["tier_alert"] = True
    if flash_now and (last_flash_at is None or (now - last_flash_at).days >= debounce_days):
        out["flash_alert"] = True
    return out


# --- Message building --------------------------------------------------------

def _data_tier_note(active_cats: list[str], onchain_active: bool) -> str:
    if onchain_active and "onchain" in active_cats:
        return "Data tier: on-chain layer ACTIVE (full signal)."
    return ("Data tier: running on free data only (on-chain valuation layer inactive - "
            "the highest-signal bottom layer). Confidence leans on price-structure, "
            "macro, and sentiment.")


def _common_lines(*, composite: float, tier: str, subscores: dict,
                  price_struct: dict, readings: dict, active_cats: list[str],
                  onchain_active: bool) -> list[str]:
    in_zone = scoring.indicators_in_zone(subscores)
    price = price_struct.get("price")
    wma200 = price_struct.get("wma200")
    p2w = price_struct.get("price_to_wma200")
    realized_ratio = readings.get("realized_ratio")

    lines = [f"Composite Accumulation Confidence: {composite:.1f}/100 ({TIER_LABELS.get(tier, tier)})"]
    if price is not None:
        lines.append(f"BTC price: ${price:,.0f}")
    if wma200 is not None and p2w is not None:
        rel = "below" if p2w <= 1 else "above"
        lines.append(f"vs 200-week MA: {p2w:.2f}x ({rel}, MA ${wma200:,.0f})")
    if realized_ratio is not None:
        rel = "below" if realized_ratio <= 1 else "above"
        lines.append(f"vs realized price: {realized_ratio:.2f}x ({rel})")
    if in_zone:
        lines.append("Indicators in their bottom zone: " + ", ".join(in_zone))
    else:
        lines.append("Indicators in their bottom zone: none yet")
    lines.append(_data_tier_note(active_cats, onchain_active))
    lines.append("Not financial advice - alert only. You decide whether, how much, and where to buy.")
    return lines


def build_tier_message(*, composite: float, tier: str, subscores: dict,
                       price_struct: dict, readings: dict, active_cats: list[str],
                       onchain_active: bool) -> tuple[str, str]:
    """Return (title, body) for a tier-transition alert."""
    label = TIER_LABELS.get(tier, tier)
    headline = TIER_HEADLINES.get(tier, "")
    title = f"BTC accumulation: {label} ({composite:.0f}/100)"
    body_lines = [f"Tier changed to {label.upper()} - {headline}.", ""]
    body_lines += _common_lines(composite=composite, tier=tier, subscores=subscores,
                                price_struct=price_struct, readings=readings,
                                active_cats=active_cats, onchain_active=onchain_active)
    return title, "\n".join(body_lines)


def build_flash_message(*, composite: float, tier: str, subscores: dict,
                        price_struct: dict, readings: dict, active_cats: list[str],
                        onchain_active: bool) -> tuple[str, str]:
    """Return (title, body) for an acute-capitulation flash alert."""
    drop = readings.get("drop_24_48h_pct")
    fng = readings.get("fng")
    title = "BTC capitulation flash - consider a tranche"
    head = "Acute capitulation event detected (independent of current tier)."
    detail = []
    if drop is not None:
        detail.append(f"price down {drop:.1f}% over 24-48h")
    if fng is not None:
        detail.append(f"Fear & Greed {fng:.0f}")
    if detail:
        head += " " + ", ".join(detail).capitalize() + "."
    body_lines = [head, ""]
    body_lines += _common_lines(composite=composite, tier=tier, subscores=subscores,
                                price_struct=price_struct, readings=readings,
                                active_cats=active_cats, onchain_active=onchain_active)
    return title, "\n".join(body_lines)


# --- Short-term swing alerts -------------------------------------------------

def is_counter_trend(direction: str, state: str) -> bool:
    """A trigger is counter-trend when it points against the regime bias — a BUY in
    a bearish state, or a SELL in a bullish state. Single source of truth, reused by
    the alert message builder and the dashboard API."""
    return ((direction == "BUY" and state in ("SELL", "STRONG_SELL"))
            or (direction == "SELL" and state in ("BUY", "STRONG_BUY")))


def decide_st_alert(*, candle_ts: int, last_alert: dict | None,
                    now: datetime, cooldown_hours: float) -> bool:
    """Whether a fired short-term trigger should actually alert.

    Pure function (mirrors decide_alerts). Suppresses if (a) we already alerted on
    THIS candle for this trigger, or (b) we are still inside the cooldown window.
    ``last_alert`` is ``store.last_st_alert(key, tf)`` -> {'ts', 'created_at'} or None.
    """
    if last_alert is None:
        return True
    if last_alert.get("ts") == candle_ts:          # same closed candle -> no repeat
        return False
    created = last_alert.get("created_at")
    if created:
        try:
            elapsed_h = (now - datetime.fromisoformat(created)).total_seconds() / 3600.0
            if elapsed_h < cooldown_hours:
                return False
        except ValueError:
            pass
    return True


def build_st_message(*, trigger, timeframe: str, score: float, state: str,
                     price: float | None, indicators: dict) -> tuple[str, str]:
    """Return (title, body) for a short-term swing alert. ``trigger`` is a
    shortterm.Trigger."""
    arrow = "[BUY]" if trigger.direction == "BUY" else "[SELL]"
    counter = is_counter_trend(trigger.direction, state)
    title = f"BTC swing {timeframe}: {trigger.label} ({trigger.direction})"
    lines = [f"{arrow} Short-term {trigger.direction} signal on the {timeframe} timeframe.",
             f"Trigger: {trigger.label}." + (f" {trigger.detail}" if trigger.detail else ""),
             ""]
    if price is not None:
        lines.append(f"Price (last closed {timeframe}): ${price:,.0f}")
    # The score is the regime/momentum BIAS (context), not the signal itself —
    # the trigger above is the actionable event.
    lines.append(f"Regime bias (context): {score:+.0f}/100 ({state})")
    if counter:
        lines.append(f"[!] Counter-trend: this {trigger.direction} fires against a "
                     f"{'bearish' if trigger.direction == 'BUY' else 'bullish'} regime "
                     "- treat as lower-confidence / fade risk.")

    rsi = indicators.get("rsi")
    if isinstance(rsi, (list, tuple)) and rsi and rsi[0] is not None:
        lines.append(f"RSI(14): {rsi[0]:.0f}")
    atr_pct = indicators.get("atr_pct")
    if atr_pct is not None:
        lines.append(f"ATR: {atr_pct:.1f}% (volatility)")

    lines.append("")
    lines.append("Short-term swing timing - separate from the long-term accumulation thesis.")
    lines.append("Not financial advice - alert only. You decide whether, how much, and where to trade.")
    return title, "\n".join(lines)

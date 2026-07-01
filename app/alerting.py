"""Alert decisioning: tier transitions + the acute-capitulation flash.

Tier alerts fire only on a tier *change* (no repeat spam). The flash is
independent of tier and has its own debounce window. Both decisions are pure
functions of the inputs so they are easy to unit-test.
"""
from __future__ import annotations

from datetime import datetime

from . import scoring, shortterm
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


def decide_alerts(current_tier: str, prev_notified_tier: str,
                  flash_now: bool, last_flash_at, debounce_days: int, now,
                  *, prev_active_cats: list[str] | None = None,
                  active_cats: list[str] | None = None) -> dict:
    """Decide which alerts to fire this run.

    ``prev_notified_tier`` is the tier we last SUCCESSFULLY communicated (NOT the
    last computed tier): comparing against it means a failed send is retried next
    run instead of being swallowed by an advanced display tier, and a genuine
    re-entry (NEUTRAL→WATCH→NEUTRAL→WATCH) still re-alerts each time.

    * ``tier_alert``  — entered/changed into an alert tier (WATCH/ACCUMULATE/DEEP_VALUE).
    * ``exit_alert``  — dropped back to NEUTRAL from an alert tier (a low-key "zone
                        closed" note so a laddering user learns the window ended).
    * ``flash_alert`` — acute capitulation, independent of tier, own debounce.
    * ``cats_changed``— the scored category set differs from the previous run, so a
                        tier change may be a renormalization artifact, not a market
                        move; the message builder caveats this.
    """
    out = {"tier_alert": False, "flash_alert": False, "exit_alert": False,
           "cats_changed": False}
    if current_tier != prev_notified_tier:
        if current_tier in ALERT_TIERS:
            out["tier_alert"] = True
        elif current_tier == "NEUTRAL" and prev_notified_tier in ALERT_TIERS:
            out["exit_alert"] = True
    if (out["tier_alert"] or out["exit_alert"]) and \
            prev_active_cats is not None and active_cats is not None:
        out["cats_changed"] = set(prev_active_cats) != set(active_cats)
    if flash_now and (last_flash_at is None or (now - last_flash_at).days >= debounce_days):
        out["flash_alert"] = True
    return out


# --- Sell-side overheat (froth) alert ------------------------------------------

FROTH_ALERT_BANDS = ("FROTHY", "OVERHEATED")
_FROTH_ORDER = ["COOL", "WARMING", "FROTHY", "OVERHEATED"]


def decide_froth_alert(band: str | None, prev_notified_band: str | None) -> bool:
    """Owner-only sell-side alert: fire when the overheat band crosses UP into
    FROTHY/OVERHEATED relative to the last band we successfully communicated.
    The cursor tracks every computed band (even quiet ones), so falling back
    below FROTHY re-arms the alert for the next run-up. Pure function."""
    if band not in FROTH_ALERT_BANDS:
        return False
    prev_i = (_FROTH_ORDER.index(prev_notified_band)
              if prev_notified_band in _FROTH_ORDER else 0)
    return _FROTH_ORDER.index(band) > prev_i


def next_froth_cursor(band: str | None, prev_notified: str | None,
                      alert_fired: bool, send_ok: bool) -> str | None:
    """Advance the notified-froth-band cursor. Pure function.

    Semantics: hold on a failed send (retry next run); advance upward freely;
    fall DOWN only on a full cool-down to COOL. The sticky downgrade is the
    oscillation debounce — without it a score wobbling across a band floor
    (e.g. 47<->53 around FROTHY's hysteresis window) would re-email on every
    re-entry; requiring a genuine cool-down first caps it at one email per
    excursion. A None band (no froth data) holds the cursor."""
    if band is None:
        return prev_notified
    if alert_fired and not send_ok:
        return prev_notified
    if (prev_notified in _FROTH_ORDER
            and _FROTH_ORDER.index(band) < _FROTH_ORDER.index(prev_notified)
            and band != "COOL"):
        return prev_notified
    return band


def build_froth_message(*, froth: dict, band: str, price: float | None,
                        composite: float, tier: str) -> tuple[str, str]:
    """Return (title, body) for an overheat band-crossing alert (owner-only)."""
    score = froth.get("score") or 0.0
    title = f"BTC overheat: {band} ({score:.0f}/100) - consider reviewing"
    lines = [f"Sell-side overheat crossed into {band}.", "",
             f"Overheat score: {score:.0f}/100 ({band})"]
    if price is not None:
        lines.append(f"BTC price: ${price:,.0f}")
    lines.append(f"Long-term accumulation read (context): {composite:.0f}/100 "
                 f"({TIER_LABELS.get(tier, tier)})")
    lit = froth.get("in_zone") or []
    lines.append("Top signals lit: " + (", ".join(lit) if lit else "none"))
    lines += [
        "",
        "Heuristic top-signal: thresholds anchored to the 2017/2021/2025 cycle tops "
        "(1-3 cycles per indicator) and tuned IN-SAMPLE on those same tops (no "
        "out-of-sample holdout) - a small circular sample, not a proven edge. This is "
        "the sell-side mirror of the accumulation score: consider reviewing/trimming "
        "into strength, sized to your own plan.",
        "Not financial advice - alert only. You decide whether, how much, and where to sell.",
    ]
    return title, "\n".join(lines)


# --- Message building --------------------------------------------------------

def _data_tier_note(active_cats: list[str], onchain_active: bool) -> str:
    if onchain_active and "onchain" in active_cats:
        return ("Data tier: on-chain valuation ACTIVE (free BGeometrics feed). "
                "Only real liquidation-cascade / order-flow remains a paid gap.")
    return ("Data tier: on-chain valuation layer inactive this run (free feed "
            "unreachable - the highest-signal bottom layer). Confidence leans on "
            "price-structure, macro, and sentiment.")


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


_CATS_CHANGED_CAVEAT = (
    "[!] Note: the available data categories changed since the last run, so this "
    "tier move may partly reflect a category dropping out / coming back (the "
    "composite renormalizes over whatever returned data) rather than a pure market "
    "move. Confirm against the indicator breakdown before acting."
)

_DEGRADED_CAVEAT = (
    "[!] Note: the on-chain category (the heaviest weight) returned no data this "
    "run, so the composite is renormalized over the remaining categories — that "
    "alone can shift it 10+ points, several times the tier hysteresis margin. A "
    "tier move on a degraded run can be a data-outage artifact, not a market "
    "move; confirm against the indicator breakdown before acting."
)


def build_tier_message(*, composite: float, tier: str, subscores: dict,
                       price_struct: dict, readings: dict, active_cats: list[str],
                       onchain_active: bool, changed: dict | None = None,
                       what_to_do: dict | None = None, plan: dict | None = None,
                       cats_changed: bool = False, degraded: bool = False) -> tuple[str, str]:
    """Return (title, body) for a tier-transition alert. ``degraded`` marks a run
    whose on-chain category returned no data (see scoring.composite_degraded)."""
    label = TIER_LABELS.get(tier, tier)
    headline = TIER_HEADLINES.get(tier, "")
    title = f"BTC accumulation: {label} ({composite:.0f}/100)"
    body_lines = [f"Tier changed to {label.upper()} - {headline}.", ""]
    body_lines += _common_lines(composite=composite, tier=tier, subscores=subscores,
                                price_struct=price_struct, readings=readings,
                                active_cats=active_cats, onchain_active=onchain_active)
    if cats_changed:
        body_lines += ["", _CATS_CHANGED_CAVEAT]
    if degraded:
        body_lines += ["", _DEGRADED_CAVEAT]
    pb = _playbook_lines(changed, what_to_do, plan)
    if pb:
        body_lines += ["", *pb]
    return title, "\n".join(body_lines)


def build_exit_message(*, composite: float, tier: str, subscores: dict,
                       price_struct: dict, readings: dict, active_cats: list[str],
                       onchain_active: bool, prev_tier: str = "",
                       changed: dict | None = None, what_to_do: dict | None = None,
                       plan: dict | None = None, cats_changed: bool = False,
                       degraded: bool = False) -> tuple[str, str]:
    """Return (title, body) for a zone-exit note (alert tier -> NEUTRAL).

    A low-key bookend to the tier alerts: a user laddering on the last ACCUMULATE
    email is otherwise never told the accumulation window closed.
    """
    prev_label = TIER_LABELS.get(prev_tier, prev_tier) if prev_tier else "the accumulation zone"
    title = f"BTC accumulation: zone exited ({composite:.0f}/100)"
    body_lines = [
        f"Accumulation zone CLOSED - dropped from {prev_label.upper()} back to NEUTRAL.",
        "The confluence that opened the zone has faded; no new laddering signal. "
        "Existing positions are your call - this is the long-term thesis cooling, "
        "not a sell signal.",
        "",
    ]
    body_lines += _common_lines(composite=composite, tier=tier, subscores=subscores,
                                price_struct=price_struct, readings=readings,
                                active_cats=active_cats, onchain_active=onchain_active)
    if cats_changed:
        body_lines += ["", _CATS_CHANGED_CAVEAT]
    if degraded:
        body_lines += ["", _DEGRADED_CAVEAT]
    pb = _playbook_lines(changed, what_to_do, plan)
    if pb:
        body_lines += ["", *pb]
    return title, "\n".join(body_lines)


def build_flash_message(*, composite: float, tier: str, subscores: dict,
                        price_struct: dict, readings: dict, active_cats: list[str],
                        onchain_active: bool, changed: dict | None = None,
                        what_to_do: dict | None = None, plan: dict | None = None) -> tuple[str, str]:
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
    pb = _playbook_lines(changed, what_to_do, plan)
    if pb:
        body_lines += ["", *pb]
    return title, "\n".join(body_lines)


# --- Short-term swing alerts -------------------------------------------------

# Applies to EVERY swing trigger, not just order-flow: the committed calibration
# (app/st_winrates.json) measures the ALERTED (post-confluence) population as
# statistically indistinguishable from the base rate — the email must not imply
# a conviction the system's own numbers refute.
_ST_NO_EDGE_LINE = ("Swing triggers overall: measured ~ coin-flip vs the base rate "
                    "on the alerted population (no demonstrated edge) - timing "
                    "context, not conviction.")
_FLOW_NOTE = ("Order-flow read (CVD / OI / liquidation): FORWARD-TEST layer - "
              "backtested ~ coin-flip over its short history, no live forward-test "
              "verdict yet; timing context only.")
_UNVALIDATED_NOTE = ("Funding/OI trigger: unvalidated - no backtest coverage "
                     "(not replayable from candle history alone).")

def diff_since(prev: dict | None, cur: dict) -> dict | None:
    """'What changed' between the last alerted run and now. prev/cur each carry
    {composite, tier, subscores}. None when there's no prior alert to diff against."""
    if not prev:
        return None
    prev_zone = set(scoring.indicators_in_zone(prev.get("subscores") or {}))
    cur_zone = set(scoring.indicators_in_zone(cur.get("subscores") or {}))
    return {
        "composite_delta": round((cur.get("composite") or 0.0) - (prev.get("composite") or 0.0), 1),
        "tier_from": prev.get("tier"),
        "tier_to": cur.get("tier"),
        "newly_in_zone": sorted(cur_zone - prev_zone),
        "dropped_out": sorted(prev_zone - cur_zone),
        "since": prev.get("run_ts"),
    }


def _playbook_lines(changed: dict | None, what_to_do: dict | None,
                    plan: dict | None) -> list[str]:
    """Render the playbook blocks for an email body (omitted sections stay quiet)."""
    lines: list[str] = []
    if changed:
        d = changed["composite_delta"]
        parts = [f"What changed: composite {d:+.1f}"]
        if changed["tier_from"] != changed["tier_to"]:
            parts.append(f"tier {changed['tier_from']}→{changed['tier_to']}")
        if changed["newly_in_zone"]:
            parts.append("new in-zone: " + ", ".join(changed["newly_in_zone"]))
        if changed["dropped_out"]:
            parts.append("left zone: " + ", ".join(changed["dropped_out"]))
        lines.append(". ".join(parts) + ".")
    if what_to_do:
        lines.append(f"What to do now: {what_to_do['stance']} — {what_to_do['suggested_action']} "
                     f"({what_to_do['rationale']})")
    if plan and plan.get("tranches"):
        ladder = "; ".join(f"{t['label']} ~{t['pct']:.0f}%"
                           + (f" @ ${t['price']:,.0f}" if t['label'] != 'now' else "")
                           for t in plan["tranches"])
        lines.append(f"Illustrative ladder ({plan['deploy_now_pct']:.0f}% now): {ladder}")
    return lines


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
                     price: float | None, indicators: dict,
                     regime: str = "unknown") -> tuple[str, str]:
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
    aligned = shortterm.regime_aligned(trigger.direction, regime)
    if aligned is not None:
        lines.append(f"200-day regime: {regime} — this {trigger.direction} is "
                     f"{'with' if aligned else 'against'} the macro trend.")

    rsi = indicators.get("rsi")
    if isinstance(rsi, (list, tuple)) and rsi and rsi[0] is not None:
        lines.append(f"RSI(14): {rsi[0]:.0f}")
    atr_pct = indicators.get("atr_pct")
    if atr_pct is not None:
        lines.append(f"ATR: {atr_pct:.1f}% (volatility)")
    lv = shortterm.trade_levels(trigger.direction, price, indicators.get("atr"))
    if lv:
        lines.append(f"ATR risk frame (illustrative): stop ${lv['stop']:,.0f} / "
                     f"target ${lv['target']:,.0f}" + (f" (~{lv['rr']}R)" if lv["rr"] else ""))

    lines.append("")
    from .flow import FLOW_TRIGGER_KEYS
    if trigger.key in FLOW_TRIGGER_KEYS:
        lines.append(_FLOW_NOTE)
    elif trigger.key in shortterm.UNVALIDATED_TRIGGER_KEYS:
        lines.append(_UNVALIDATED_NOTE)
    lines.append(_ST_NO_EDGE_LINE)
    lines.append("Short-term swing timing - separate from the long-term accumulation thesis.")
    lines.append("Not financial advice - alert only. You decide whether, how much, and where to trade.")
    return title, "\n".join(lines)


def build_st_batch_message(items: list[dict], direction: str) -> tuple[str, str]:
    """Return (title, body) for ALL same-direction triggers fired this run.

    ``items`` is a list of {trigger, timeframe, score, state, price, indicators,
    regime}. Batching into one email per direction kills the duplicate-email
    fatigue from the confluence gate (which guarantees >=2 triggers) and from the
    same event firing on both 4h and 1d. A single email below 4-6 near-identical
    ones is what keeps the rare long-term alert from being trained-out.
    """
    if len(items) == 1:
        it = items[0]
        return build_st_message(
            trigger=it["trigger"], timeframe=it["timeframe"], score=it["score"],
            state=it["state"], price=it["price"], indicators=it["indicators"],
            regime=it.get("regime", "unknown"))

    arrow = "[BUY]" if direction == "BUY" else "[SELL]"
    # Count triggers per timeframe for the subject, e.g. "2 on 4h, 1 on 1d".
    by_tf: dict[str, int] = {}
    for it in items:
        by_tf[it["timeframe"]] = by_tf.get(it["timeframe"], 0) + 1
    tf_summary = ", ".join(f"{n} on {tf}" for tf, n in by_tf.items())
    title = f"BTC swing {direction}: {len(items)} triggers ({tf_summary})"

    lines = [f"{arrow} {len(items)} short-term {direction} triggers fired this run ({tf_summary}).",
             "Batched into one email because several triggers agreed — the agreement is "
             "a noise/spam filter, not evidence of edge (the alerted population still "
             "measures ~ coin-flip).",
             ""]
    for it in items:
        trig = it["trigger"]
        tf = it["timeframe"]
        price = it["price"]
        counter = is_counter_trend(direction, it["state"])
        flag = " [counter-trend]" if counter else ""
        head = f"• {tf}: {trig.label}{flag}"
        if price is not None:
            head += f" @ ${price:,.0f}"
        lines.append(head)
        if trig.detail:
            lines.append(f"    {trig.detail}")
        lv = shortterm.trade_levels(direction, price, (it["indicators"] or {}).get("atr"))
        if lv:
            lines.append(f"    risk frame: stop ${lv['stop']:,.0f} / target ${lv['target']:,.0f}"
                         + (f" (~{lv['rr']}R)" if lv["rr"] else ""))
    lines.append("")
    from .flow import FLOW_TRIGGER_KEYS
    if any(it["trigger"].key in FLOW_TRIGGER_KEYS for it in items):
        lines.append(_FLOW_NOTE)
    if any(it["trigger"].key in shortterm.UNVALIDATED_TRIGGER_KEYS for it in items):
        lines.append(_UNVALIDATED_NOTE)
    lines.append(_ST_NO_EDGE_LINE)
    lines.append("Short-term swing timing - separate from the long-term accumulation thesis.")
    lines.append("Not financial advice - alert only. You decide whether, how much, and where to trade.")
    return title, "\n".join(lines)

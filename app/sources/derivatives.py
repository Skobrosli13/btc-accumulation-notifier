"""Derivatives — richer layer (PAID drop-in — Coinglass).

Adds, on top of the free Binance funding proxy:
  liq_magnitude : 24h aggregate liquidations ($bn) — large = capitulation flush
  oi_flush      : recent % change in open interest — sharp drop = deleveraging
  funding       : OI-weighted funding (overrides the single-venue free proxy)

Returns all-None when COINGLASS_API_KEY is absent (category falls back to the
free funding-only sub-score, or renormalizes away entirely). Coinglass endpoint
shapes vary across plan tiers/versions, so every extraction degrades to None.

Field-name note: the base URL is the v4 API (open-api-v4.coinglass.com). v4
aggregated-history rows use snake_case (e.g. ``aggregated_long_liquidation_usd``
/ ``long_liquidation_usd``), whereas older versions used camelCase
(``longLiquidationUsd``). We accept BOTH so a keyed layer doesn't go silently
dark. v4 also wraps errors in a 200-OK envelope, so we check ``code``/``success``
before trusting ``data``.
"""
from __future__ import annotations

import logging

from ._http import get_json

log = logging.getLogger(__name__)

COINGLASS_BASE = "https://open-api-v4.coinglass.com/api"

_NONE_READINGS = {"liq_magnitude": None, "oi_flush": None, "oi_flush_unit": None}

# Timestamp field names across Coinglass versions. Row ORDER varies like field
# names do, so history rows are sorted by timestamp instead of trusting the
# response order; a row with NO timestamp can't be order-verified and is unusable.
_TS_FIELDS = ("time", "t", "ts", "timestamp", "createTime")


def _hdr(api_key: str) -> dict:
    # Coinglass has used both header names across versions; send both.
    return {"coinglassSecret": api_key, "CG-API-KEY": api_key}


def _envelope_ok(data) -> bool:
    """v4 wraps failures in 200-OK JSON. Treat the response as usable only when it
    doesn't explicitly signal an error.

    Documented success markers: ``{"code": "0", ...}`` (string or int 0) and/or
    ``{"success": true}``. We're permissive: if neither field is present we don't
    reject (some endpoints omit them), but an explicit non-zero code / success
    false is a hard fail.
    """
    if not isinstance(data, dict):
        return False
    if data.get("success") is False:
        return False
    code = data.get("code")
    if code is not None:
        # Accept "0"/0 as success; anything else is an error envelope.
        return str(code) in ("0", "success", "ok")
    return True


def _first_field(row: dict, *names: str):
    """Return the first present, non-None field among ``names`` (case variants)."""
    for n in names:
        if n in row and row[n] is not None:
            return row[n]
    return None


def _row_ts(row: dict) -> int | None:
    """Epoch timestamp of a history row, or None when no usable field exists."""
    v = _first_field(row, *_TS_FIELDS)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _liquidations_24h_bn(api_key: str) -> float | None:
    data = get_json(f"{COINGLASS_BASE}/futures/liquidation/aggregated-history",
                    params={"symbol": "BTC", "interval": "1d", "limit": 1},
                    headers=_hdr(api_key))
    if not data or not _envelope_ok(data):
        return None
    try:
        rows = data.get("data") or []
        if not isinstance(rows, list) or not rows:
            return None
        # Latest bar BY TIMESTAMP, not by position — a newest-first response
        # would otherwise hand back the oldest bar.
        stamped = [r for r in rows if isinstance(r, dict) and _row_ts(r) is not None]
        if not stamped:
            return None
        last = max(stamped, key=_row_ts)
        # Accept v4 snake_case (aggregated_* and plain) AND legacy camelCase.
        longs = _first_field(last,
                             "aggregated_long_liquidation_usd",
                             "long_liquidation_usd",
                             "longLiquidationUsd")
        shorts = _first_field(last,
                              "aggregated_short_liquidation_usd",
                              "short_liquidation_usd",
                              "shortLiquidationUsd")
        total = float(longs or 0) + float(shorts or 0)
        return total / 1e9 if total else None
    except (KeyError, TypeError, ValueError):
        return None


def _oi_change_pct(api_key: str) -> float | None:
    # Match the FREE oi_flush (run_once's OKX-derived path) in BOTH window and
    # denomination so adding a paid key does NOT silently redefine the indicator
    # against the same -25% extreme threshold:
    #   * window: limit=2 on the 1d interval => change between the last two daily
    #     closes ~ a 24h window. (We avoid pulling oi_flush_window_hours from
    #     config here because the daily granularity can't honor arbitrary sub-day
    #     windows anyway; 24h is the closest faithful match.)
    #   * denomination: the free path reads OKX OI in CONTRACTS (coin terms), so
    #     request unit=coin here. A USD OI series embeds the price move itself —
    #     a -20% crash drops USD OI ~20% with ZERO deleveraging, double-counting
    #     what drop_24_48h_pct already measures. Explicitly USD-named fields are
    #     rejected (None) rather than mis-scored in the wrong denomination.
    data = get_json(f"{COINGLASS_BASE}/futures/open-interest/aggregated-history",
                    params={"symbol": "BTC", "interval": "1d", "limit": 2,
                            "unit": "coin"},
                    headers=_hdr(api_key))
    if not data or not _envelope_ok(data):
        return None
    try:
        rows = data.get("data") or []
        if not isinstance(rows, list):
            return None
        stamped: list[tuple[int, float]] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            ts = _row_ts(r)
            if ts is None:
                continue  # order-unverifiable row: unusable, not order-trusted
            # v4 close field varies: close / open_interest / openInterest
            # (legacy). close_usd / open_interest_usd are NOT accepted — wrong
            # denomination for this indicator (see above).
            v = _first_field(r, "close", "open_interest", "openInterest")
            if v is None:
                continue
            try:
                stamped.append((ts, float(v)))
            except (TypeError, ValueError):
                continue
        stamped.sort(key=lambda p: p[0])  # oldest-first regardless of response order
        if len(stamped) < 2 or stamped[0][1] == 0:
            return None
        return (stamped[-1][1] / stamped[0][1] - 1.0) * 100.0  # % change over the window
    except (KeyError, TypeError, ValueError):
        return None


def derivatives() -> dict:
    """Paid derivatives readings; all-None when no Coinglass key is set."""
    from ..config import load_config

    cfg = load_config()
    if not cfg.coinglass_api_key:
        log.info("No Coinglass key set; paid derivatives readings skipped")
        return dict(_NONE_READINGS)

    try:
        out = {
            "liq_magnitude": _liquidations_24h_bn(cfg.coinglass_api_key),
            "oi_flush": _oi_change_pct(cfg.coinglass_api_key),
        }
        # Record the denomination so downstream display can label the number.
        # The free OKX-contracts fallback in run_once is coin-denominated by
        # construction; _oi_change_pct requests unit=coin, so both paths match.
        out["oi_flush_unit"] = "coin" if out["oi_flush"] is not None else None
    except Exception as exc:  # noqa: BLE001
        log.warning("Coinglass fetch failed (%s); paid derivatives skipped", exc)
        return dict(_NONE_READINGS)

    # A KEYED layer that returns nothing usable is a silently-dark paid feed
    # (almost always a field-name/plan-tier mismatch). Surface it at WARNING so
    # it shows up in logs instead of looking like a normal fail-soft skip.
    if all(v is None for v in out.values()):
        log.warning("Coinglass key set but returned no usable derivatives "
                    "(check plan tier / response field names); layer dark this run")
    return out

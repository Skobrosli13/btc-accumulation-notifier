"""Derivatives — richer layer (PAID drop-in — Coinglass).

Adds, on top of the free Binance funding proxy:
  liq_magnitude : 24h aggregate liquidations ($bn) — large = capitulation flush
  oi_flush      : recent % change in open interest — sharp drop = deleveraging
  funding       : OI-weighted funding (overrides the single-venue free proxy)

Returns all-None when COINGLASS_API_KEY is absent (category falls back to the
free funding-only sub-score, or renormalizes away entirely). Coinglass endpoint
shapes vary across plan tiers/versions, so every extraction degrades to None.
"""
from __future__ import annotations

import logging

from ._http import get_json

log = logging.getLogger(__name__)

COINGLASS_BASE = "https://open-api-v4.coinglass.com/api"

_NONE_READINGS = {"liq_magnitude": None, "oi_flush": None}


def _hdr(api_key: str) -> dict:
    # Coinglass has used both header names across versions; send both.
    return {"coinglassSecret": api_key, "CG-API-KEY": api_key}


def _liquidations_24h_bn(api_key: str) -> float | None:
    data = get_json(f"{COINGLASS_BASE}/futures/liquidation/aggregated-history",
                    params={"symbol": "BTC", "interval": "1d", "limit": 1},
                    headers=_hdr(api_key))
    if not data:
        return None
    try:
        rows = data.get("data") or []
        if not rows:
            return None
        last = rows[-1]
        total = (float(last.get("longLiquidationUsd", 0) or 0)
                 + float(last.get("shortLiquidationUsd", 0) or 0))
        return total / 1e9 if total else None
    except (KeyError, TypeError, ValueError):
        return None


def _oi_change_pct(api_key: str) -> float | None:
    data = get_json(f"{COINGLASS_BASE}/futures/open-interest/aggregated-history",
                    params={"symbol": "BTC", "interval": "1d", "limit": 8},
                    headers=_hdr(api_key))
    if not data:
        return None
    try:
        rows = data.get("data") or []
        vals = [float(r.get("close", r.get("openInterest"))) for r in rows
                if r.get("close", r.get("openInterest")) is not None]
        if len(vals) < 2 or vals[0] == 0:
            return None
        return (vals[-1] / vals[0] - 1.0) * 100.0  # % change over the window
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
        return {
            "liq_magnitude": _liquidations_24h_bn(cfg.coinglass_api_key),
            "oi_flush": _oi_change_pct(cfg.coinglass_api_key),
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("Coinglass fetch failed (%s); paid derivatives skipped", exc)
        return dict(_NONE_READINGS)

"""On-chain valuation (PAID drop-in — Glassnode, CryptoQuant alternative).

This is the highest-signal layer for cycle bottoms but requires a paid key.
Returns all-None (and the whole category renormalizes away) when no key is set.

Glassnode metrics used (header ``X-Api-Key``, params ``a=BTC&i=24h``):
  market/mvrv_z_score                     -> mvrv_z
  market/price_realized_usd               -> realized price (-> realized_ratio = price/realized)
  indicators/net_unrealized_profit_loss   -> nupl
  indicators/sopr                         -> sopr (averaged over 7d)
  indicators/puell_multiple               -> puell
"""
from __future__ import annotations

import logging

from ._http import get_json

log = logging.getLogger(__name__)

GLASSNODE_BASE = "https://api.glassnode.com/v1/metrics"

_NONE_READINGS = {"mvrv_z": None, "realized_ratio": None, "nupl": None,
                  "sopr": None, "puell": None}


def _gn_series(path: str, api_key: str) -> list[float]:
    """Fetch a Glassnode metric as a list of float values (oldest->newest)."""
    data = get_json(f"{GLASSNODE_BASE}/{path}",
                    params={"a": "BTC", "i": "24h"},
                    headers={"X-Api-Key": api_key})
    if not data:
        return []
    out: list[float] = []
    for row in data:
        v = row.get("v")
        if v is not None:
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                continue
    return out


def _gn_latest(path: str, api_key: str) -> float | None:
    vals = _gn_series(path, api_key)
    return vals[-1] if vals else None


def _from_glassnode(api_key: str, price: float | None) -> dict:
    realized = _gn_latest("market/price_realized_usd", api_key)
    realized_ratio = (price / realized) if (price and realized) else None

    sopr_series = _gn_series("indicators/sopr", api_key)
    sopr_7d = (sum(sopr_series[-7:]) / len(sopr_series[-7:])) if sopr_series else None

    return {
        "mvrv_z": _gn_latest("market/mvrv_z_score", api_key),
        "realized_ratio": realized_ratio,
        "nupl": _gn_latest("indicators/net_unrealized_profit_loss", api_key),
        "sopr": sopr_7d,
        "puell": _gn_latest("indicators/puell_multiple", api_key),
    }


def onchain(price: float | None = None) -> dict:
    """On-chain readings keyed for the scorer; all-None when no paid key is set.

    ``price`` is the current spot price (from the price source) needed to turn
    realized price into the realized-price ratio.
    """
    from ..config import load_config

    cfg = load_config()
    if cfg.glassnode_api_key:
        try:
            return _from_glassnode(cfg.glassnode_api_key, price)
        except Exception as exc:  # noqa: BLE001
            log.warning("Glassnode fetch failed (%s); on-chain layer skipped", exc)
            return dict(_NONE_READINGS)

    if cfg.cryptoquant_api_key:
        # CryptoQuant is an equivalent provider; wire its endpoints here if used.
        log.info("CRYPTOQUANT_API_KEY set but the CryptoQuant adapter is not wired; "
                 "on-chain layer skipped (set GLASSNODE_API_KEY instead)")
        return dict(_NONE_READINGS)

    log.info("No on-chain key set; on-chain valuation layer skipped")
    return dict(_NONE_READINGS)

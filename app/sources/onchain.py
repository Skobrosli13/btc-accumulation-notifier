"""On-chain valuation.

The highest-signal layer for cycle bottoms. Provider precedence:
  1. Glassnode (paid, ``GLASSNODE_API_KEY``) — richest, 7d-smoothed SOPR.
  2. bitcoin-data.com / BGeometrics (FREE, no key) — the default; lights the
     layer up end-to-end on the free tier. Disable with ``ONCHAIN_FREE=false``.
Each metric fails soft to None and the whole category renormalizes away if the
chosen provider is unreachable.

Glassnode metrics used (header ``X-Api-Key``, params ``a=BTC&i=24h``):
  market/mvrv_z_score                     -> mvrv_z
  market/price_realized_usd               -> realized price (-> realized_ratio = price/realized)
  indicators/net_unrealized_profit_loss   -> nupl
  indicators/sopr                         -> sopr (averaged over 7d)
  indicators/puell_multiple               -> puell

bitcoin-data.com endpoints (GET ``/v1/<metric>/last`` -> ``{"d":..,"<field>":num}``):
  mvrv-zscore -> mvrvZscore | nupl -> nupl | sopr -> sopr |
  puell-multiple -> puellMultiple | realized-price -> realizedPrice
"""
from __future__ import annotations

import logging

from ._http import get_json

log = logging.getLogger(__name__)

GLASSNODE_BASE = "https://api.glassnode.com/v1/metrics"
BITCOIN_DATA_BASE = "https://bitcoin-data.com/v1"

_NONE_READINGS = {"mvrv_z": None, "realized_ratio": None, "nupl": None,
                  "sopr": None, "puell": None}

# Free provider: scorer key -> (endpoint slug, JSON field) for the point metrics.
_BD_METRICS = {
    "mvrv_z": ("mvrv-zscore", "mvrvZscore"),
    "nupl":   ("nupl", "nupl"),
    "sopr":   ("sopr", "sopr"),
    "puell":  ("puell-multiple", "puellMultiple"),
}

# Context-only metrics (shown, NOT scored — only ~1 cycle of free history to
# threshold against, so they inform rather than move the composite).
_BD_CONTEXT = {
    "reserve_risk": ("reserve-risk", "reserveRisk"),
    "rhodl":        ("rhodl-ratio", "rhodlRatio"),
}


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


def _bd_last(slug: str, field: str) -> float | None:
    """Latest value of one bitcoin-data.com metric, or None on any failure."""
    data = get_json(f"{BITCOIN_DATA_BASE}/{slug}/last")
    if not data:
        return None
    try:
        v = data.get(field)
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _from_bitcoin_data(price: float | None) -> dict:
    """Free on-chain readings (bitcoin-data.com / BGeometrics) keyed for the scorer.

    No API key. Each metric fails soft to None independently; ``realized_ratio``
    needs the current spot price (passed in) and is None if either is missing.
    """
    out = {key: _bd_last(slug, field) for key, (slug, field) in _BD_METRICS.items()}
    realized_price = _bd_last("realized-price", "realizedPrice")
    out["realized_ratio"] = (price / realized_price) if (price and realized_price) else None
    # Context-only metrics (not scored).
    for key, (slug, field) in _BD_CONTEXT.items():
        out[key] = _bd_last(slug, field)
    return out


def history(slug: str) -> list[dict]:
    """Full daily series for a bitcoin-data.com metric (OFFLINE calibration only).

    GET /v1/<slug> (no /last) -> [{"d","unixTs","<field>":v}, ...]. Returns [] on
    failure. Rate-limited (~10 req/hr) — NEVER call from the live run/collect/api
    paths; only scripts/calibrate.py uses this.
    """
    data = get_json(f"{BITCOIN_DATA_BASE}/{slug}")
    return data if isinstance(data, list) else []


def onchain(price: float | None = None) -> dict:
    """On-chain readings keyed for the scorer.

    Free by default (bitcoin-data.com); Glassnode if a key is set. All-None only
    when the free feed is disabled and no paid key is set, or the chosen provider
    is unreachable. ``price`` (current spot) is needed for the realized ratio.
    """
    from ..config import load_config

    cfg = load_config()
    if cfg.glassnode_api_key:
        try:
            return _from_glassnode(cfg.glassnode_api_key, price)
        except Exception as exc:  # noqa: BLE001
            log.warning("Glassnode fetch failed (%s); on-chain layer skipped", exc)
            return dict(_NONE_READINGS)

    if cfg.onchain_free_enabled:
        try:
            readings = _from_bitcoin_data(price)
        except Exception as exc:  # noqa: BLE001
            log.warning("bitcoin-data.com fetch failed (%s); on-chain layer skipped", exc)
            return dict(_NONE_READINGS)
        if all(v is None for v in readings.values()):
            log.warning("bitcoin-data.com returned no on-chain values; layer dark this run")
        return readings

    log.info("On-chain layer disabled (ONCHAIN_FREE=false and no paid key); skipped")
    return dict(_NONE_READINGS)

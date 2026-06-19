"""Stablecoin Supply Ratio (SSR) — crypto dry-powder.

SSR = BTC market cap / total major USD-stablecoin market cap. A LOW ratio means a
large pool of stablecoins sits idle relative to BTC (latent buying capacity) —
historically supportive near bottoms; a HIGH ratio means little sidelined dry
powder. Free, no key: BTC cap from CoinGecko, stablecoin supply from DefiLlama.

We sum the major fiat-backed USD pegs (USDT/USDC/DAI) for a stable denominator
rather than DefiLlama's full list (which mixes non-USD and algorithmic stables).
Both endpoints are public, keyless, and reachable headless; each fails soft to
None so the SSR term simply drops from the macro category on any error.
"""
from __future__ import annotations

import logging

from ._http import get_json

log = logging.getLogger(__name__)

COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
DEFILLAMA_STABLECOINS = "https://stablecoins.llama.fi/stablecoins"
_MAJOR_USD = {"USDT", "USDC", "DAI"}

_NONE = {"ssr": None}


def _btc_mcap() -> float | None:
    data = get_json(COINGECKO_MARKETS, params={"vs_currency": "usd", "ids": "bitcoin"})
    if not isinstance(data, list) or not data:
        return None
    try:
        v = data[0].get("market_cap")
        return float(v) if v else None
    except (TypeError, ValueError, AttributeError):
        return None


def _stablecoin_mcap() -> float | None:
    """Sum of major USD-stablecoin circulating supply ($). None if unavailable."""
    data = get_json(DEFILLAMA_STABLECOINS, params={"includePrices": "true"})
    assets = data.get("peggedAssets") if isinstance(data, dict) else None
    if not assets:
        return None
    total, found = 0.0, False
    for a in assets:
        if not isinstance(a, dict) or a.get("symbol") not in _MAJOR_USD:
            continue
        v = (a.get("circulating") or {}).get("peggedUSD")
        if v is None:
            continue
        try:
            total += float(v)
            found = True
        except (TypeError, ValueError):
            continue
    return total if found else None


def ssr() -> dict:
    """Stablecoin Supply Ratio keyed for the scorer. Fails soft to {"ssr": None}."""
    try:
        btc = _btc_mcap()
        stables = _stablecoin_mcap()
        if not btc or not stables:
            return dict(_NONE)
        return {"ssr": btc / stables}
    except Exception as exc:  # noqa: BLE001 - fail soft; never break the long-term run
        log.warning("ssr() failed (%s); SSR skipped", exc)
        return dict(_NONE)

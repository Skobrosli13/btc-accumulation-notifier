"""Stablecoin Supply Ratio (SSR) — crypto dry-powder.

SSR = BTC market cap / USD-stablecoin market cap. A LOW ratio means a large pool
of stablecoins sits idle relative to BTC (latent buying capacity) — historically
supportive near bottoms; a HIGH ratio means little sidelined dry powder. Free,
no key: BTC cap from CoinGecko, stablecoin supply from DefiLlama.

The denominator universe is selected DYNAMICALLY from the DefiLlama payload:
every USD-pegged asset (``pegType == "peggedUSD"``) above a $1bn circulating
floor. A hardcoded trio (USDT/USDC/DAI) drifts away from the metric as the
stablecoin market moves — DAI's supply migrated to USDS (uncounted), and
USDe/FDUSD/PYUSD grew from zero — so a fixed universe systematically understates
dry powder over time and quietly makes the fixed scoring band (neutral 6 /
extreme 3, aligned with the canonical total-supply SSR) read less bullish every
year for non-market reasons. The floor keeps dust/exotic pegs out; if the
payload stops carrying ``pegType``, the static trio remains as a fallback.
Both endpoints are public, keyless, and reachable headless; each fails soft to
None so the SSR term simply drops from the macro category on any error.
"""
from __future__ import annotations

import logging

from ._http import get_json

log = logging.getLogger(__name__)

COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
DEFILLAMA_STABLECOINS = "https://stablecoins.llama.fi/stablecoins"
_MAJOR_USD = {"USDT", "USDC", "DAI"}   # static fallback when pegType is absent
_MIN_USD_MCAP = 1e9                     # dynamic-universe floor ($ circulating)

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
    """Total USD-stablecoin circulating supply ($), dynamic universe (pegType
    ``peggedUSD`` above the $1bn floor); static-trio fallback. None if unavailable."""
    data = get_json(DEFILLAMA_STABLECOINS, params={"includePrices": "true"})
    assets = data.get("peggedAssets") if isinstance(data, dict) else None
    if not assets:
        return None
    dyn_total, dyn_found = 0.0, False
    fb_total, fb_found = 0.0, False
    for a in assets:
        if not isinstance(a, dict):
            continue
        v = (a.get("circulating") or {}).get("peggedUSD")
        try:
            val = float(v)
        except (TypeError, ValueError):
            continue
        if a.get("pegType") == "peggedUSD" and val >= _MIN_USD_MCAP:
            dyn_total += val
            dyn_found = True
        if a.get("symbol") in _MAJOR_USD:
            fb_total += val
            fb_found = True
    if dyn_found:
        return dyn_total
    return fb_total if fb_found else None


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

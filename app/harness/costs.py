"""Per-tier round-trip cost model (§5.4).

Static, deliberately conservative round-trip (entry+exit) cost assumptions in
bps, netted out of every gross expectancy before a gate sees it. Micro-tier is
treated as a LOWER BOUND in gate math (§7): real micro fills are worse, so a
strategy that only clears costs in micro is not promotable evidence.

Measured live costs (Phase 4 paper fills) later refresh these via the nightly
cost-curve job — until then the constants come from config (COST_BPS_*).
"""
from __future__ import annotations

# Default round-trip costs in bps by equity cap tier + BTC spot (§5.4).
DEFAULT_COST_BPS = {
    "large": 10.0,
    "mid": 20.0,
    "small": 40.0,
    "micro": 80.0,
    "btc": 10.0,
}


def round_trip_bps(tier: str | None, overrides: dict | None = None) -> float:
    """Round-trip cost in bps for a tier ('large'/'mid'/'small'/'micro'/'btc').
    Unknown/None tier gets the WORST equity tier (unknown liquidity is priced
    pessimistically, never optimistically)."""
    table = {**DEFAULT_COST_BPS, **(overrides or {})}
    return float(table.get((tier or "").lower(), table["micro"]))


def net_return(gross: float, tier: str | None, overrides: dict | None = None) -> float:
    """Gross fractional return -> net of the tier's round-trip cost."""
    return gross - round_trip_bps(tier, overrides) / 10_000.0

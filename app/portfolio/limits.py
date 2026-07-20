"""Portfolio limits + drawdown discipline (§7) — pure checks.

Pre-registered constants: ≤12 concurrent positions; ≤3 per sector; reject a
candidate whose mean 60-day correlation to the open book exceeds 0.6; BTC is
one position with its own 15% NAV cap. Drawdown ladder: −10% ⇒ halve gross;
−15% ⇒ flat pending written review. Every check returns the violated rules by
name — the caller renders/decides, this module never mutates anything.
"""
from __future__ import annotations

MAX_CONCURRENT = 12
MAX_PER_SECTOR = 3
MAX_MEAN_CORR = 0.6
DD_HALVE_GROSS = 0.10
DD_GO_FLAT = 0.15

# Per-source budgets for the shared paper book. 'lab' deliberately re-states the
# original §7 constants and is evaluated against LAB POSITIONS ONLY: the
# meta-gate (§9) judges the program on the promoted study's curve, so that curve
# must stay a function of the constants the study was registered under. Adding
# swing/long-term feeds must not retroactively change which lab fills happened.
SOURCE_LIMITS = {
    "lab":      {"max_concurrent": MAX_CONCURRENT, "max_per_sector": MAX_PER_SECTOR},
    "swing":    {"max_concurrent": 8,  "max_per_sector": 3},
    "longterm": {"max_concurrent": 12, "max_per_sector": 4},
}


def limits_for(source: str) -> dict:
    """Budget for a book source; unknown sources fall back to the §7 constants."""
    return SOURCE_LIMITS.get(source, SOURCE_LIMITS["lab"])


def check_candidate(open_positions: list[dict], candidate: dict, *,
                    mean_corr_60d: float | None = None,
                    max_concurrent: int = MAX_CONCURRENT,
                    max_per_sector: int = MAX_PER_SECTOR) -> list[str]:
    """Violations that bar ``candidate`` from opening (empty list = admissible).

    ``open_positions``: [{ticker, sector, is_btc?}]; ``candidate`` likewise;
    ``mean_corr_60d``: candidate's mean 60d return correlation to the open book
    (None with an empty book, or when the caller couldn't compute it — an
    UNCOMPUTABLE correlation on a non-empty book is a violation: unpriced
    crowding risk is rejected, not waved through).

    ``max_concurrent``/``max_per_sector`` default to the §7 constants; the book
    passes the calling source's budget (see :data:`SOURCE_LIMITS`). Callers pass
    only their own source's open positions, so budgets never interact."""
    v: list[str] = []
    if len(open_positions) >= max_concurrent:
        v.append(f"max_concurrent ({len(open_positions)}/{max_concurrent})")
    sector = candidate.get("sector")
    if sector:
        n_sector = sum(1 for p in open_positions if p.get("sector") == sector)
        if n_sector >= max_per_sector:
            v.append(f"max_per_sector ({sector}: {n_sector}/{max_per_sector})")
    if candidate.get("is_btc") and any(p.get("is_btc") for p in open_positions):
        v.append("btc_single_position")
    if open_positions:
        if mean_corr_60d is None:
            v.append("correlation_unpriced")
        elif mean_corr_60d > MAX_MEAN_CORR:
            v.append(f"mean_corr_60d {mean_corr_60d:.2f} > {MAX_MEAN_CORR}")
    return v


def drawdown_action(current_dd: float) -> str:
    """'none' | 'halve_gross' | 'flat' from the current peak-to-now drawdown
    (positive fraction). The −15% action requires a WRITTEN review to resume
    (ledger-enforced by the caller)."""
    if current_dd >= DD_GO_FLAT:
        return "flat"
    if current_dd >= DD_HALVE_GROSS:
        return "halve_gross"
    return "none"

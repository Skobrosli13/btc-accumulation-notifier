"""Long-term "long buys" engine (pure, I/O-free): gate -> rank -> combine.

Evidence-based factor design (see STOCKS_LONGTERM_PLAN.md):
- **Gate** purges value traps: Piotroski F >= 5, Altman not-distressed, positive
  gross profitability + operating margin, above 200-DMA, positive 12-1 momentum, not
  a heavy diluter. Low quality + weak momentum *is* a value trap.
- **Value** (cheap) ranked SECTOR-RELATIVE (a bank's P/E != software's): a composite
  of earnings / OCF / sales / book / shareholder yields.
- **Quality** (durable) ranked universe-wide: gross profitability, ROIC, margin, low
  accruals, low asset growth, Piotroski.
- **Momentum** (working) ranked universe-wide: 12-1 return.
- **Combine at the portfolio level** into a value-led conviction score with quality +
  momentum confirmation — long buys are the intersection of cheap + working + quality,
  not one mushy blend. Sub-ranks are surfaced so the presentation stays two-axis.
"""
from __future__ import annotations

# value metrics (higher yield = cheaper = better), ranked sector-relative
VALUE_METRICS = ["earnings_yield", "ocf_yield", "sales_yield", "book_yield", "shareholder_yield"]
# quality metrics: (key, higher_is_better)
QUALITY_METRICS = [
    ("gross_profitability", True), ("roic", True), ("operating_margin", True),
    ("accruals", False), ("asset_growth", False),
]
CONVICTION_WEIGHTS = {"value": 0.45, "quality": 0.30, "momentum": 0.25}


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _percentiles(values: list[float | None], higher_better: bool = True) -> list[float]:
    """Ordinal percentile [0,1] within the list. None -> 0.0 (missing = no credit)."""
    n = len(values)
    if n == 0:
        return []
    present = [(i, v) for i, v in enumerate(values) if v is not None]
    ranks = [0.0] * n
    if len(present) <= 1:
        for i, _ in present:
            ranks[i] = 0.5
        return ranks
    present.sort(key=lambda iv: iv[1], reverse=higher_better)   # best first
    # after sort, best is first; assign 1.0 (best) .. 0.0 (worst)
    m = len(present)
    for pos, (i, _) in enumerate(present):
        ranks[i] = 1.0 - pos / (m - 1)
    return ranks


def gate(cand: dict) -> tuple[bool, list[str]]:
    """Value-trap purge. Returns (passes, reasons_failed)."""
    m = cand.get("metrics") or {}
    pio = (m.get("piotroski") or {}).get("score")
    alt = m.get("altman") or {}
    fails = []
    if pio is None or pio < 5:
        fails.append("piotroski<5")
    if alt.get("band") == "distress":
        fails.append("altman_distress")
    if not (m.get("gross_profitability") or 0) > 0:
        fails.append("unprofitable_gross")
    if not (m.get("operating_margin") or 0) > 0:
        fails.append("negative_op_margin")
    if not cand.get("above_200dma"):
        fails.append("below_200dma")
    if not (cand.get("momentum_12_1") or 0) > 0:
        fails.append("negative_momentum")
    if (m.get("shareholder_yield") is not None) and m["shareholder_yield"] < -0.10:
        fails.append("heavy_dilution")
    return (len(fails) == 0, fails)


def _sector_relative_value(survivors: list[dict]) -> None:
    """Assign each survivor a value_rank = mean sector-relative percentile of the value
    metrics (mutates survivors: sets ['value_rank'] and per-metric percentiles)."""
    by_sector: dict[str, list[dict]] = {}
    for c in survivors:
        by_sector.setdefault(c.get("sector") or "?", []).append(c)
    for sector, group in by_sector.items():
        # small sectors are unstable -> fall back to the whole survivor set
        cohort = group if len(group) >= 5 else survivors
        for metric in VALUE_METRICS:
            vals = [(c.get("metrics") or {}).get(metric) for c in cohort]
            pcts = _percentiles(vals, higher_better=True)
            pmap = {id(c): p for c, p in zip(cohort, pcts)}
            for c in group:
                c.setdefault("_value_pct", {})[metric] = pmap.get(id(c), 0.0)
    for c in survivors:
        vp = c.get("_value_pct") or {}
        c["value_rank"] = round(sum(vp.values()) / len(VALUE_METRICS), 3) if vp else 0.0


def _universe_quality_momentum(survivors: list[dict]) -> None:
    """Assign quality_rank (universe-wide mean percentile of quality metrics) and
    momentum_rank (percentile of 12-1 return)."""
    for metric, higher in QUALITY_METRICS:
        vals = [(c.get("metrics") or {}).get(metric) for c in survivors]
        pcts = _percentiles(vals, higher_better=higher)
        for c, p in zip(survivors, pcts):
            c.setdefault("_qual_pct", {})[metric] = p
    # Piotroski contributes directly (0-9 -> 0-1)
    for c in survivors:
        pio = ((c.get("metrics") or {}).get("piotroski") or {}).get("score") or 0
        c.setdefault("_qual_pct", {})["piotroski"] = pio / 9.0
    for c in survivors:
        qp = c["_qual_pct"]
        c["quality_rank"] = round(sum(qp.values()) / len(qp), 3)
    mvals = [c.get("momentum_12_1") for c in survivors]
    mpcts = _percentiles(mvals, higher_better=True)
    for c, p in zip(survivors, mpcts):
        c["momentum_rank"] = round(p, 3)


def rank_long_buys(candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    """Gate + rank the universe. Returns (survivors_ranked_by_conviction, gated_out).

    Each candidate: {ticker, sector, metrics (from stock_fundamentals.compute),
    momentum_12_1, above_200dma, price}."""
    survivors, gated = [], []
    for c in candidates:
        ok, fails = gate(c)
        (survivors if ok else gated).append(c)
        c["gate_pass"], c["gate_fails"] = ok, fails
    if not survivors:
        return [], gated
    _sector_relative_value(survivors)
    _universe_quality_momentum(survivors)
    w = CONVICTION_WEIGHTS
    for c in survivors:
        c["conviction"] = round(100.0 * _clamp01(
            w["value"] * c["value_rank"] + w["quality"] * c["quality_rank"]
            + w["momentum"] * c["momentum_rank"]), 1)
        # clean up scratch
        c.pop("_value_pct", None)
        c.pop("_qual_pct", None)
    survivors.sort(key=lambda c: c["conviction"], reverse=True)
    return survivors, gated


def fair_value_band(cand: dict, sector_median_ey: float | None) -> dict | None:
    """Illustrative 'accumulate below' band: price implied by the SECTOR-MEDIAN earnings
    yield vs the name's own earnings. > current price => trades cheap vs sector."""
    m = cand.get("metrics") or {}
    ey, price = m.get("earnings_yield"), cand.get("price")
    if not ey or ey <= 0 or not price or not sector_median_ey or sector_median_ey <= 0:
        return None
    fair = price * (ey / sector_median_ey)   # if name's EY > sector median (cheaper), fair > price
    return {"fair_value": round(fair, 2),
            "discount_pct": round((fair / price - 1) * 100, 1),
            "basis": "sector-median earnings yield (illustrative, not a target)"}

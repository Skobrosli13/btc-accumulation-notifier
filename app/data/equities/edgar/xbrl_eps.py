"""SRW-SUE from SEC EDGAR XBRL — standardized earnings surprise (§4.5).

Replaces the Finnhub PEAD seed (whose historical surprises were reconstructed
with look-ahead — see the edge audit). Here the surprise is a **seasonal random
walk** standardized unexpected earnings, built only from point-in-time
as-reported diluted EPS:

    SUE_q = (EPS_q − EPS_{q−4}) / σ(trailing seasonal diffs)

Point-in-time discipline (the whole reason this exists):
  * one value per (fiscal_year, fiscal_quarter), the **earliest acceptance-dated**
    filing — a later restatement is look-ahead and is dropped.
  * true single-quarter EPS only: EDGAR also carries 6-/9-month YTD facts under
    the same ``fp`` tag, so we keep only ~quarter-length durations and DERIVE Q4
    as FY − (Q1+Q2+Q3) (10-Ks report the year, never Q4 alone).
  * σ is the sample stdev of the trailing ``window`` seasonal diffs (≥
    ``min_diffs`` required); |SUE| winsorized to ``winsor``.

The event timestamp (BMO/AMC) comes from the 8-K/Item-2.02 acceptance instant in
``sources/stocks/edgar_earnings`` — this module supplies the surprise MAGNITUDE.

Split adjustment: as-reported EPS must be split-adjusted before differencing
(§4.5). That factor comes from Sharadar ACTIONS (a keyed feed); ``seasonal_sue``
therefore accepts an optional ``adjust`` callable ``(fy, fq) -> factor`` so the
math is testable now and the ACTIONS join plugs in when the bundle lands.

Everything above the fetch boundary is pure (no I/O) and fixture-tested.
"""
from __future__ import annotations

import logging
import statistics
from datetime import date

log = logging.getLogger(__name__)

# EDGAR duration buckets (days) that separate a true single quarter from the
# 6-/9-month YTD facts filers also tag Q2/Q3, and the annual (FY) fact.
_Q_MIN_DAYS, _Q_MAX_DAYS = 80, 100
_FY_MIN_DAYS, _FY_MAX_DAYS = 350, 380

_FP_TO_Q = {"Q1": 1, "Q2": 2, "Q3": 3}

# Defaults for the standardization window.
_WINDOW = 8         # trailing seasonal diffs used to scale the surprise
_MIN_DIFFS = 6      # need at least this many diffs before a SUE is defined
_WINSOR = 10.0      # clip |SUE| to this


def _duration_days(start: str, end: str) -> int | None:
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except (ValueError, TypeError):
        return None


def parse_companyconcept(payload: dict) -> dict[tuple[int, int], float]:
    """PIT quarterly diluted EPS from an EDGAR ``companyconcept`` payload.

    Returns ``{(fiscal_year, fiscal_quarter): eps}`` for q in 1..4:
      * quarterly facts (fp Q1/Q2/Q3, ~quarter duration), deduped to the EARLIEST
        ``filed`` per (fy, q) — a restatement filed later is look-ahead;
      * Q4 = FY − (Q1+Q2+Q3), only when all three quarters AND the annual fact
        are present for that fiscal year.

    Ignores YTD facts (6-/9-month durations under a Q2/Q3 tag) via the duration
    filter, so differencing never mixes a cumulative figure with a single quarter.
    """
    units = (payload or {}).get("units") or {}
    facts = units.get("USD/shares") or units.get("USD/share") or []

    # Earliest-filed wins for both the quarters and the annual figure.
    best_q: dict[tuple[int, int], tuple[str, float]] = {}     # (fy,q) -> (filed, eps)
    best_fy: dict[int, tuple[str, float]] = {}                 # fy     -> (filed, eps)
    for f in facts:
        fy, fp, val = f.get("fy"), f.get("fp"), f.get("val")
        start, end, filed = f.get("start"), f.get("end"), f.get("filed") or ""
        if fy is None or val is None or not start or not end:
            continue
        dur = _duration_days(start, end)
        if dur is None:
            continue
        if fp in _FP_TO_Q and _Q_MIN_DAYS <= dur <= _Q_MAX_DAYS:
            key = (int(fy), _FP_TO_Q[fp])
            cur = best_q.get(key)
            if cur is None or filed < cur[0]:
                best_q[key] = (filed, float(val))
        elif fp == "FY" and _FY_MIN_DAYS <= dur <= _FY_MAX_DAYS:
            cur = best_fy.get(int(fy))
            if cur is None or filed < cur[0]:
                best_fy[int(fy)] = (filed, float(val))

    out: dict[tuple[int, int], float] = {(fy, q): eps for (fy, q), (_f, eps) in best_q.items()}
    # Derive Q4 where the year is complete.
    for fy, (_f, fy_eps) in best_fy.items():
        q123 = [out.get((fy, q)) for q in (1, 2, 3)]
        if all(v is not None for v in q123):
            out[(fy, 4)] = round(fy_eps - sum(q123), 6)
    return out


def _seasonal_diffs(eps: dict[tuple[int, int], float],
                    adjust=None) -> dict[tuple[int, int], float]:
    """d_(fy,q) = adj(fy,q)·EPS_(fy,q) − adj(fy-1,q)·EPS_(fy-1,q), where the
    prior-year same quarter exists. ``adjust(fy, q) -> factor`` defaults to 1."""
    adj = adjust or (lambda _fy, _q: 1.0)
    out: dict[tuple[int, int], float] = {}
    for (fy, q), v in eps.items():
        prev = eps.get((fy - 1, q))
        if prev is None:
            continue
        out[(fy, q)] = adj(fy, q) * v - adj(fy - 1, q) * prev
    return out


def seasonal_sue(eps: dict[tuple[int, int], float], *, window: int = _WINDOW,
                 min_diffs: int = _MIN_DIFFS, winsor: float = _WINSOR,
                 adjust=None) -> dict[tuple[int, int], float]:
    """SRW-SUE per fiscal quarter from a PIT EPS series (pure).

    For each quarter, scale its seasonal diff by the sample stdev of the ``window``
    seasonal diffs that PRECEDE it (exclusive of the current quarter — so a genuine
    surprise is not allowed to inflate its own scale, and |SUE| can legitimately
    exceed the winsor bound, which is the point of clipping). Quarters with fewer
    than ``min_diffs`` preceding diffs, or a degenerate zero-variance window, get
    no SUE (undefined rather than fabricated). |SUE| is winsorized to ``winsor``.
    """
    diffs = _seasonal_diffs(eps, adjust)
    ordered = sorted(diffs)                       # chronological (fy, q)
    series = [diffs[k] for k in ordered]
    out: dict[tuple[int, int], float] = {}
    for i, key in enumerate(ordered):
        win = series[max(0, i - window): i]       # the diffs BEFORE this quarter
        if len(win) < min_diffs:
            continue
        sigma = statistics.stdev(win)             # sample stdev (ddof=1)
        if sigma <= 0:
            continue
        sue = diffs[key] / sigma
        out[key] = max(-winsor, min(winsor, sue))
    return out


# --- Network boundary (EDGAR companyconcept; free, key-less) ------------------

_CONCEPT_URL = ("https://data.sec.gov/api/xbrl/companyconcept/"
                "CIK{cik10}/us-gaap/EarningsPerShareDiluted.json")


def fetch_diluted_eps(cik: str, user_agent: str) -> dict | None:
    """Fetch the raw ``EarningsPerShareDiluted`` companyconcept JSON (or None).

    Fail-soft like every adapter; the pure ``parse_companyconcept`` /
    ``seasonal_sue`` do the work. Import of the shared HTTP helper is local to
    avoid a deep relative import (that helper moves to core/ in the deferred
    §0.5c source relocation)."""
    from app.sources._http import get_json
    try:
        cik10 = f"{int(cik):010d}"
    except (ValueError, TypeError):
        return None
    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
    data = get_json(_CONCEPT_URL.format(cik10=cik10), headers=headers)
    return data if isinstance(data, dict) else None


def sue_for_cik(cik: str, user_agent: str, *, adjust=None) -> dict[tuple[int, int], float]:
    """End-to-end: fetch -> parse (PIT) -> SUE. Returns {} on any failure."""
    payload = fetch_diluted_eps(cik, user_agent)
    if not payload:
        return {}
    return seasonal_sue(parse_companyconcept(payload), adjust=adjust)

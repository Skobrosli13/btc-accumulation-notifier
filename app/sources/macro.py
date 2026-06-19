"""Macro / liquidity from FRED (free API key).

Series used:
  M2SL        US M2 (monthly)  -> YoY % change, a starting proxy for global liquidity
  DFII10      10Y TIPS / real yield (daily) -> falling is bullish
  BAMLH0A0HYM2  HY OAS credit spread (daily) -> wide = risk-off capitulation
  DGS10, DTWEXBGS are fetched too for the ledger, but are not scored directly.
  NOTE: DTWEXBGS is the FRED *Broad* trade-weighted USD index, NOT ICE's DXY.
  It is surfaced under ``broad_dollar`` (with a legacy ``dxy`` alias kept for any
  existing consumer); both are context-only and never scored.

Returns all-None readings if no FRED key is configured (layer is skipped and the
category weight renormalizes away).
"""
from __future__ import annotations

import logging
from datetime import date as _date, timedelta

from ._http import get_json

log = logging.getLogger(__name__)

FRED_OBS = "https://api.stlouisfed.org/fred/series/observations"


def _series(series_id: str, api_key: str, limit: int = 0) -> list[tuple[str, float]]:
    """Return [(date, value), ...] oldest->newest, skipping FRED's '.' missings."""
    params = {"series_id": series_id, "api_key": api_key, "file_type": "json"}
    if limit:
        params["sort_order"] = "desc"
        params["limit"] = limit
    data = get_json(FRED_OBS, params=params)
    if not isinstance(data, dict):
        # FRED normally returns an object; a list / error string would make
        # ``.get`` raise. Degrade to "no rows" instead.
        return []
    obs = data.get("observations", []) or []
    out: list[tuple[str, float]] = []
    for o in obs:
        v = o.get("value")
        if v in (None, "", "."):
            continue
        try:
            out.append((o.get("date", ""), float(v)))
        except ValueError:
            continue
    if limit:  # we asked desc; hand back oldest->newest
        out.reverse()
    return out


def _latest(series_id: str, api_key: str) -> float | None:
    rows = _series(series_id, api_key, limit=10)
    return rows[-1][1] if rows else None


def _m2_yoy(api_key: str) -> float | None:
    """Percent change in M2 vs ~12 months ago (M2SL is monthly)."""
    rows = _series("M2SL", api_key, limit=14)
    if len(rows) < 13:
        return None
    latest = rows[-1][1]
    year_ago = rows[-13][1]
    if year_ago == 0:
        return None
    return (latest / year_ago - 1.0) * 100.0


def _as_of(rows: list[tuple[str, float]], target: _date) -> float | None:
    """Value of the last row dated on/before ``target`` (rows oldest->newest)."""
    val = None
    for d, v in rows:
        try:
            if _date.fromisoformat(d) <= target:
                val = v
            else:
                break
        except ValueError:
            continue
    return val


def _net_liquidity(api_key: str) -> tuple[float | None, float | None]:
    """(level $M, YoY %) of Fed net liquidity = WALCL - TGA(WTREGEN) - RRP*1000.

    WALCL/WTREGEN are weekly ($M); RRPONTSYD is daily ($B, hence *1000 to $M). The
    different-frequency series are aligned by date (RRP/TGA taken as-of the WALCL
    week). The YoY change is the scored signal — expanding net liquidity is bullish
    (like M2), and it captures the TGA/RRP plumbing that M2 misses; the absolute
    level is recorded as context only.
    """
    walcl = _series("WALCL", api_key, limit=70)        # weekly, ~16 months
    tga = _series("WTREGEN", api_key, limit=70)         # weekly
    rrp = _series("RRPONTSYD", api_key, limit=420)      # daily, ~16 months
    if not walcl or not tga or not rrp:
        return None, None
    d_now = _date.fromisoformat(walcl[-1][0])
    tga_now, rrp_now = _as_of(tga, d_now), _as_of(rrp, d_now)
    if tga_now is None or rrp_now is None:
        return None, None
    net_now = walcl[-1][1] - tga_now - rrp_now * 1000.0
    target = d_now - timedelta(days=364)
    w_prev, tga_prev, rrp_prev = _as_of(walcl, target), _as_of(tga, target), _as_of(rrp, target)
    if w_prev is None or tga_prev is None or rrp_prev is None:
        return net_now, None
    net_prev = w_prev - tga_prev - rrp_prev * 1000.0
    yoy = ((net_now / net_prev - 1.0) * 100.0) if net_prev else None
    return net_now, yoy


def macro() -> dict:
    """Macro readings keyed for the scorer. Imports config lazily to avoid cycles.

    Blanket-wrapped so this public entry point can ONLY return its normal dict
    (never raise) into run_once.gather_readings — a top-level non-dict FRED
    response would otherwise make ``data.get(...)`` raise and kill the whole run.
    Matches the fail-soft style of onchain()/derivatives().
    """
    none_readings = {"m2_yoy": None, "hy_spread": None, "real_yield": None,
                     "net_liq_yoy": None, "nfci": None,
                     "dgs10": None, "broad_dollar": None, "dxy": None,
                     "net_liq": None}
    try:
        from ..config import load_config

        cfg = load_config()
        if not cfg.fred_api_key:
            log.info("FRED_API_KEY not set; macro layer skipped")
            return none_readings

        key = cfg.fred_api_key
        # DTWEXBGS is the FRED Broad trade-weighted dollar index, not ICE DXY.
        # Surface it correctly as ``broad_dollar`` and keep a ``dxy`` alias so no
        # existing consumer breaks. Both are context-only (not scored).
        broad_dollar = _latest("DTWEXBGS", key)
        net_liq, net_liq_yoy = _net_liquidity(key)
        return {
            "m2_yoy": _m2_yoy(key),
            "hy_spread": _latest("BAMLH0A0HYM2", key),
            "real_yield": _latest("DFII10", key),
            "net_liq_yoy": net_liq_yoy,   # Fed net liquidity YoY % (scored)
            "nfci": _latest("NFCI", key),  # Chicago Fed financial conditions (scored)
            # context-only, recorded but not scored:
            "dgs10": _latest("DGS10", key),
            "broad_dollar": broad_dollar,
            "dxy": broad_dollar,  # legacy alias (deprecated label)
            "net_liq": net_liq,   # net liquidity level $M (context)
        }
    except Exception as exc:  # noqa: BLE001 - fail soft; never break the long-term run
        log.warning("macro() failed (%s); macro layer skipped", exc)
        return none_readings

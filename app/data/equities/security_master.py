"""Security master keyed on Sharadar ``permaticker`` — §4.2.

A ticker is reused across companies over time, so research data must NEVER be
joined on a bare ticker; it is joined on ``permaticker`` (stable per issuer). This
builds, from the TICKERS table:

  * ``ticker_permaticker_map`` — current ticker -> permaticker (collisions
    resolved toward the still-listed issuer; a remaining tie is flagged, because
    fully PIT ticker resolution needs the ACTIONS ticker-change history);
  * ``master_by_permaticker`` — permaticker -> issuer attributes + its ticker(s).

Pure (operates on TICKERS row-dicts); the ingest supplies the rows from the lake.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _is_listed(row: dict) -> bool:
    return str(row.get("isdelisted", "")).upper() == "N"


def ticker_permaticker_map(rows: list[dict]) -> dict[str, int]:
    """{ticker: permaticker}. On a reused ticker, prefer the still-listed issuer;
    if two listed (or two delisted) issuers share it, keep the first and log —
    that pair needs date-scoped resolution via ACTIONS ticker changes."""
    out: dict[str, int] = {}
    chosen_listed: dict[str, bool] = {}
    for r in rows:
        tk, pt = r.get("ticker"), r.get("permaticker")
        if not tk or pt is None:
            continue
        listed = _is_listed(r)
        if tk not in out:
            out[tk], chosen_listed[tk] = pt, listed
        elif listed and not chosen_listed[tk]:
            out[tk], chosen_listed[tk] = pt, True          # prefer the listed one
        elif listed == chosen_listed[tk] and out[tk] != pt:
            log.info("ticker %s maps to permatickers %s and %s (same listed=%s) "
                     "— keeping first; needs PIT ticker-change resolution",
                     tk, out[tk], pt, listed)
    return out


def master_by_permaticker(rows: list[dict]) -> dict[int, dict]:
    """{permaticker: {name, exchange, sector, category, siccode, isdelisted,
    tickers:[...]}}. Attributes come from the still-listed row when present, else
    the last row seen; all of the issuer's tickers are collected."""
    out: dict[int, dict] = {}
    for r in rows:
        pt = r.get("permaticker")
        if pt is None:
            continue
        entry = out.get(pt)
        if entry is None:
            entry = {"permaticker": pt, "tickers": [], "isdelisted": r.get("isdelisted"),
                     "name": None, "exchange": None, "sector": None,
                     "category": None, "siccode": None}
            out[pt] = entry
        tk = r.get("ticker")
        if tk and tk not in entry["tickers"]:
            entry["tickers"].append(tk)
        # Prefer a listed row's attributes; otherwise take the first non-null.
        prefer = _is_listed(r) or entry["name"] is None
        if prefer:
            for k in ("name", "exchange", "sector", "category", "siccode", "isdelisted"):
                if r.get(k) is not None:
                    entry[k] = r.get(k)
    return out

"""insider_cluster emitter (§6 study #1) — clustered open-market insider buying.

Event (exact, from the pre-registration): within a trailing 14-day window on
TRANSACTION dates, ≥2 DISTINCT officers/directors make open-market code-P buys
aggregating ≥ $50k → one LONG event, stamped at the latest FILING date among
the contributing buys (the instant the full cluster became public — PIT).
CEO/CFO participation ⇒ strength 1.5, else 1.0.

Exclusions:
  * routine insiders (Cohen–Malloy–Pomorski): a buyer who bought in the SAME
    calendar month in ≥2 of the prior 3 years (per ticker+owner) is dropped
    before clustering;
  * 10b5-1 plans: SF2 carries no plan flag — the routine filter is the
    documented proxy (pre-registered limitation in the spec).

A cluster emits ONCE: subsequent buys inside the same rolling window extend it
silently; a ≥14-day quiet gap closes it and a fresh cluster may form. Pure —
the caller feeds SF2 code-P rows and enriches/persists the result.
"""
from __future__ import annotations

from datetime import datetime, timezone

WINDOW_DAYS = 14
MIN_OWNERS = 2
MIN_AGG_USD = 50_000.0
EXEC_STRENGTH = 1.5
_DAY_MS = 86_400_000

_EXEC_TITLES = ("CEO", "CHIEF EXECUTIVE", "CFO", "CHIEF FINANCIAL")


def _ms(date_str) -> int | None:
    try:
        return int(datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def is_executive(officertitle) -> bool:
    # NaN-proof: pandas rows carry float('nan') for missing titles, and NaN is
    # truthy — `officertitle or ""` would pass it through.
    t = officertitle.upper() if isinstance(officertitle, str) else ""
    return any(pat in t for pat in _EXEC_TITLES)


def routine_owner_keys(fills: list[dict]) -> set[tuple[str, str]]:
    """(ticker, ownername) pairs whose buying is ROUTINE: for some buy, the same
    calendar month carries a buy in >= 2 of the prior 3 years (per ticker+owner).
    Computed over the full fill history the caller supplies."""
    months: dict[tuple[str, str], set[tuple[int, int]]] = {}
    for f in fills:
        d = str(f.get("transactiondate") or "")[:10]
        if len(d) < 7:
            continue
        key = (f.get("ticker"), f.get("ownername"))
        months.setdefault(key, set()).add((int(d[:4]), int(d[5:7])))
    routine: set[tuple[str, str]] = set()
    for key, ym in months.items():
        for (y, m) in ym:
            prior = sum(1 for back in (1, 2, 3) if (y - back, m) in ym)
            if prior >= 2:
                routine.add(key)
                break
    return routine


def _fill_value(f: dict) -> float:
    v = f.get("transactionvalue")
    if v:
        return float(v)
    px, sh = f.get("transactionpricepershare"), f.get("transactionshares")
    return float(px) * float(sh) if px and sh else 0.0


def cluster_events(fills: list[dict], *, window_days: int = WINDOW_DAYS,
                   min_owners: int = MIN_OWNERS,
                   min_agg_usd: float = MIN_AGG_USD) -> list[dict]:
    """Emit insider-cluster events from SF2 code-P officer/director buys (pure).

    ``fills``: rows with ticker, ownername, officertitle, isofficer, isdirector,
    transactiondate, filingdate, transactionvalue (or price+shares). Returns
    [{ticker, event_ts, direction, strength, meta}] sorted by event_ts.
    """
    routine = routine_owner_keys(fills)
    usable = [f for f in fills
              if (str(f.get("isofficer", "")).upper() == "Y"
                  or str(f.get("isdirector", "")).upper() == "Y")
              and (f.get("ticker"), f.get("ownername")) not in routine
              and _ms(f.get("transactiondate")) is not None
              and _fill_value(f) > 0]

    by_ticker: dict[str, list[dict]] = {}
    for f in usable:
        by_ticker.setdefault(f["ticker"], []).append(f)

    events: list[dict] = []
    win_ms = window_days * _DAY_MS
    for tk, rows in by_ticker.items():
        rows.sort(key=lambda f: _ms(f["transactiondate"]))
        cluster: list[dict] = []
        emitted = False
        for f in rows:
            t = _ms(f["transactiondate"])
            # slide the window; a full gap closes the cluster
            cluster = [c for c in cluster if t - _ms(c["transactiondate"]) <= win_ms]
            if not cluster:
                emitted = False
            cluster.append(f)
            if emitted:
                continue
            owners = {c["ownername"] for c in cluster}
            agg = sum(_fill_value(c) for c in cluster)
            if len(owners) >= min_owners and agg >= min_agg_usd:
                filing_ts = [ts for ts in (_ms(c.get("filingdate")) for c in cluster)
                             if ts is not None]
                event_ts = max(filing_ts) if filing_ts else t
                strength = (EXEC_STRENGTH if any(is_executive(c.get("officertitle"))
                                                 for c in cluster) else 1.0)
                events.append({
                    "ticker": tk, "event_ts": event_ts, "direction": "LONG",
                    "strength": strength,
                    "meta": {"owners": sorted(owners), "agg_usd": round(agg, 2),
                             "n_fills": len(cluster)},
                })
                emitted = True
    events.sort(key=lambda e: e["event_ts"])
    return events

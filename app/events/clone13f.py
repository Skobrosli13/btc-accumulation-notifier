"""clone13f emitter (§6 study #4) — new positions by concentrated low-turnover
managers, cloned at the 13F deadline.

Pure: the caller feeds per-(investor, quarter) SHR holdings snapshots
[{investor, quarter (ISO quarter-end), aum, tickers:set}] built from SF3; this
module applies the pre-registered manager filters and emits one LONG event per
(ticker, quarter) aggregated across qualifying adders. Event timestamp =
quarter-end + 45 days (the statutory deadline — SF3 has no acceptance date;
the deadline is when the information is guaranteed public).

See studies/clone13f.md for the registered definition; any change here is
Class B (re-register as clone13f-v2).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

AUM_MIN = 100e6
AUM_MAX = 5e9
MAX_HOLDINGS = 30
MAX_ANNUAL_TURNOVER = 0.25
MIN_PRIOR_TRANSITIONS = 2
DEADLINE_DAYS = 45


def _quarter_index(q: str) -> int:
    """ISO quarter-end date -> monotonic quarter counter (consecutiveness test)."""
    d = datetime.strptime(str(q)[:10], "%Y-%m-%d")
    return d.year * 4 + (d.month - 1) // 3


def event_ts_for_quarter(q: str) -> int:
    """Quarter-end + 45 days, epoch ms UTC — the guaranteed-public instant."""
    d = datetime.strptime(str(q)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int((d + timedelta(days=DEADLINE_DAYS)).timestamp() * 1000)


def quarterly_turnover(prev: set, cur: set) -> float | None:
    """(#added + #dropped) / (2 × #held_prev); None on an empty prior book."""
    if not prev:
        return None
    added = len(cur - prev)
    dropped = len(prev - cur)
    return (added + dropped) / (2.0 * len(prev))


def cluster_events(snapshots: list[dict]) -> list[dict]:
    """Emit clone13f events from manager-quarter holdings snapshots (pure).

    ``snapshots``: [{investor, quarter, aum, tickers}] — every SHR quarter for
    every candidate manager (the caller pre-filters to managers that EVER pass
    the aum/holdings screen, but must include their FULL history so turnover
    and adds are computed against real priors).
    """
    by_mgr: dict[str, list[dict]] = {}
    for s in snapshots:
        by_mgr.setdefault(s["investor"], []).append(s)

    adds_by_key: dict[tuple[str, str], list[str]] = {}   # (ticker, quarter) -> managers
    for mgr, rows in by_mgr.items():
        rows.sort(key=lambda r: str(r["quarter"]))
        # trailing turnover series over CONSECUTIVE transitions
        turnovers: list[tuple[int, float]] = []           # (q_idx of the LATER quarter, turnover)
        for i in range(1, len(rows)):
            prev, cur = rows[i - 1], rows[i]
            qi_prev, qi_cur = _quarter_index(prev["quarter"]), _quarter_index(cur["quarter"])
            if qi_cur - qi_prev != 1:
                continue                                   # reporting gap breaks the chain
            t = quarterly_turnover(set(prev["tickers"]), set(cur["tickers"]))
            if t is not None:
                turnovers.append((qi_cur, t))

            # Qualification AT the signal quarter (cur):
            if not (AUM_MIN <= (cur.get("aum") or 0) <= AUM_MAX):
                continue
            if len(cur["tickers"]) > MAX_HOLDINGS:
                continue
            # trailing <=4 transitions ENDING at cur, all consecutive; need >=2
            # PRIOR transitions beyond the current one? The registered rule:
            # >=2 prior consecutive transitions of history INCLUDING the current
            # transition window — i.e. at least 2 observed turnovers ending <= cur.
            trail = [t for (qi, t) in turnovers if qi_cur - 4 < qi <= qi_cur]
            if len(trail) < MIN_PRIOR_TRANSITIONS:
                continue
            annual = (sum(trail) / len(trail)) * 4.0
            if annual > MAX_ANNUAL_TURNOVER:
                continue

            for tk in set(cur["tickers"]) - set(prev["tickers"]):
                adds_by_key.setdefault((tk, str(cur["quarter"])[:10]), []).append(mgr)

    events = []
    for (tk, q), mgrs in adds_by_key.items():
        events.append({
            "ticker": tk, "event_ts": event_ts_for_quarter(q),
            "direction": "LONG", "strength": float(len(mgrs)),
            "meta": {"managers": sorted(mgrs)[:20], "n_managers": len(mgrs),
                     "quarter": q},
        })
    events.sort(key=lambda e: (e["event_ts"], e["ticker"]))
    return events

"""Internal BTC paper ledger (§7) — fills into the harness `fills` table.

BTC paper fills price at OKX mid ± max(measured half-spread, 5 bps) against the
order side — the taker always crosses the spread, never earns it. Slippage is
recorded per fill so the nightly cost-curve refresh has real data.

**Scope: BTC only.** The internal ledger is valid for BTC because OKX mid is an
observable, keyless price. There is deliberately no equity function here.

Equity paper fills are NOT routed through a broker API. They are replayed by
``portfolio.book`` off the lake's Sharadar adjusted bars — a fill is the next
session's open ± the tier's half round-trip cost, so no equity price is ever
invented (working agreement #3: never fabricate market data). EDGE_LAB_PLAN_v2
§7 specifies Alpaca paper for equities and this module's docstring used to
claim it was implemented; it never was, and the plan's ADV cap and +10bps limit
cap are likewise unimplemented. Bar replay satisfies the honesty invariant but
NOT the plan's execution realism — treat that as an open gap, not a done item.
"""
from __future__ import annotations

import sqlite3
import time

MIN_HALF_SPREAD = 0.0005      # 5 bps


def btc_fill_price(mid: float, side: str, *, half_spread: float | None = None) -> float:
    """Paper fill at mid ± max(measured half-spread, 5bps); BUY pays up."""
    hs = max(half_spread if half_spread is not None else 0.0, MIN_HALF_SPREAD)
    return mid * (1.0 + hs) if side.upper() == "BUY" else mid * (1.0 - hs)


def record_btc_fill(conn: sqlite3.Connection, *, event_id: int | None, side: str,
                    qty: float, mid: float, half_spread: float | None = None,
                    ts: int | None = None) -> dict:
    """Write one BTC paper fill; returns the row (incl. realized slippage bps)."""
    px = btc_fill_price(mid, side, half_spread=half_spread)
    slippage_bps = abs(px / mid - 1.0) * 10_000.0
    row = {"event_id": event_id, "asset": "BTC", "side": side.upper(),
           "qty": qty, "limit_px": None, "fill_px": px,
           "fill_ts": ts if ts is not None else int(time.time() * 1000),
           "venue": "paper-okx-mid", "slippage_bps": slippage_bps}
    conn.execute(
        "INSERT INTO fills (event_id, asset, side, qty, limit_px, fill_px, "
        "fill_ts, venue, slippage_bps) VALUES (:event_id, :asset, :side, :qty, "
        ":limit_px, :fill_px, :fill_ts, :venue, :slippage_bps)", row)
    conn.commit()
    return row

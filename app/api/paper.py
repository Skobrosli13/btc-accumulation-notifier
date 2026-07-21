"""/api/paper — the consolidated paper-trading surface (dual-track §7).

ONE contract the redesigned dashboard centers on, merging what used to be three
disjoint representations (/api/book, today.paper, the per-strategy forward-test
cards):

  account          headline = the LIVE Alpaca paper account (@broker): real fills,
                   real slippage, reconciled equity.
  positions/orders/fills   the live account's holdings, working orders, executions.
  curves           @broker (live) + @lab + @combined (deterministic replay).
  research         labeled curve summaries — @lab is the ONLY meta-gate evidence.
  execution_delta  @broker vs @combined: what real fills cost vs the replay's
                   frictionless next-open assumption.

Honesty: the live account and the meta-gate evidence are kept permanently
distinct (see ``note``). Read-only + token-gated, same pattern as /api/book.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from ..api_deps import conn_ro as _conn
from ..api_deps import get_config, require_token
from ..config import Config
from ..portfolio import book as pbook
from ..portfolio import broker as pbroker
from .book import _curve_summary

router = APIRouter()

_DEGRADED_AFTER_MIN = 180.0   # a live account not reconciled in ~3h reads as stale


def _rows(conn, sql: str, params=()) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []


def _meta(conn) -> dict[str, str]:
    try:
        return {k: v for k, v in conn.execute(
            "SELECT key, value FROM lab_meta WHERE key LIKE 'broker_%'").fetchall()}
    except sqlite3.Error:
        return {}


def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@router.get("/api/paper")
def paper(cfg: Config = Depends(get_config), _=Depends(require_token)) -> dict:
    conn = _conn(cfg)
    try:
        orders = _rows(conn,
                       "SELECT client_order_id, broker_order_id, namespace, source, "
                       "ticker, asset, side, target_qty, limit_px, tif, "
                       "adv_cap_shares, sizing_basis, status, reject_reason, "
                       "submitted_ts, updated_ts FROM broker_orders "
                       "ORDER BY submitted_ts DESC LIMIT 400")
        bpositions = _rows(conn, "SELECT symbol, asset, qty, avg_entry_px, market_px, "
                                 "unrealized_pnl, updated_ts FROM broker_positions "
                                 "ORDER BY symbol")
        # fills stores no symbol; the order carries the resolved ticker.
        fills = _rows(conn, "SELECT f.namespace AS namespace, bo.ticker AS symbol, "
                            "f.side, f.qty, f.limit_px, f.fill_px, f.slippage_bps, "
                            "f.fill_ts, f.venue, f.client_order_id "
                            "FROM fills f LEFT JOIN broker_orders bo "
                            "ON f.client_order_id = bo.client_order_id "
                            "WHERE f.venue='alpaca-paper' "
                            "ORDER BY f.fill_ts DESC LIMIT 400")
        nav = _rows(conn, "SELECT study, date, nav, nav_after_tax, bench, n_open "
                          "FROM paper_nav WHERE study IN (?,?,?) ORDER BY date ASC",
                    (pbroker.NAV_BROKER, pbook.NAV_LAB, pbook.NAV_COMBINED))
        meta = _meta(conn)
    finally:
        conn.close()

    curves: dict[str, list[dict]] = {}
    for r in nav:
        curves.setdefault(r["study"], []).append(
            {k: r[k] for k in ("date", "nav", "nav_after_tax", "bench", "n_open")})
    broker_curve = curves.get(pbroker.NAV_BROKER, [])

    # fills lack the alpaca symbol column (fills stores the intent ticker); the
    # order table carries the resolved broker symbol, so tag live positions with
    # their originating namespace/source for the per-source view.
    sym_src = {pbroker._broker_symbol(o["ticker"]): (o["namespace"], o["source"])
               for o in orders if o.get("ticker")}
    for p in bpositions:
        ns, src = sym_src.get(p["symbol"], (None, None))
        p["namespace"], p["source"] = ns, src

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    last_rec = _fnum(meta.get("broker_last_reconcile"))
    reconcile_age_min = round((now_ms - last_rec) / 60_000, 1) if last_rec else None
    equity = _fnum(meta.get("broker_last_equity"))
    day_pnl = _fnum(meta.get("broker_day_pnl"))
    last_curve = broker_curve[-1] if broker_curve else None
    enabled = bool(cfg.broker_active) or bool(broker_curve) or equity is not None
    degraded = (not last_rec) or (reconcile_age_min is not None
                                  and reconcile_age_min > _DEGRADED_AFTER_MIN)

    account = None
    if enabled:
        account = {
            "enabled": True,
            "source": "alpaca-paper",
            "equity": equity,
            "day_pnl": day_pnl,
            "nav": last_curve.get("nav") if last_curve else None,
            "bench": last_curve.get("bench") if last_curve else None,
            "as_of": last_curve.get("date") if last_curve else None,
            "n_open": last_curve.get("n_open") if last_curve else len(bpositions),
            "reconcile_age_min": reconcile_age_min,
            "degraded": degraded,
        }

    slips = [f["slippage_bps"] for f in fills if f.get("slippage_bps") is not None]
    adv_capped = sum(1 for o in orders if o.get("reject_reason") == "adv_capped")

    return {
        "account": account,
        "positions": bpositions,
        "orders": orders,
        "fills": fills,
        "curves": curves,
        "research": {
            "lab": _curve_summary(curves.get(pbook.NAV_LAB, [])),
            "combined": _curve_summary(curves.get(pbook.NAV_COMBINED, [])),
        },
        "execution_delta": {
            "mean_slippage_bps": round(sum(slips) / len(slips), 2) if slips else None,
            "n_fills": len(fills),
            "adv_capped": adv_capped,
        },
        "note": ("@broker is the LIVE Alpaca paper account — real fills and real "
                 "slippage. @lab is the deterministic research curve and the ONLY "
                 "meta-gate evidence (after tax vs SPY); it is NOT the live "
                 "account. The @broker-vs-@combined gap is real execution cost."),
    }

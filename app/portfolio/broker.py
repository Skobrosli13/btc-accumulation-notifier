"""Live Alpaca **paper** broker track (dual-track §7) — the account the redesign
centers on.

A SECOND execution track running parallel to the deterministic replay book. It
reads the SAME signal intents (the ``paper_positions`` PENDING rows the replay
files) but submits REAL Alpaca paper limit orders and reconciles the async fills
into its own tables (``broker_orders``/``broker_positions``/``fills``) and its own
``paper_nav`` namespace ``@broker``. It NEVER writes back to ``paper_positions``,
so the replay curves (``@lab`` meta-gate evidence, ``@combined``) stay byte-for-
byte reproducible — adding the live broker cannot change which lab fills happened.

Honesty / safety invariants:
  - **Never fabricate market data.** Every network call goes through the fail-soft
    ``_http`` helpers (return None on failure); ``@broker`` NAV advances only from
    real account equity — a failed reconcile writes no NAV row and flags degraded.
  - **Paper-only.** The client refuses any base host that isn't
    ``paper-api.alpaca.markets`` — it can never route to a live account.
  - **Idempotent.** A deterministic ``client_order_id`` dedupes re-runs; Alpaca
    also rejects a duplicate client id, so a retried submit can't double-order.
  - **No zombies.** Equity orders are TIF=day, crypto TIF=ioc — an unfilled order
    auto-expires into an honest ``no_fill`` rather than lingering across sessions.

The live track deliberately sizes every intent on **vol-parity under the 2% cap**
(``sizing.position_size(expectancy=None)``): validated edge-sizing is expressed in
the reproducible ``@lab`` replay curve (the meta-gate), not in the live account.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

from ..sources import _http
from . import liquidity, sizing
from .book import _iso, _sign, _vol60

log = logging.getLogger(__name__)

PAPER_HOST = "paper-api.alpaca.markets"
NAV_BROKER = "@broker"                     # live paper account curve (NOT meta-gate)
_CRYPTO = {"BTC": "BTC/USD", "ETH": "ETH/USD"}
# Terminal Alpaca order states — reconcile stops polling these.
_TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day", "no_fill"}


def _now_ms(now_ms: int | None) -> int:
    return now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)


def _is_crypto(ticker: str) -> bool:
    return "/" in ticker or ticker.upper() in _CRYPTO


def _broker_symbol(ticker: str) -> str:
    return _CRYPTO.get(ticker.upper(), ticker)


def client_order_id(study: str, ticker: str, event_ts: int, side: str) -> str:
    """Deterministic dedup key for one intent — stable across re-runs so a submit
    is idempotent (and Alpaca rejects a duplicate id as a second guard)."""
    raw = f"{study}|{ticker}|{int(event_ts)}|{side.upper()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


class AlpacaPaper:
    """Thin fail-soft client over the Alpaca **paper** trading API. Network is
    isolated here so orchestration is testable with a fake."""

    def __init__(self, key: str, secret: str, *, base: str = f"https://{PAPER_HOST}"):
        host = (urlparse(base).hostname or "").lower()
        if host != PAPER_HOST:
            raise ValueError(
                f"AlpacaPaper refuses non-paper host {host!r}; the live track is "
                f"paper-only ({PAPER_HOST}).")
        self._base = base.rstrip("/")
        self._h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    def clock(self) -> dict | None:
        return _http.get_json(f"{self._base}/v2/clock", headers=self._h)

    def account(self) -> dict | None:
        return _http.get_json(f"{self._base}/v2/account", headers=self._h)

    def positions(self) -> list[dict]:
        return _http.get_json(f"{self._base}/v2/positions", headers=self._h) or []

    def get_order_by_coid(self, coid: str) -> dict | None:
        return _http.get_json(f"{self._base}/v2/orders:by_client_order_id",
                              params={"client_order_id": coid}, headers=self._h)

    def submit_limit(self, *, client_order_id: str, symbol: str, side: str,
                     qty: float, limit_px: float, tif: str = "day") -> dict | None:
        body = {"symbol": symbol, "side": side.lower(), "type": "limit",
                "qty": str(qty), "limit_price": str(round(limit_px, 2)),
                "time_in_force": tif, "client_order_id": client_order_id}
        return _http.post_json(f"{self._base}/v2/orders", json_body=body,
                               headers=self._h)


# --- orchestration (DB-pure: network is confined to the injected ``api``) --------

def submit_pending(conn: sqlite3.Connection, cfg, api: AlpacaPaper, *,
                   ref_px: dict[str, float], adv_bars: dict[str, list[dict]],
                   now_ms: int | None = None) -> dict:
    """Submit a real paper limit order for each new PENDING intent.

    ``ref_px`` is the reference (last close) per ticker; ``adv_bars`` the volume
    history for the ADV cap. Skips intents already in ``broker_orders`` (dedup),
    unpriced names, closed equity sessions, and unsizeable/ADV-unpriced orders —
    each an honest no-op that retries next run (no row is written)."""
    now_ms = _now_ms(now_ms)
    stats = {"submitted": 0, "skipped": 0, "capped": 0, "market_closed": 0}
    if not cfg.broker_active:
        return stats

    clock = api.clock() or {}
    eq_open = bool(clock.get("is_open"))
    acct = api.account() or {}
    try:
        equity = float(acct.get("equity")) if acct.get("equity") is not None else None
    except (TypeError, ValueError):
        equity = None
    if not equity or equity <= 0:
        log.warning("broker.submit_pending: no account equity; skipping run")
        return stats

    n_live = conn.execute(
        "SELECT count(*) FROM broker_orders WHERE status NOT IN "
        "('canceled','expired','rejected','no_fill')").fetchone()[0]
    have = {r[0] for r in conn.execute("SELECT client_order_id FROM broker_orders")}

    pendings = [dict(r) for r in conn.execute(
        "SELECT * FROM paper_positions WHERE status='PENDING' ORDER BY event_ts")]
    for r in pendings:
        ticker = r["ticker"]
        side = "sell" if _sign(r.get("direction")) < 0 else "buy"
        coid = client_order_id(r["study"], ticker, r["event_ts"], side)
        if coid in have:
            continue
        crypto = _is_crypto(ticker)
        if not crypto and not eq_open:
            stats["market_closed"] += 1
            continue                          # retry when the session opens
        ref = ref_px.get(ticker)
        if not ref or ref <= 0:
            stats["skipped"] += 1
            continue                          # unpriced ⇒ no order this run

        bars = adv_bars.get(ticker, [])
        vol = _vol60(bars, len(bars) - 1) if len(bars) >= 2 else None
        frac, basis = sizing.position_size(
            asset_vol_annual=vol or 0.0, n_concurrent=n_live + 1,
            expectancy=None, variance=None)   # live track: vol-parity sizing only
        if frac <= 0:
            stats["skipped"] += 1
            continue

        lim = liquidity.limit_price(ref, side, bps=cfg.broker_limit_bps)
        desired = frac * equity / ref
        adv_cap = liquidity.adv_share_cap(
            bars, participation=cfg.broker_adv_participation) if not crypto else None
        if crypto:
            qty, reason = round(desired, 6), ""
        else:
            qty, reason = liquidity.cap_order_shares(
                desired, bars, participation=cfg.broker_adv_participation)
            qty = float(int(qty))             # equities: whole shares
        if reason == "adv_unpriced" or qty <= 0:
            stats["skipped"] += 1
            continue
        capped = reason == "adv_capped"
        if capped:
            stats["capped"] += 1

        tif = "ioc" if crypto else "day"
        resp = api.submit_limit(client_order_id=coid, symbol=_broker_symbol(ticker),
                                side=side, qty=qty, limit_px=lim, tif=tif)
        if not resp:                          # broker down ⇒ retry next run (idempotent)
            stats["skipped"] += 1
            continue
        # reject_reason doubles as the size-note: 'adv_capped' records that the ADV
        # ceiling reduced the order (target_qty is floored to whole shares, so it
        # won't exactly equal adv_cap_shares — the note is the reliable signal).
        conn.execute(
            "INSERT OR IGNORE INTO broker_orders "
            "(client_order_id, broker_order_id, intent_id, namespace, source, "
            " ticker, asset, side, target_qty, limit_px, tif, adv_cap_shares, "
            " sizing_fraction, sizing_basis, status, reject_reason, submitted_ts, "
            " updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (coid, resp.get("id"), r["id"], r["study"], r.get("source") or "lab",
             ticker, "CRYPTO" if crypto else "EQ", side, qty, lim, tif, adv_cap,
             frac, basis, resp.get("status") or "submitted",
             "adv_capped" if capped else None, now_ms, now_ms))
        n_live += 1
        stats["submitted"] += 1
    conn.commit()
    return stats


def reconcile(conn: sqlite3.Connection, cfg, api: AlpacaPaper, *,
              now_ms: int | None = None) -> dict:
    """Poll every non-terminal order, book fills into ``fills`` (dedup by
    client_order_id) and refresh the ``broker_positions`` snapshot."""
    now_ms = _now_ms(now_ms)
    stats = {"updated": 0, "filled": 0}
    if not cfg.broker_active:
        return stats

    open_orders = [dict(r) for r in conn.execute(
        "SELECT * FROM broker_orders WHERE status NOT IN "
        "('filled','canceled','expired','rejected','no_fill')")]
    have_fill = {r[0] for r in conn.execute(
        "SELECT client_order_id FROM fills WHERE client_order_id IS NOT NULL")}
    for o in open_orders:
        od = api.get_order_by_coid(o["client_order_id"])
        if od is None:
            continue
        status = od.get("status") or o["status"]
        conn.execute("UPDATE broker_orders SET status=?, broker_order_id=?, "
                     "updated_ts=? WHERE client_order_id=?",
                     (status, od.get("id") or o.get("broker_order_id"), now_ms,
                      o["client_order_id"]))
        stats["updated"] += 1
        try:
            filled_qty = float(od.get("filled_qty") or 0.0)
            avg = float(od.get("filled_avg_price")) if od.get("filled_avg_price") else None
        except (TypeError, ValueError):
            filled_qty, avg = 0.0, None
        if filled_qty > 0 and avg and o["client_order_id"] not in have_fill:
            lim = o.get("limit_px")
            slip = round((avg / lim - 1.0) * 10_000, 2) if lim else None
            conn.execute(
                "INSERT INTO fills (event_id, asset, side, qty, limit_px, fill_px, "
                " fill_ts, venue, slippage_bps, client_order_id, broker_fill_id, "
                " namespace) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (o.get("intent_id"), o.get("asset"), o.get("side"), filled_qty,
                 lim, avg, now_ms, "alpaca-paper", slip, o["client_order_id"],
                 od.get("id"), o.get("namespace")))
            have_fill.add(o["client_order_id"])
            stats["filled"] += 1

    # Refresh the position snapshot (delete-and-replace: it mirrors the broker).
    snap = api.positions()
    conn.execute("DELETE FROM broker_positions")
    for p in snap:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO broker_positions "
                "(symbol, asset, qty, avg_entry_px, market_px, unrealized_pnl, updated_ts) "
                "VALUES (?,?,?,?,?,?,?)",
                (p.get("symbol"),
                 "CRYPTO" if _is_crypto(p.get("symbol") or "") else "EQ",
                 float(p.get("qty") or 0.0), float(p.get("avg_entry_price") or 0.0),
                 float(p.get("current_price") or 0.0),
                 float(p.get("unrealized_pl") or 0.0), now_ms))
        except (TypeError, ValueError):
            continue
    conn.execute("INSERT OR REPLACE INTO lab_meta (key, value) VALUES ('broker_last_reconcile', ?)",
                 (str(now_ms),))
    conn.commit()
    return stats


def _bench_at(bench_bars: list[dict], ts_ms: int) -> float | None:
    """Latest SPY close at/just before ``ts_ms`` (bench_bars oldest→newest)."""
    prev = None
    for b in bench_bars:
        if b["ts"] <= ts_ms:
            prev = b["close"]
        else:
            break
    return prev


def mark_broker_nav(conn: sqlite3.Connection, cfg, api: AlpacaPaper,
                    bench_bars: list[dict], *, now_ms: int | None = None) -> int:
    """Append today's ``@broker`` NAV row from real account equity.

    Anti-backfill: the first successful mark stamps ``broker_go_live_equity`` and
    starts the curve at NAV 1.0 — Alpaca's portfolio history is deliberately NOT
    replayed (same honesty rule as bridge.go_live_ts: no manufactured curve). A
    failed account read writes NO row and returns 0 (degrades, never fabricates).
    Unlike the replay marks this is incremental — a live account's past equity
    can't be recomputed."""
    now_ms = _now_ms(now_ms)
    if not cfg.broker_active:
        return 0
    acct = api.account()
    try:
        equity = float(acct["equity"]) if acct and acct.get("equity") is not None else None
        last_eq = float(acct.get("last_equity")) if acct and acct.get("last_equity") else None
    except (TypeError, ValueError):
        equity = last_eq = None
    if not equity or equity <= 0:
        log.warning("broker.mark_broker_nav: no account equity; no NAV row written")
        return 0

    row = conn.execute("SELECT value FROM lab_meta WHERE key='broker_go_live_equity'").fetchone()
    if row is None:
        conn.execute("INSERT OR REPLACE INTO lab_meta (key, value) VALUES ('broker_go_live_equity', ?)",
                     (str(equity),))
        conn.execute("INSERT OR REPLACE INTO lab_meta (key, value) VALUES ('broker_go_live_ts', ?)",
                     (str(now_ms),))
        go_live_eq, go_live_ts = equity, now_ms
    else:
        go_live_eq = float(row[0])
        gts = conn.execute("SELECT value FROM lab_meta WHERE key='broker_go_live_ts'").fetchone()
        go_live_ts = int(gts[0]) if gts else now_ms

    nav = equity / go_live_eq if go_live_eq else 1.0
    b0 = _bench_at(bench_bars, go_live_ts)
    b_now = _bench_at(bench_bars, now_ms)
    bench = (b_now / b0) if (b0 and b_now) else None
    n_open = conn.execute("SELECT count(*) FROM broker_positions").fetchone()[0]
    # @broker is the live account value (pre-tax); the after-tax meta-gate curve
    # is @lab. nav_after_tax mirrors nav here so curve consumers stay uniform.
    conn.execute(
        "INSERT OR REPLACE INTO paper_nav (study, date, nav, nav_after_tax, bench, n_open) "
        "VALUES (?,?,?,?,?,?)", (NAV_BROKER, _iso(now_ms), nav, nav, bench, n_open))
    conn.execute("INSERT OR REPLACE INTO lab_meta (key, value) VALUES ('broker_last_equity', ?)",
                 (str(equity),))
    if last_eq:
        conn.execute("INSERT OR REPLACE INTO lab_meta (key, value) VALUES ('broker_day_pnl', ?)",
                     (str(equity - last_eq),))
    conn.commit()
    return 1

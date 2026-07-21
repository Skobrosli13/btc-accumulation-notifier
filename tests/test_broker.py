"""Live Alpaca paper-broker track (app/portfolio/broker.py) — a FakeAlpaca is
injected so no test ever touches a network/live API. Asserts the dual-track
honesty invariants: deterministic dedup, ADV + 10bps caps, market-hours gating,
fills reconciliation, and degrade-not-fabricate on a broker outage."""
from __future__ import annotations

import sqlite3

import pytest

from app.harness import schema
from app.portfolio import broker
from tests.factories import make_config

DAY = 86_400_000


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    schema.init_harness_db(c)
    return c


def _cfg(**over):
    base = dict(broker_paper_enabled=True, alpaca_api_key="k", alpaca_secret_key="s",
                broker_adv_participation=0.01, broker_limit_bps=10.0)
    base.update(over)
    return make_config(**base)


def _alt_bars(n=25, base=20.0, vol=100_000.0):
    """Alternating ±1% closes (real vol so vol-parity sizes a position) with a
    fixed share volume for the ADV cap."""
    out = []
    for k in range(n):
        close = base * (1.01 if k % 2 else 0.99)
        out.append({"ts": k * DAY, "open": base, "high": max(base, close),
                    "low": min(base, close), "close": close, "volume": vol})
    return out


def _pending(conn, ticker="AAA", *, direction="LONG", source="swing"):
    conn.execute(
        "INSERT INTO paper_positions (study, source, ticker, event_ts, direction, "
        "status, horizon_sessions, tier, sector) "
        "VALUES ('swing:pead_drift', ?, ?, ?, ?, 'PENDING', 10, 'small', 'Tech')",
        (source, ticker, 5 * DAY, direction))
    conn.commit()


class FakeAlpaca:
    def __init__(self, *, is_open=True, equity=100_000.0, last_equity=99_000.0):
        self.is_open, self.equity, self.last_equity = is_open, equity, last_equity
        self.submitted: list[dict] = []
        self._orders: dict[str, dict] = {}
        self._positions: list[dict] = []
        self.down = False

    def clock(self):
        return None if self.down else {"is_open": self.is_open}

    def account(self):
        if self.down or self.equity is None:
            return None
        return {"equity": self.equity, "last_equity": self.last_equity}

    def positions(self):
        return list(self._positions)

    def get_order_by_coid(self, coid):
        return self._orders.get(coid)

    def submit_limit(self, *, client_order_id, symbol, side, qty, limit_px, tif="day"):
        if self.down:
            return None
        self.submitted.append(dict(client_order_id=client_order_id, symbol=symbol,
                                   side=side, qty=qty, limit_px=limit_px, tif=tif))
        oid = "ord-" + client_order_id[:8]
        self._orders[client_order_id] = {"id": oid, "status": "accepted",
                                         "filled_qty": "0", "filled_avg_price": None}
        return {"id": oid, "status": "accepted"}

    def fill(self, coid, *, qty, avg):
        o = self._orders[coid]
        o.update(status="filled", filled_qty=str(qty), filled_avg_price=str(avg))


def test_host_guard_refuses_live():
    with pytest.raises(ValueError):
        broker.AlpacaPaper("k", "s", base="https://api.alpaca.markets")
    # the paper host is accepted.
    broker.AlpacaPaper("k", "s")


def test_client_order_id_is_deterministic():
    a = broker.client_order_id("swing:pead", "AAA", 5 * DAY, "buy")
    b = broker.client_order_id("swing:pead", "AAA", 5 * DAY, "BUY")
    assert a == b and len(a) == 32
    assert a != broker.client_order_id("swing:pead", "AAA", 5 * DAY, "sell")


def test_submit_dedups_and_caps_limit():
    conn, cfg, api = _conn(), _cfg(), FakeAlpaca()
    _pending(conn, "AAA", direction="LONG")
    bars = _alt_bars(vol=100_000.0)
    ref = bars[-1]["close"]
    st = broker.submit_pending(conn, cfg, api, ref_px={"AAA": ref},
                               adv_bars={"AAA": bars})
    assert st["submitted"] == 1 and len(api.submitted) == 1
    o = api.submitted[0]
    assert o["side"] == "buy" and o["tif"] == "day"
    assert o["limit_px"] == pytest.approx(ref * 1.001)     # buy crosses UP 10bps
    # re-run is idempotent: the intent is already in broker_orders.
    st2 = broker.submit_pending(conn, cfg, api, ref_px={"AAA": ref},
                                adv_bars={"AAA": bars})
    assert st2["submitted"] == 0 and len(api.submitted) == 1
    row = dict(conn.execute("SELECT * FROM broker_orders").fetchone())
    assert row["sizing_basis"] == "vol_parity_only" and row["status"] == "accepted"


def test_short_sells_and_adv_cap_binds():
    conn, cfg, api = _conn(), _cfg(), FakeAlpaca()
    _pending(conn, "BBB", direction="SHORT")
    thin = _alt_bars(vol=100.0)                             # ADV$ ~ 2000 -> cap ~1 share
    ref = thin[-1]["close"]
    st = broker.submit_pending(conn, cfg, api, ref_px={"BBB": ref},
                               adv_bars={"BBB": thin})
    assert st["submitted"] == 1 and st["capped"] == 1
    o = api.submitted[0]
    assert o["side"] == "sell"
    assert o["limit_px"] == pytest.approx(ref * 0.999)      # sell crosses DOWN 10bps
    row = dict(conn.execute("SELECT * FROM broker_orders").fetchone())
    assert row["reject_reason"] == "adv_capped"             # ADV ceiling bound the size
    assert row["target_qty"] <= row["adv_cap_shares"]       # floored under the cap


def test_market_closed_is_a_no_op_for_equities():
    conn, cfg, api = _conn(), _cfg(), FakeAlpaca(is_open=False)
    _pending(conn, "AAA")
    bars = _alt_bars()
    st = broker.submit_pending(conn, cfg, api, ref_px={"AAA": bars[-1]["close"]},
                               adv_bars={"AAA": bars})
    assert st["market_closed"] == 1 and st["submitted"] == 0
    assert conn.execute("SELECT count(*) FROM broker_orders").fetchone()[0] == 0


def test_broker_down_submit_degrades_without_rows():
    conn, cfg, api = _conn(), _cfg(), FakeAlpaca()
    api.down = True
    _pending(conn, "AAA")
    bars = _alt_bars()
    st = broker.submit_pending(conn, cfg, api, ref_px={"AAA": bars[-1]["close"]},
                               adv_bars={"AAA": bars})
    assert st["submitted"] == 0
    assert conn.execute("SELECT count(*) FROM broker_orders").fetchone()[0] == 0


def test_reconcile_books_fills_once_and_snapshots():
    conn, cfg, api = _conn(), _cfg(), FakeAlpaca()
    _pending(conn, "AAA")
    bars = _alt_bars()
    ref = bars[-1]["close"]
    broker.submit_pending(conn, cfg, api, ref_px={"AAA": ref}, adv_bars={"AAA": bars})
    coid = api.submitted[0]["client_order_id"]
    api.fill(coid, qty=5, avg=ref * 1.0005)                # filled between ref and limit
    api._positions = [{"symbol": "AAA", "qty": "5", "avg_entry_price": str(ref),
                       "current_price": str(ref * 1.01), "unrealized_pl": "2.5"}]
    st = broker.reconcile(conn, cfg, api)
    assert st["filled"] == 1
    fills = conn.execute("SELECT venue, slippage_bps FROM fills").fetchall()
    assert len(fills) == 1 and fills[0]["venue"] == "alpaca-paper"
    assert dict(conn.execute("SELECT * FROM broker_orders").fetchone())["status"] == "filled"
    assert conn.execute("SELECT count(*) FROM broker_positions").fetchone()[0] == 1
    # re-reconcile must not double-book the fill.
    broker.reconcile(conn, cfg, api)
    assert conn.execute("SELECT count(*) FROM fills").fetchone()[0] == 1


def test_mark_nav_anti_backfill_and_degrade():
    conn, cfg, api = _conn(), _cfg(), FakeAlpaca(equity=100_000.0)
    spy = [{"ts": k * DAY, "close": 400.0 + k} for k in range(30)]
    now = 20 * DAY
    assert broker.mark_broker_nav(conn, cfg, api, spy, now_ms=now) == 1
    row = dict(conn.execute("SELECT * FROM paper_nav WHERE study='@broker'").fetchone())
    assert row["nav"] == pytest.approx(1.0)                # first mark starts at 1.0
    go_live = conn.execute("SELECT value FROM lab_meta WHERE key='broker_go_live_equity'").fetchone()
    assert float(go_live[0]) == 100_000.0
    # equity grows -> NAV tracks it off the SAME go-live floor (not backfilled).
    api.equity = 110_000.0
    broker.mark_broker_nav(conn, cfg, api, spy, now_ms=21 * DAY)
    row2 = dict(conn.execute("SELECT nav FROM paper_nav WHERE study='@broker' "
                             "ORDER BY date DESC LIMIT 1").fetchone())
    assert row2["nav"] == pytest.approx(1.1)
    # broker outage: no equity -> NO row written (degrade, never fabricate).
    api.down = True
    n_before = conn.execute("SELECT count(*) FROM paper_nav WHERE study='@broker'").fetchone()[0]
    assert broker.mark_broker_nav(conn, cfg, api, spy, now_ms=22 * DAY) == 0
    assert conn.execute("SELECT count(*) FROM paper_nav WHERE study='@broker'").fetchone()[0] == n_before

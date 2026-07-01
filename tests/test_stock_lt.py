"""Tests for the long-term engine — fundamentals metrics + gate/rank/combine (pure)
plus the SPY-benchmarked holdings lifecycle (in-memory DB)."""
from __future__ import annotations

from datetime import datetime, timezone

from app import stock_fundamentals as F
from app import stock_lt_collect, stock_lt_store, stock_store, store
from app import stock_lt_scoring as S
from app.config import load_config

CFG = load_config()
DAY = 86_400_000

_STMT = {
    "revenues": "income_statement", "gross_profit": "income_statement",
    "operating_income_loss": "income_statement", "net_income_loss": "income_statement",
    "income_loss_from_continuing_operations_before_tax": "income_statement",
    "income_tax_expense_benefit": "income_statement",
    "diluted_earnings_per_share": "income_statement", "diluted_average_shares": "income_statement",
    "assets": "balance_sheet", "current_assets": "balance_sheet",
    "current_liabilities": "balance_sheet", "liabilities": "balance_sheet",
    "equity": "balance_sheet", "long_term_debt": "balance_sheet",
    "net_cash_flow_from_operating_activities": "cash_flow_statement",
}


def _period(fp="FY", **lines):
    fin = {"income_statement": {}, "balance_sheet": {}, "cash_flow_statement": {}}
    for k, v in lines.items():
        fin[_STMT[k]][k] = {"value": v}
    return {"fiscal_period": fp, "fiscal_year": "2025", "end_date": "2025-12-31", "financials": fin}


def _good_periods():
    # a clean, improving, profitable, buying-back company
    cur = _period(
        revenues=1000, gross_profit=500, operating_income_loss=200, net_income_loss=150,
        income_loss_from_continuing_operations_before_tax=190, income_tax_expense_benefit=40,
        diluted_earnings_per_share=1.5, diluted_average_shares=100,
        assets=1000, current_assets=400, current_liabilities=200, liabilities=400,
        equity=600, long_term_debt=100, net_cash_flow_from_operating_activities=180)
    prev = _period(
        revenues=900, gross_profit=430, operating_income_loss=160, net_income_loss=120,
        income_loss_from_continuing_operations_before_tax=150, income_tax_expense_benefit=30,
        diluted_earnings_per_share=1.15, diluted_average_shares=105,   # share count fell (buyback)
        assets=980, current_assets=360, current_liabilities=210, liabilities=430,
        equity=550, long_term_debt=130, net_cash_flow_from_operating_activities=150)
    return [cur, prev]


# --- fundamentals ------------------------------------------------------------

def test_compute_core_metrics():
    m = F.compute(_good_periods(), price=None, market_cap=3000, shares=100)
    assert abs(m["earnings_yield"] - 150 / 3000) < 1e-9      # E/P
    assert abs(m["roe"] - 150 / 600) < 1e-9                  # 25%
    assert abs(m["roa"] - 150 / 1000) < 1e-9
    assert abs(m["gross_profitability"] - 500 / 1000) < 1e-9  # Novy-Marx
    assert abs(m["operating_margin"] - 200 / 1000) < 1e-9
    assert abs(m["accruals"] - (150 - 180) / 1000) < 1e-9    # negative = cash-backed = good
    assert abs(m["asset_growth"] - (1000 - 980) / 980) < 1e-9
    # share count fell 105 -> 100 => positive buyback/shareholder yield
    assert m["shareholder_yield"] > 0


def test_piotroski_high_for_improving_company():
    m = F.compute(_good_periods(), price=None, market_cap=3000, shares=100)
    assert m["piotroski"]["score"] >= 8   # improving on nearly every axis


def test_altman_safe_for_healthy_company():
    m = F.compute(_good_periods(), price=None, market_cap=3000, shares=100)
    assert m["altman"]["band"] == "safe" and m["altman"]["z"] > 3


def test_compute_needs_two_periods():
    assert F.compute(_good_periods()[:1], price=10, market_cap=100) is None


def test_roic_tax_guard_loss_year():
    # tax-benefit year (negative tax on positive-then-negative pretax) must not flip NOPAT:
    # raw tax_rate would be negative/>1; the clamp keeps ROIC sane and positive.
    p = _good_periods()
    p[0]["financials"]["income_statement"]["income_tax_expense_benefit"] = {"value": -80}
    p[0]["financials"]["income_statement"]["income_loss_from_continuing_operations_before_tax"] = {"value": 200}
    m = F.compute(p, price=None, market_cap=3000, shares=100)
    assert m["roic"] is not None and m["roic"] > 0   # not distorted negative by tax<0


def test_negative_equity_roe_none_safe():
    p = _good_periods()
    p[0]["financials"]["balance_sheet"]["equity"] = {"value": 0}   # div-by-zero guard
    m = F.compute(p, price=None, market_cap=3000, shares=100)
    assert m["roe"] is None   # no crash


# --- scoring: gate -----------------------------------------------------------

def _cand(**over):
    m = F.compute(_good_periods(), price=None, market_cap=3000, shares=100)
    base = {"ticker": "GOOD", "sector": "Technology", "metrics": m,
            "momentum_12_1": 0.20, "above_200dma": True, "price": 30.0}
    base.update(over)
    return base


def test_gate_passes_quality_and_fails_traps():
    ok, fails = S.gate(_cand())
    assert ok and not fails
    # value trap: below 200DMA + negative momentum
    ok2, fails2 = S.gate(_cand(above_200dma=False, momentum_12_1=-0.1))
    assert not ok2 and "below_200dma" in fails2 and "negative_momentum" in fails2


def test_gate_fails_low_piotroski():
    c = _cand()
    c["metrics"]["piotroski"]["score"] = 3
    ok, fails = S.gate(c)
    assert not ok and "piotroski<5" in fails


def test_gate_fails_distress():
    c = _cand()
    c["metrics"]["altman"] = {"z": 1.0, "band": "distress"}
    ok, fails = S.gate(c)
    assert not ok and "altman_distress" in fails


# --- scoring: rank + combine -------------------------------------------------

def test_percentiles_ordering():
    p = S._percentiles([10, 20, 30], higher_better=True)
    assert p[2] == 1.0 and p[0] == 0.0        # 30 best, 10 worst
    p2 = S._percentiles([10, 20, 30], higher_better=False)
    assert p2[0] == 1.0 and p2[2] == 0.0      # lower better -> 10 best


def test_rank_long_buys_sorts_and_gates():
    good = _cand(ticker="A")
    trap = _cand(ticker="B", above_200dma=False, momentum_12_1=-0.2)
    survivors, gated = S.rank_long_buys([good, trap])
    assert [c["ticker"] for c in survivors] == ["A"]
    assert [c["ticker"] for c in gated] == ["B"]
    assert 0 <= survivors[0]["conviction"] <= 100
    assert "value_rank" in survivors[0] and "quality_rank" in survivors[0]


def test_fair_value_band_cheap_vs_sector():
    c = _cand()  # earnings_yield = 150/3000 = 5%
    fv = S.fair_value_band(c, sector_median_ey=0.025)   # sector median 2.5% -> name cheaper
    assert fv and fv["fair_value"] > c["price"] and fv["discount_pct"] > 0


# --- holdings lifecycle (SPY-benchmarked forward-test) -------------------------

def _conn_mem():
    conn = store.connect(":memory:")
    store.init_db(conn)
    stock_store.init_stock_db(conn)
    stock_lt_store.init_stock_lt_db(conn)
    return conn


_NOW = datetime.now(timezone.utc)


def test_close_dropped_by_conviction_uses_date_matched_spy():
    conn = _conn_mem()
    t = 100 * DAY
    stock_lt_store.open_lt_holding(conn, ticker="AAA", opened_run_ts="r0", opened_ts=t,
                                   entry=100.0, spy_entry=400.0, conviction=80)
    # AAA is still scored (in candidates) but fell off the conviction list
    candidates = [{"ticker": "AAA", "price": 110.0, "last_ts": t + 30 * DAY}]
    spy_by_ts = {t + 30 * DAY: 440.0}
    opened, deferred = stock_lt_collect._manage_holdings(
        conn, CFG, "r1", set(), [], candidates, 440.0, spy_by_ts, _NOW, False)
    assert opened == [] and deferred == []
    closed = stock_lt_store.closed_lt_holdings(conn)
    assert len(closed) == 1
    h = closed[0]
    assert h["exit_reason"] == "dropped_by_conviction"
    assert h["spy_exit"] == 440.0
    assert abs(h["excess_return"] - (0.10 - 0.10)) < 1e-9   # +10% vs SPY +10%
    conn.close()


def test_close_deferred_when_no_date_matched_pair():
    conn = _conn_mem()
    t_old, t_new = 100 * DAY, 130 * DAY
    stock_lt_store.open_lt_holding(conn, ticker="BBB", opened_run_ts="r0", opened_ts=t_old,
                                   entry=50.0, spy_entry=400.0, conviction=70)
    # BBB vanished from the scorable set; its stored bar is weeks older than SPY's
    stock_store.upsert_prices(conn, "BBB", [(t_old, 10, 11, 9, 48.0, 1e6)], source="test")
    opened, deferred = stock_lt_collect._manage_holdings(
        conn, CFG, "r1", set(), [], [], 440.0, {t_new: 440.0}, _NOW, False)
    assert deferred == ["BBB"]
    assert stock_lt_store.open_lt_holdings(conn)          # still open — no phantom exit
    assert stock_lt_store.closed_lt_holdings(conn) == []
    conn.close()


def test_close_data_gap_when_dates_match():
    conn = _conn_mem()
    t = 100 * DAY
    stock_lt_store.open_lt_holding(conn, ticker="CCC", opened_run_ts="r0", opened_ts=t,
                                   entry=50.0, spy_entry=400.0, conviction=70)
    # not scored this run (e.g. financials decode failure) but prices are current
    stock_store.upsert_prices(conn, "CCC", [(t + 30 * DAY, 54, 56, 53, 55.0, 1e6)], source="test")
    opened, deferred = stock_lt_collect._manage_holdings(
        conn, CFG, "r1", set(), [], [], 440.0, {t + 30 * DAY: 440.0}, _NOW, False)
    assert deferred == []
    closed = stock_lt_store.closed_lt_holdings(conn)
    assert len(closed) == 1 and closed[0]["exit_reason"] == "data_gap"
    assert closed[0]["exit"] == 55.0
    conn.close()


def test_close_reexpresses_entry_in_current_basis_after_split():
    conn = _conn_mem()
    t = 100 * DAY
    stock_lt_store.open_lt_holding(conn, ticker="EEE", opened_run_ts="r0", opened_ts=t,
                                   entry=100.0, spy_entry=400.0, conviction=70, entry_ts=t)
    # 10:1 split mid-hold: the stored series (current basis) shows the entry bar at 10
    stock_store.upsert_prices(conn, "EEE", [(t, 9.9, 10.1, 9.8, 10.0, 1e7),
                                            (t + 30 * DAY, 10.9, 11.2, 10.8, 11.0, 1e7)],
                              source="test")
    stock_lt_collect._manage_holdings(
        conn, CFG, "r1", set(), [], [], 440.0, {t + 30 * DAY: 440.0}, _NOW, False)
    closed = stock_lt_store.closed_lt_holdings(conn)
    assert len(closed) == 1
    # +10% vs SPY +10% -> ~0 excess, NOT the -89% a naive exit/entry would book
    assert abs(closed[0]["excess_return"]) < 1e-6
    conn.close()


def test_open_uses_date_matched_spy_entry():
    conn = _conn_mem()
    t = 200 * DAY
    survivors = [{"ticker": "DDD", "price": 30.0, "last_ts": t, "conviction": 90}]
    spy_by_ts = {t: 430.0}   # SPY close of the SAME bar date as the entry price
    opened, _ = stock_lt_collect._manage_holdings(
        conn, CFG, "r1", {"DDD"}, survivors, survivors, 440.0, spy_by_ts, _NOW, False)
    assert opened == ["DDD"]
    h = stock_lt_store.open_lt_holdings(conn)[0]
    assert h["spy_entry"] == 430.0 and h["entry"] == 30.0
    conn.close()

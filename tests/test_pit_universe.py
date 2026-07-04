"""PIT universe classification + snapshot, and the Shumway delisting policy."""
from __future__ import annotations

import pandas as pd

from app.data.equities import delisting, universe
from app.data_lake import Lake


def test_tier_boundaries():
    assert universe.classify_tier(40e6) is None        # below the $50M micro floor
    assert universe.classify_tier(50e6) == "micro"
    assert universe.classify_tier(299e6) == "micro"
    assert universe.classify_tier(300e6) == "small"
    assert universe.classify_tier(1.9e9) == "small"
    assert universe.classify_tier(2e9) == "mid"
    assert universe.classify_tier(9.9e9) == "mid"
    assert universe.classify_tier(10e9) == "large"
    assert universe.classify_tier(3e12) == "large"


def test_liquidity_floors_by_tier():
    # micro/small floor $1M, price >= $3
    assert universe.is_liquid(5.0, 2e6, "micro") is True
    assert universe.is_liquid(2.99, 2e6, "micro") is False       # price floor
    assert universe.is_liquid(5.0, 0.5e6, "small") is False      # dollar-vol floor
    # mid/large floor $5M
    assert universe.is_liquid(50.0, 4e6, "mid") is False
    assert universe.is_liquid(50.0, 6e6, "large") is True
    assert universe.is_liquid(50.0, 100e6, None) is False        # sub-micro -> out


def test_dollar_volume_20d_trailing_mean():
    closes = [10.0] * 25
    vols = [100.0] * 25
    # only the last 20 sessions count -> 10*100 = 1000
    assert universe.dollar_volume_20d(closes, vols) == 1000.0
    assert universe.dollar_volume_20d([], []) is None


def test_build_snapshot_includes_since_delisted_name():
    # As of a past date, TWTR (since delisted) had live data -> it is IN that
    # snapshot; a sub-micro shell and an excluded name are OUT.
    rows = [
        {"permaticker": 1, "ticker": "TWTR", "sector": "Tech",
         "mcap_usd": 30e9, "price": 53.0, "dollar_vol_20d": 500e6},
        {"permaticker": 2, "ticker": "SHELL", "sector": "Fin",
         "mcap_usd": 20e6, "price": 4.0, "dollar_vol_20d": 2e6},     # sub-micro -> out
        {"permaticker": 3, "ticker": "SECRET", "sector": "Tech",
         "mcap_usd": 5e9, "price": 40.0, "dollar_vol_20d": 50e6},    # excluded -> out
    ]
    snap = universe.build_snapshot("2022-06-30", rows, excluded_tickers={"SECRET"})
    by_ticker = {r["ticker"]: r for r in snap}
    assert by_ticker["TWTR"]["tier"] == "large" and by_ticker["TWTR"]["included"] is True
    assert by_ticker["SHELL"]["tier"] is None and by_ticker["SHELL"]["included"] is False
    assert by_ticker["SECRET"]["excluded"] is True and by_ticker["SECRET"]["included"] is False
    assert all(r["date"] == "2022-06-30" for r in snap)


def test_build_from_lake_joins_tickers_daily_sep(tmp_path):
    lake = Lake(tmp_path / "lake")
    assert universe.build_from_lake(lake, "2026-01-10") == []      # nothing ingested
    lake.write("tickers", pd.DataFrame({
        "table": ["SEP", "SF1", "SEP", "SEP"],
        "permaticker": [1, 1, 2, 3],
        "ticker": ["AAPL", "AAPL", "PENNY", "FUND"],
        "sector": ["Tech", "Tech", "Fin", None],
        "category": ["Domestic Common Stock", "Domestic Common Stock",
                     "Domestic Common Stock", "ETF"]}))     # FUND excluded (not common)
    lake.write("daily", pd.DataFrame({
        "ticker": ["AAPL", "AAPL", "PENNY"],
        "date": ["2026-01-05", "2026-01-09", "2026-01-09"],
        "marketcap": [3_900_000.0, 4_000_000.0, 10.0]}))     # $M: AAPL $4T, PENNY $10M
    lake.write("sep", pd.DataFrame({
        "ticker": ["AAPL", "AAPL", "PENNY"],
        "date": ["2026-01-08", "2026-01-09", "2026-01-09"],
        "close": [199.0, 200.0, 4.0], "volume": [1_000_000.0, 1_000_000.0, 1000.0]}))
    snap = universe.build_from_lake(lake, "2026-01-10")
    by = {r["ticker"]: r for r in snap}
    assert "FUND" not in by                                   # non-common filtered out
    assert by["AAPL"]["tier"] == "large" and by["AAPL"]["included"] is True
    assert by["AAPL"]["price"] == 200.0                       # latest close in the window
    assert by["PENNY"]["tier"] is None and by["PENNY"]["included"] is False  # sub-micro


def test_build_from_lake_stale_names_fall_out_of_later_snapshots(tmp_path):
    """Regression for the M1-acceptance FAIL: a delisted name's frozen final bars
    must not keep it 'included' in snapshots after its delisting (TWTR was still
    in the 2026 universe at its Oct-2022 acquisition price)."""
    lake = Lake(tmp_path / "lake")
    lake.write("tickers", pd.DataFrame({
        "table": ["SEP", "SEP"], "permaticker": [1, 2],
        "ticker": ["LIVE", "DEAD"], "sector": ["Tech", "Tech"],
        "category": ["Domestic Common Stock"] * 2}))
    lake.write("daily", pd.DataFrame({
        "ticker": ["LIVE", "DEAD"],
        "date": ["2026-06-29", "2022-10-27"],          # DEAD's last mcap = delisting day
        "marketcap": [50_000.0, 40_000.0]}))
    lake.write("sep", pd.DataFrame({
        "ticker": ["LIVE", "LIVE", "DEAD", "DEAD"],
        "date": ["2026-06-26", "2026-06-29", "2022-10-26", "2022-10-27"],
        "close": [100.0, 101.0, 53.0, 53.7],
        "volume": [1e6, 1e6, 1e8, 1e8]}))
    # As of mid-2026: only LIVE. DEAD's 2022 bars must NOT resurrect it.
    snap_2026 = {r["ticker"] for r in universe.build_from_lake(lake, "2026-06-30")}
    assert snap_2026 == {"LIVE"}
    # As of its own era, DEAD was alive -> in the snapshot (PIT semantics).
    snap_2022 = {r["ticker"] for r in universe.build_from_lake(lake, "2022-10-28")}
    assert "DEAD" in snap_2022


# --- delisting-return policy (Shumway) ---------------------------------------

def test_delisting_merger_closes_at_final_price():
    # TWTR: delisted + acquisitionby (cash M&A) -> exit at final price, no shock.
    acts = ["delisted", "acquisitionby"]
    assert delisting.is_merger(acts) is True
    assert delisting.terminal_return(acts, final_return=0.02) == 0.02
    assert delisting.terminal_return(acts, final_return=None) == 0.0


def test_delisting_mergerto_is_a_merger_exit():
    """Regression for the M1-acceptance PARTIAL: Sharadar tags stock-mergers as
    'mergerto' (e.g. WRK 2024-07-05) with NO acquisitionby/of row — they must
    route as merger exits, never take the -30% performance haircut."""
    acts = ["delisted", "mergerto"]
    assert delisting.is_merger(acts) is True
    assert delisting.is_performance_delisting(acts) is False
    assert delisting.terminal_return(acts, final_return=None) == 0.0


def test_delisting_performance_missing_return_is_minus_30pct():
    # Plain regulatory delisting, no terminal return -> Shumway -30%.
    assert delisting.terminal_return(["delisted"], final_return=None) == -0.30
    # Bankruptcy liquidation likewise.
    assert delisting.terminal_return(["bankruptcyliquidation"], final_return=None) == -0.30
    # If a real terminal return exists, use it (not the haircut).
    assert delisting.terminal_return(["delisted"], final_return=-0.55) == -0.55
    assert delisting.is_performance_delisting(["delisted"]) is True
    assert delisting.is_performance_delisting(["delisted", "acquisitionof"]) is False

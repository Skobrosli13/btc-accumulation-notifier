"""PIT universe classification + snapshot, and the Shumway delisting policy."""
from __future__ import annotations

from app.data.equities import delisting, universe


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


# --- delisting-return policy (Shumway) ---------------------------------------

def test_delisting_merger_closes_at_final_price():
    # TWTR: delisted + acquisitionby (cash M&A) -> exit at final price, no shock.
    acts = ["delisted", "acquisitionby"]
    assert delisting.is_merger(acts) is True
    assert delisting.terminal_return(acts, final_return=0.02) == 0.02
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

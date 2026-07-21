"""ADV cap + marketable-limit cap (app/portfolio/liquidity.py) — pure."""
from __future__ import annotations

from app.portfolio import liquidity as liq


def _bars(closes, vols):
    return [{"close": c, "volume": v} for c, v in zip(closes, vols)]


def test_adv_dollar_needs_full_lookback():
    # 19 sessions < ADV_LOOKBACK (20) => cannot estimate.
    b = _bars([10.0] * 19, [1000] * 19)
    assert liq.adv_dollar(b) is None
    # exactly 20 priced+volumed sessions => mean dollar volume = 10 * 1000.
    b20 = _bars([10.0] * 20, [1000] * 20)
    assert liq.adv_dollar(b20) == 10_000.0


def test_adv_share_cap_and_limit_price():
    b = _bars([20.0] * 20, [100_000] * 20)          # ADV$ = 2,000,000
    cap = liq.adv_share_cap(b, participation=0.01)   # 1% -> $20,000 -> /20 = 1000 sh
    assert cap == 1000.0
    # buy crosses up, sell crosses down, both by 10 bps.
    assert liq.limit_price(100.0, "buy", bps=10) == 100.1
    assert liq.limit_price(100.0, "sell", bps=10) == 99.9
    assert liq.limit_price(100.0, "LONG") == 100.1


def test_cap_order_shares_paths():
    b = _bars([20.0] * 20, [100_000] * 20)          # cap = 1000 shares
    # under cap -> unchanged, no reason.
    assert liq.cap_order_shares(500, b) == (500, "")
    # over cap -> clamped, flagged.
    assert liq.cap_order_shares(5000, b) == (1000.0, "adv_capped")
    # no volume history -> unpriced -> skip (never an uncapped order).
    assert liq.cap_order_shares(500, _bars([20.0] * 5, [0] * 5)) == (0.0, "adv_unpriced")

"""Shared test factories."""
from __future__ import annotations

from datetime import date

from app.config import Config

_BASE = dict(
    ntfy_topic=None, ntfy_server="https://ntfy.sh", telegram_bot_token=None,
    telegram_chat_id=None,
    resend_api_key=None, email_from="onboarding@resend.dev", email_to=None,
    fred_api_key=None,
    coinalyze_api_key=None, coinalyze_symbol="BTCUSDT_PERP.A",
    flow_cvd_lookback=14, flow_liq_spike_mult=3.0,
    flow_oi_bar_surge_pct=3.0, flow_liq_min_usd=500_000.0,
    glassnode_api_key=None, cryptoquant_api_key=None,
    coinglass_api_key=None, sosovalue_api_key=None,
    onchain_free_enabled=True, oi_flush_window_hours=24.0,
    exchange="okx", symbol="BTC-USDT", db_path=":memory:",
    weights={"onchain": 0.35, "price": 0.20, "macro": 0.20, "sentiment": 0.10, "derivs": 0.15},
    tier_watch=40, tier_accumulate=60, tier_deepvalue=80, tier_hysteresis_margin=2.0,
    flash_fng_max=10, flash_drop_pct=10, flash_debounce_days=3,
    ath_date=date(2025, 10, 6), peak_to_trough_days=370, cycle_mult_swing=0.05,
    st_timeframes=("4h", "1d"), st_cooldown_hours=12,
    st_rsi_oversold=30, st_rsi_overbought=70, st_vol_spike_mult=2.0,
    st_funding_spike=0.0005, st_oi_surge_pct=10.0,
    st_buy_threshold=30, st_strong_buy_threshold=60,
    st_sell_threshold=-30, st_strong_sell_threshold=-60, st_regime_suppress=False,
    st_require_confluence=True,
    api_token=None, api_cors_origin=None,
    public_base_url="https://btc.example.com", watchdog_stale_hours=3,
)


def make_config(**over) -> Config:
    """A fully-populated Config for tests; override any field via kwargs."""
    base = dict(_BASE)
    base.update(over)
    return Config(**base)

"""Load and validate configuration from environment / .env.

The presence of optional API keys is what toggles the paid data layers — there
is no separate "enable" flag. Everything has a sane default so the app runs
end-to-end on free data with an empty .env (or no .env at all).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime

try:
    from dotenv import load_dotenv

    load_dotenv()  # loads .env from cwd if present; no-op otherwise
except ImportError:  # python-dotenv is a convenience, not a hard requirement
    pass


def _strip_inline_comment(value: str) -> str:
    """Drop a trailing inline comment.

    python-dotenv does not strip inline comments when the value is blank, so a
    line like ``KEY=    # note`` yields the comment as the value. Our config
    values never contain spaces, so we treat a leading ``#`` (after trim) as an
    empty value and cut anything from the first `` #`` (space-hash) onward. A
    ``#`` embedded in a value with no preceding space (e.g. a token) is kept.
    """
    if value.strip().startswith("#"):
        return ""
    idx = value.find(" #")
    if idx != -1:
        value = value[:idx]
    return value


def _get(name: str, default: str = "") -> str:
    return _strip_inline_comment(os.environ.get(name, default)).strip()


def _get_float(name: str, default: float) -> float:
    raw = _get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name).lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _opt(name: str) -> str | None:
    """An optional secret: empty string -> None, so callers test truthiness."""
    val = _get(name)
    return val or None


def _parse_date(raw: str, default: date) -> date:
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return default


@dataclass(frozen=True)
class Config:
    # Notifications
    ntfy_topic: str | None
    ntfy_server: str
    telegram_bot_token: str | None
    telegram_chat_id: str | None

    # Email (Resend) — primary channel after the pivot
    resend_api_key: str | None
    email_from: str
    email_to: str | None

    # Free data
    fred_api_key: str | None

    # Order-flow layer (Coinalyze — free, aggregated derivs incl. Binance:
    # OI / funding / liquidations / candle-CVD). Presence of the key activates it.
    coinalyze_api_key: str | None
    coinalyze_symbol: str          # Coinalyze market id (e.g. BTCUSDT_PERP.A = Binance perp)
    flow_cvd_lookback: int         # bars for CVD/price divergence on the primary ST timeframe
    flow_liq_spike_mult: float     # last-bar liquidations >= this x recent mean = a flush trigger
    flow_oi_bar_surge_pct: float   # abs SINGLE-BAR OI %change for the participant trigger (per-bar, not per-window)
    flow_liq_min_usd: float        # absolute $ floor for a flush (guards a near-zero quiet baseline)

    # Paid drop-ins (presence toggles the layer)
    glassnode_api_key: str | None
    cryptoquant_api_key: str | None
    coinglass_api_key: str | None
    sosovalue_api_key: str | None

    # Free on-chain default (bitcoin-data.com / BGeometrics) — on unless disabled.
    onchain_free_enabled: bool
    # Window for the free OKX-OI-derived oi_flush (long-term derivs), in hours.
    oi_flush_window_hours: float

    # Market data
    exchange: str          # okx (default) | kraken | binance (only from unrestricted regions)
    symbol: str            # spot symbol, e.g. BTC-USDT (long-term price structure)
    db_path: str

    # Long-term signal config
    weights: dict[str, float]
    tier_watch: float
    tier_accumulate: float
    tier_deepvalue: float
    tier_hysteresis_margin: float   # dead-band (composite pts) before a tier change sticks

    # Acute-capitulation flash
    flash_fng_max: float
    flash_drop_pct: float
    flash_debounce_days: int

    # Cycle context
    ath_date: date            # fallback only; the live ATH is derived from price history
    peak_to_trough_days: int
    cycle_mult_swing: float   # +/- swing of the cycle-timing multiplier (0 = timing off)

    # Short-term swing config
    st_timeframes: tuple[str, ...]   # e.g. ("4h", "1d")
    st_cooldown_hours: float         # min hours between repeats of the same trigger+timeframe
    st_rsi_oversold: float
    st_rsi_overbought: float
    st_vol_spike_mult: float
    st_funding_spike: float          # |funding| above this (8h fraction) = spike
    st_oi_surge_pct: float           # OI change over window above this % = surge
    st_buy_threshold: float          # st_composite >= this => BUY state
    st_strong_buy_threshold: float
    st_sell_threshold: float         # st_composite <= this => SELL state (negative)
    st_strong_sell_threshold: float
    st_regime_suppress: bool         # if true (default), drop alerts that fight the 200-day regime — also prevents opposing BUY+SELL alerts in the same run
    st_require_confluence: bool       # if true, a lone unaligned trigger won't alert

    # Dashboard read-only API
    api_token: str | None
    api_cors_origin: str | None

    # Public origin used to build links in outgoing email (the unsubscribe URL).
    public_base_url: str

    # Watchdog (dead-man's-switch)
    watchdog_stale_hours: float

    # --- Derived helpers -------------------------------------------------

    @property
    def onchain_active(self) -> bool:
        # On-chain valuation is available for free (bitcoin-data.com) by default,
        # so the layer is active unless the free feed is explicitly disabled and no
        # paid key is set. CryptoQuant is excluded (unwired stub — returns no data).
        # NOTE: this only drives health/messaging — scoring lights up purely from
        # onchain() returning real numbers.
        return bool(self.glassnode_api_key) or self.onchain_free_enabled

    @property
    def onchain_source(self) -> str | None:
        """Which provider onchain() will actually use — kept aligned with the
        precedence in app/sources/onchain.py so /api/health never lies."""
        if self.glassnode_api_key:
            return "glassnode"
        if self.onchain_free_enabled:
            return "bitcoin-data"
        return None

    @property
    def email_active(self) -> bool:
        return bool(self.resend_api_key and self.email_to)

    @property
    def derivs_paid_active(self) -> bool:
        return bool(self.coinglass_api_key)

    @property
    def coinalyze_active(self) -> bool:
        """The free order-flow layer (CVD / OI participant / liquidations) lights up
        purely from the Coinalyze key being present, like every other layer."""
        return bool(self.coinalyze_api_key)

    @property
    def macro_active(self) -> bool:
        return bool(self.fred_api_key)

    def notifications_configured(self) -> bool:
        return bool(
            self.email_active
            or self.ntfy_topic
            or (self.telegram_bot_token and self.telegram_chat_id)
        )


def _get_tuple(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _get(name)
    if not raw:
        return default
    parts = tuple(p.strip().lower() for p in raw.split(",") if p.strip())
    return parts or default


def load_config() -> Config:
    return Config(
        ntfy_topic=_opt("NTFY_TOPIC"),
        ntfy_server=_get("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
        telegram_bot_token=_opt("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_opt("TELEGRAM_CHAT_ID"),
        resend_api_key=_opt("RESEND_API_KEY"),
        email_from=_get("EMAIL_FROM", "onboarding@resend.dev"),
        email_to=_opt("EMAIL_TO"),
        fred_api_key=_opt("FRED_API_KEY"),
        coinalyze_api_key=_opt("COINALYZE_API_KEY"),
        coinalyze_symbol=_get("COINALYZE_SYMBOL", "BTCUSDT_PERP.A"),
        flow_cvd_lookback=_get_int("FLOW_CVD_LOOKBACK", 14),
        flow_liq_spike_mult=_get_float("FLOW_LIQ_SPIKE_MULT", 3.0),
        flow_oi_bar_surge_pct=_get_float("FLOW_OI_BAR_SURGE_PCT", 3.0),
        flow_liq_min_usd=_get_float("FLOW_LIQ_MIN_USD", 500_000.0),
        glassnode_api_key=_opt("GLASSNODE_API_KEY"),
        cryptoquant_api_key=_opt("CRYPTOQUANT_API_KEY"),
        coinglass_api_key=_opt("COINGLASS_API_KEY"),
        sosovalue_api_key=_opt("SOSOVALUE_API_KEY"),
        onchain_free_enabled=_get_bool("ONCHAIN_FREE", True),
        oi_flush_window_hours=_get_float("OI_FLUSH_WINDOW_HOURS", 24.0),
        exchange=_get("EXCHANGE", "okx").lower(),
        symbol=_get("SYMBOL", "BTC-USDT"),
        db_path=_get("DB_PATH", "./btc.db"),
        weights={
            "onchain": _get_float("W_ONCHAIN", 0.35),
            "price": _get_float("W_PRICE", 0.20),
            "macro": _get_float("W_MACRO", 0.20),
            "sentiment": _get_float("W_SENTIMENT", 0.10),
            "derivs": _get_float("W_DERIVS", 0.15),
        },
        tier_watch=_get_float("TIER_WATCH", 40),
        tier_accumulate=_get_float("TIER_ACCUMULATE", 60),
        tier_deepvalue=_get_float("TIER_DEEPVALUE", 80),
        tier_hysteresis_margin=_get_float("TIER_HYSTERESIS_MARGIN", 2.0),
        flash_fng_max=_get_float("FLASH_FNG_MAX", 10),
        flash_drop_pct=_get_float("FLASH_DROP_PCT", 10),
        flash_debounce_days=_get_int("FLASH_DEBOUNCE_DAYS", 3),
        ath_date=_parse_date(_get("ATH_DATE", "2025-10-06"), date(2025, 10, 6)),
        peak_to_trough_days=_get_int("PEAK_TO_TROUGH_DAYS", 370),
        cycle_mult_swing=_get_float("CYCLE_MULT_SWING", 0.05),
        st_timeframes=_get_tuple("ST_TIMEFRAMES", ("4h", "1d")),
        st_cooldown_hours=_get_float("ST_COOLDOWN_HOURS", 12),
        st_rsi_oversold=_get_float("ST_RSI_OVERSOLD", 30),
        st_rsi_overbought=_get_float("ST_RSI_OVERBOUGHT", 70),
        st_vol_spike_mult=_get_float("ST_VOL_SPIKE_MULT", 2.0),
        st_funding_spike=_get_float("ST_FUNDING_SPIKE", 0.0005),
        st_oi_surge_pct=_get_float("ST_OI_SURGE_PCT", 10.0),
        st_buy_threshold=_get_float("ST_BUY_THRESHOLD", 30),
        st_strong_buy_threshold=_get_float("ST_STRONG_BUY_THRESHOLD", 60),
        st_sell_threshold=_get_float("ST_SELL_THRESHOLD", -30),
        st_strong_sell_threshold=_get_float("ST_STRONG_SELL_THRESHOLD", -60),
        # Default ON: don't fight the 200-day regime, and (the reason this is the
        # default) it stops the collector from emitting an opposing BUY and SELL in
        # the same run — counter-regime triggers are dropped. Set =false to re-enable
        # two-sided swing alerts.
        st_regime_suppress=_get_bool("ST_REGIME_SUPPRESS", True),
        st_require_confluence=_get_bool("ST_REQUIRE_CONFLUENCE", True),
        api_token=_opt("API_TOKEN"),
        api_cors_origin=_opt("API_CORS_ORIGIN"),
        public_base_url=_get("PUBLIC_BASE_URL", "https://btc.riverviewweb.com").rstrip("/"),
        watchdog_stale_hours=_get_float("WATCHDOG_STALE_HOURS", 3),
    )

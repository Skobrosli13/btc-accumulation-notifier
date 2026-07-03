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


# --- Namespaced config groups (§0.5) ----------------------------------------
# Backward-compatible views over the flat Config below: the flat dataclass stays
# the single source of truth (every existing cfg.<field> call site, the loader
# and the test factory are untouched), and these typed groups are BUILT on demand
# from it via the cfg.core / cfg.btc / cfg.equity properties. New code (routers,
# harness, portfolio) reads cfg.btc.st_cooldown_hours etc.; nothing has to.

@dataclass(frozen=True)
class CoreConfig:
    """Asset-agnostic infra: notifications, DB, the API gate, watchdog, freshness."""
    db_path: str
    api_token: str | None
    api_cors_origin: str | None
    public_base_url: str
    watchdog_stale_hours: float
    freshness_budget_days: float
    ntfy_topic: str | None
    ntfy_server: str
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    resend_api_key: str | None
    email_from: str
    email_to: str | None


@dataclass(frozen=True)
class BtcConfig:
    """BTC/crypto: data-layer keys, long-term accumulation scoring, short-term swing."""
    # data layers
    fred_api_key: str | None
    coinalyze_api_key: str | None
    coinalyze_symbol: str
    flow_cvd_lookback: int
    flow_liq_spike_mult: float
    flow_oi_bar_surge_pct: float
    flow_liq_min_usd: float
    glassnode_api_key: str | None
    coinglass_api_key: str | None
    onchain_free_enabled: bool
    oi_flush_window_hours: float
    exchange: str
    symbol: str
    # long-term composite
    weights: dict[str, float]
    tier_watch: float
    tier_accumulate: float
    tier_deepvalue: float
    tier_hysteresis_margin: float
    flash_fng_max: float
    flash_drop_pct: float
    flash_debounce_days: int
    ath_date: date
    peak_to_trough_days: int
    cycle_mult_swing: float
    # short-term swing
    st_timeframes: tuple[str, ...]
    st_cooldown_hours: float
    st_rsi_oversold: float
    st_rsi_overbought: float
    st_vol_spike_mult: float
    st_funding_spike: float
    st_oi_surge_pct: float
    st_buy_threshold: float
    st_strong_buy_threshold: float
    st_sell_threshold: float
    st_strong_sell_threshold: float
    st_regime_suppress: bool
    st_require_confluence: bool


@dataclass(frozen=True)
class EquityConfig:
    """Equities: swing screener + long-term factor engine keys and knobs."""
    finnhub_api_key: str | None
    alpaca_api_key: str | None
    alpaca_secret_key: str | None
    tiingo_api_key: str | None
    massive_api_key: str | None
    sec_user_agent: str
    stock_insider_enabled: bool
    stock_universe_path: str
    stock_top_n: int
    stock_pead_lookback_days: int
    stock_pead_min_surprise: float
    stock_min_price: float
    stock_min_dollar_vol: float
    stock_atr_k_stop: float
    stock_atr_k_t1: float
    stock_atr_k_t2: float
    stock_time_stop_days: int
    stock_cooldown_days: float
    stock_allow_shorts: bool
    stock_cost_bps: float
    stock_lt_top_n: int
    stock_lt_min_dollar_vol: float


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

    # Order-flow layer (Coinalyze — free: OI / funding / liquidations / candle-CVD.
    # The default symbol BTCUSDT_PERP.A is the SINGLE-VENUE Binance perp, not a
    # cross-venue aggregate). Presence of the key activates it.
    coinalyze_api_key: str | None
    coinalyze_symbol: str          # Coinalyze market id (BTCUSDT_PERP.A = Binance perp, ONE venue)
    flow_cvd_lookback: int         # bars for CVD/price divergence on the primary ST timeframe
    flow_liq_spike_mult: float     # last-bar liquidations >= this x recent mean = a flush trigger
    flow_oi_bar_surge_pct: float   # abs SINGLE-BAR OI %change for the participant trigger (per-bar, not per-window)
    flow_liq_min_usd: float        # absolute $ floor for a flush (guards a near-zero quiet baseline)

    # Paid drop-ins (presence toggles the layer)
    glassnode_api_key: str | None
    coinglass_api_key: str | None

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

    # --- Stock swing tracker (second asset class) -----------------------
    # Optional with defaults so any external Config construction stays valid.
    # Free-tier by default: Yahoo/Stooq (prices), SEC EDGAR (insider) and FINRA
    # (short-vol) are keyless; Finnhub (a free key) lights up the scored PEAD
    # edge; Alpaca/Tiingo are optional price upgrades. Presence of a key toggles
    # the layer, exactly like the BTC side.
    finnhub_api_key: str | None = None
    alpaca_api_key: str | None = None
    alpaca_secret_key: str | None = None
    tiingo_api_key: str | None = None
    sec_user_agent: str = "riverviewweb-signal stock tracker admin@riverviewweb.com"
    stock_insider_enabled: bool = True
    stock_universe_path: str = "app/stock_universe.json"
    stock_top_n: int = 15
    stock_pead_lookback_days: int = 10
    stock_pead_min_surprise: float = 3.0
    stock_min_price: float = 5.0
    stock_min_dollar_vol: float = 5_000_000.0
    stock_atr_k_stop: float = 1.5
    stock_atr_k_t1: float = 1.5
    stock_atr_k_t2: float = 2.5
    stock_time_stop_days: int = 12
    stock_cooldown_days: float = 5.0
    stock_allow_shorts: bool = False   # Phase 1 = long-only; flip on for Phase 2 (negative-PEAD/short setups)
    stock_cost_bps: float = 10.0       # round-trip commission+slippage (bps) netted out of forward-test R
    # massive.com (Polygon-shaped): grouped-daily prices + full financials + reference.
    # Presence of the key upgrades prices to Massive (robust, 1 call/day) and lights
    # up the long-term fundamentals engine. Free tier: delayed EOD, 5 calls/min.
    massive_api_key: str | None = None
    stock_lt_top_n: int = 30           # long-buys conviction list size
    stock_lt_min_dollar_vol: float = 3_000_000.0  # liquidity floor for the LT universe

    # Freshness budget for DAILY-cadence sources (bitcoin-data /last, BGeometrics
    # files, Fear & Greed, hashrate): a dated reading older than this many days is
    # treated as missing (None) instead of scored as current, so a frozen upstream
    # renormalizes away (and flips active_cats) rather than silently pinning a
    # category at a stale level. .env: FRESHNESS_BUDGET_DAYS (default 3).
    freshness_budget_days: float = 3.0

    # --- Namespaced views (§0.5) -----------------------------------------
    # Typed groupings built from the flat fields above. Pure views (no state of
    # their own), so they can never drift from the source config.

    @property
    def core(self) -> CoreConfig:
        return CoreConfig(
            db_path=self.db_path, api_token=self.api_token,
            api_cors_origin=self.api_cors_origin, public_base_url=self.public_base_url,
            watchdog_stale_hours=self.watchdog_stale_hours,
            freshness_budget_days=self.freshness_budget_days,
            ntfy_topic=self.ntfy_topic, ntfy_server=self.ntfy_server,
            telegram_bot_token=self.telegram_bot_token, telegram_chat_id=self.telegram_chat_id,
            resend_api_key=self.resend_api_key, email_from=self.email_from,
            email_to=self.email_to)

    @property
    def btc(self) -> BtcConfig:
        return BtcConfig(
            fred_api_key=self.fred_api_key, coinalyze_api_key=self.coinalyze_api_key,
            coinalyze_symbol=self.coinalyze_symbol, flow_cvd_lookback=self.flow_cvd_lookback,
            flow_liq_spike_mult=self.flow_liq_spike_mult,
            flow_oi_bar_surge_pct=self.flow_oi_bar_surge_pct,
            flow_liq_min_usd=self.flow_liq_min_usd, glassnode_api_key=self.glassnode_api_key,
            coinglass_api_key=self.coinglass_api_key,
            onchain_free_enabled=self.onchain_free_enabled,
            oi_flush_window_hours=self.oi_flush_window_hours, exchange=self.exchange,
            symbol=self.symbol, weights=self.weights, tier_watch=self.tier_watch,
            tier_accumulate=self.tier_accumulate, tier_deepvalue=self.tier_deepvalue,
            tier_hysteresis_margin=self.tier_hysteresis_margin, flash_fng_max=self.flash_fng_max,
            flash_drop_pct=self.flash_drop_pct, flash_debounce_days=self.flash_debounce_days,
            ath_date=self.ath_date, peak_to_trough_days=self.peak_to_trough_days,
            cycle_mult_swing=self.cycle_mult_swing, st_timeframes=self.st_timeframes,
            st_cooldown_hours=self.st_cooldown_hours, st_rsi_oversold=self.st_rsi_oversold,
            st_rsi_overbought=self.st_rsi_overbought, st_vol_spike_mult=self.st_vol_spike_mult,
            st_funding_spike=self.st_funding_spike, st_oi_surge_pct=self.st_oi_surge_pct,
            st_buy_threshold=self.st_buy_threshold,
            st_strong_buy_threshold=self.st_strong_buy_threshold,
            st_sell_threshold=self.st_sell_threshold,
            st_strong_sell_threshold=self.st_strong_sell_threshold,
            st_regime_suppress=self.st_regime_suppress,
            st_require_confluence=self.st_require_confluence)

    @property
    def equity(self) -> EquityConfig:
        return EquityConfig(
            finnhub_api_key=self.finnhub_api_key, alpaca_api_key=self.alpaca_api_key,
            alpaca_secret_key=self.alpaca_secret_key, tiingo_api_key=self.tiingo_api_key,
            massive_api_key=self.massive_api_key, sec_user_agent=self.sec_user_agent,
            stock_insider_enabled=self.stock_insider_enabled,
            stock_universe_path=self.stock_universe_path, stock_top_n=self.stock_top_n,
            stock_pead_lookback_days=self.stock_pead_lookback_days,
            stock_pead_min_surprise=self.stock_pead_min_surprise,
            stock_min_price=self.stock_min_price, stock_min_dollar_vol=self.stock_min_dollar_vol,
            stock_atr_k_stop=self.stock_atr_k_stop, stock_atr_k_t1=self.stock_atr_k_t1,
            stock_atr_k_t2=self.stock_atr_k_t2, stock_time_stop_days=self.stock_time_stop_days,
            stock_cooldown_days=self.stock_cooldown_days, stock_allow_shorts=self.stock_allow_shorts,
            stock_cost_bps=self.stock_cost_bps, stock_lt_top_n=self.stock_lt_top_n,
            stock_lt_min_dollar_vol=self.stock_lt_min_dollar_vol)

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

    # --- Stock layer toggles (drive /api/stock/health messaging only; scoring
    # lights up purely from a source returning real numbers, like the BTC side) --

    @property
    def finnhub_active(self) -> bool:
        """The scored PEAD edge needs Finnhub (earnings surprises). Free key."""
        return bool(self.finnhub_api_key)

    @property
    def alpaca_active(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_secret_key)

    @property
    def massive_active(self) -> bool:
        return bool(self.massive_api_key)

    @property
    def stock_price_source(self) -> str:
        """The BULK primary venue prices() uses for the daily 536-name fetch — aligned
        with sources/stocks/prices.py. Yahoo is the keyless bulk default (Massive's
        5/min free limit rules it out as a bulk source; it's a per-ticker FALLBACK for
        the few names Yahoo drops). A free Alpaca/Tiingo key upgrades the bulk feed."""
        if self.alpaca_active:
            return "alpaca"
        if self.tiingo_api_key:
            return "tiingo"
        return "yahoo"  # keyless free default (best-effort)

    @property
    def stock_insider_active(self) -> bool:
        return self.stock_insider_enabled  # keyless

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
        coinglass_api_key=_opt("COINGLASS_API_KEY"),
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
        # --- Stock swing tracker ---
        finnhub_api_key=_opt("FINNHUB_API_KEY"),
        alpaca_api_key=_opt("ALPACA_API_KEY"),
        alpaca_secret_key=_opt("ALPACA_SECRET_KEY"),
        tiingo_api_key=_opt("TIINGO_API_KEY"),
        sec_user_agent=_get("SEC_USER_AGENT",
                            "riverviewweb-signal stock tracker admin@riverviewweb.com"),
        stock_insider_enabled=_get_bool("STOCK_INSIDER", True),
        stock_universe_path=_get("STOCK_UNIVERSE_PATH", "app/stock_universe.json"),
        stock_top_n=_get_int("STOCK_TOP_N", 15),
        stock_pead_lookback_days=_get_int("STOCK_PEAD_LOOKBACK_DAYS", 10),
        stock_pead_min_surprise=_get_float("STOCK_PEAD_MIN_SURPRISE", 3.0),
        stock_min_price=_get_float("STOCK_MIN_PRICE", 5.0),
        stock_min_dollar_vol=_get_float("STOCK_MIN_DOLLAR_VOL", 5_000_000.0),
        stock_atr_k_stop=_get_float("STOCK_ATR_K_STOP", 1.5),
        stock_atr_k_t1=_get_float("STOCK_ATR_K_T1", 1.5),
        stock_atr_k_t2=_get_float("STOCK_ATR_K_T2", 2.5),
        stock_time_stop_days=_get_int("STOCK_TIME_STOP_DAYS", 12),
        stock_cooldown_days=_get_float("STOCK_COOLDOWN_DAYS", 5),
        stock_allow_shorts=_get_bool("STOCK_ALLOW_SHORTS", False),
        stock_cost_bps=_get_float("STOCK_COST_BPS", 10.0),
        massive_api_key=_opt("MASSIVE_API_KEY"),
        stock_lt_top_n=_get_int("STOCK_LT_TOP_N", 30),
        stock_lt_min_dollar_vol=_get_float("STOCK_LT_MIN_DOLLAR_VOL", 3_000_000.0),
        freshness_budget_days=_get_float("FRESHNESS_BUDGET_DAYS", 3.0),
        api_token=_opt("API_TOKEN"),
        api_cors_origin=_opt("API_CORS_ORIGIN"),
        public_base_url=_get("PUBLIC_BASE_URL", "https://btc.riverviewweb.com").rstrip("/"),
        watchdog_stale_hours=_get_float("WATCHDOG_STALE_HOURS", 3),
    )

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


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


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

    # Free data
    fred_api_key: str | None

    # Paid drop-ins (presence toggles the layer)
    glassnode_api_key: str | None
    cryptoquant_api_key: str | None
    coinglass_api_key: str | None
    sosovalue_api_key: str | None

    # Signal config
    symbol: str
    db_path: str
    weights: dict[str, float]
    tier_watch: float
    tier_accumulate: float
    tier_deepvalue: float

    # Acute-capitulation flash
    flash_fng_max: float
    flash_drop_pct: float
    flash_debounce_days: int

    # Cycle context
    ath_date: date
    peak_to_trough_days: int

    # --- Derived helpers -------------------------------------------------

    @property
    def onchain_active(self) -> bool:
        return bool(self.glassnode_api_key or self.cryptoquant_api_key)

    @property
    def derivs_paid_active(self) -> bool:
        return bool(self.coinglass_api_key)

    @property
    def macro_active(self) -> bool:
        return bool(self.fred_api_key)

    def notifications_configured(self) -> bool:
        return bool(self.ntfy_topic or (self.telegram_bot_token and self.telegram_chat_id))


def load_config() -> Config:
    return Config(
        ntfy_topic=_opt("NTFY_TOPIC"),
        ntfy_server=_get("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
        telegram_bot_token=_opt("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_opt("TELEGRAM_CHAT_ID"),
        fred_api_key=_opt("FRED_API_KEY"),
        glassnode_api_key=_opt("GLASSNODE_API_KEY"),
        cryptoquant_api_key=_opt("CRYPTOQUANT_API_KEY"),
        coinglass_api_key=_opt("COINGLASS_API_KEY"),
        sosovalue_api_key=_opt("SOSOVALUE_API_KEY"),
        symbol=_get("SYMBOL", "BTCUSDT"),
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
        flash_fng_max=_get_float("FLASH_FNG_MAX", 10),
        flash_drop_pct=_get_float("FLASH_DROP_PCT", 10),
        flash_debounce_days=_get_int("FLASH_DEBOUNCE_DAYS", 3),
        ath_date=_parse_date(_get("ATH_DATE", "2025-10-06"), date(2025, 10, 6)),
        peak_to_trough_days=_get_int("PEAK_TO_TROUGH_DAYS", 370),
    )

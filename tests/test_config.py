"""Config parsing tests — notably inline-comment stripping (a real bug:
python-dotenv leaves the comment as the value when the value is blank)."""
from __future__ import annotations

from app import config


def test_strip_inline_comment():
    f = config._strip_inline_comment
    assert f("           # turns on the layer") == ""   # blank value + comment
    assert f("abc123 # note") == "abc123"               # value + inline comment
    assert f("abc123") == "abc123"                       # no comment
    assert f("tok#en") == "tok#en"                       # embedded # (no space) kept
    assert f("   ") == "   "                              # whitespace only -> caller strips


def test_get_skips_blank_with_comment(monkeypatch):
    monkeypatch.setenv("GLASSNODE_API_KEY", "          # turns on the on-chain layer")
    monkeypatch.setenv("SYMBOL", "BTC-USDT # spot")
    cfg = config.load_config()
    assert cfg.glassnode_api_key is None
    # on-chain is free-by-default now (bitcoin-data.com), so the layer is active
    # even without a paid key.
    assert cfg.onchain_active is True
    assert cfg.onchain_source == "bitcoin-data"
    assert cfg.symbol == "BTC-USDT"


def test_get_bool_parsing(monkeypatch):
    monkeypatch.setenv("ONCHAIN_FREE", "off")
    assert config.load_config().onchain_free_enabled is False
    monkeypatch.setenv("ONCHAIN_FREE", "1")
    assert config.load_config().onchain_free_enabled is True


def test_onchain_free_optout(monkeypatch):
    monkeypatch.delenv("GLASSNODE_API_KEY", raising=False)
    monkeypatch.setenv("ONCHAIN_FREE", "false")
    cfg = config.load_config()
    assert cfg.onchain_active is False
    assert cfg.onchain_source is None


def test_onchain_source_glassnode(monkeypatch):
    monkeypatch.setenv("GLASSNODE_API_KEY", "gn-key")
    cfg = config.load_config()
    assert cfg.onchain_active is True
    assert cfg.onchain_source == "glassnode"


def test_coinalyze_layer_toggle_and_defaults(monkeypatch):
    monkeypatch.delenv("COINALYZE_API_KEY", raising=False)
    cfg = config.load_config()
    assert cfg.coinalyze_active is False
    # Sensible defaults so the flow layer is configured the moment a key appears.
    assert cfg.coinalyze_symbol == "BTCUSDT_PERP.A"
    assert cfg.flow_cvd_lookback == 14
    assert cfg.flow_liq_spike_mult == 3.0
    assert cfg.flow_oi_bar_surge_pct == 3.0     # per-bar OI gate (decoupled from st_oi_surge_pct)
    assert cfg.flow_liq_min_usd == 500_000.0

    monkeypatch.setenv("COINALYZE_API_KEY", "ca-key")
    assert config.load_config().coinalyze_active is True


def test_freshness_budget_default_and_override(monkeypatch):
    monkeypatch.delenv("FRESHNESS_BUDGET_DAYS", raising=False)
    assert config.load_config().freshness_budget_days == 3.0
    monkeypatch.setenv("FRESHNESS_BUDGET_DAYS", "7")
    assert config.load_config().freshness_budget_days == 7.0

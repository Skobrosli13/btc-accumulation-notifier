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

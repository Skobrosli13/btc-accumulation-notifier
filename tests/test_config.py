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
    assert cfg.onchain_active is False
    assert cfg.symbol == "BTC-USDT"

"""ETF-flow source: Farside footer-row exclusion + SoSoValue POST + fail-soft."""
from __future__ import annotations

import pytest

from app.sources import etf_flows

pd = pytest.importorskip("pandas")
# read_html needs a parser; skip the Farside tests cleanly if none is installed.
# pandas 3.x requires a file-like object (StringIO), not a literal HTML string.
import io  # noqa: E402

_HAS_PARSER = True
try:  # pragma: no cover - environment probe
    pd.read_html(io.StringIO("<table><tr><td>1</td></tr></table>"))
except Exception:  # noqa: BLE001
    _HAS_PARSER = False


# A realistic Farside-style table: a Date column, per-issuer columns, a Total
# column of DAILY net flows ($m), then the footer rows (Total/Average/Maximum/
# Minimum) that must be excluded from the trailing-30d sum.
_FARSIDE_HTML = """
<table>
  <tr><th>Date</th><th>IBIT</th><th>FBTC</th><th>Total</th></tr>
  <tr><td>02 Jun 2026</td><td>50.0</td><td>30.0</td><td>80.0</td></tr>
  <tr><td>03 Jun 2026</td><td>20.0</td><td>10.0</td><td>30.0</td></tr>
  <tr><td>04 Jun 2026</td><td>(15.0)</td><td>5.0</td><td>(10.0)</td></tr>
  <tr><td>Total</td><td>9999.0</td><td>8888.0</td><td>99999.0</td></tr>
  <tr><td>Average</td><td>100.0</td><td>90.0</td><td>33333.0</td></tr>
  <tr><td>Maximum</td><td>500.0</td><td>400.0</td><td>80.0</td></tr>
  <tr><td>Minimum</td><td>-50.0</td><td>-40.0</td><td>(10.0)</td></tr>
</table>
"""


@pytest.mark.skipif(not _HAS_PARSER, reason="pandas.read_html needs lxml/html5lib")
def test_farside_excludes_footer_rows(monkeypatch):
    monkeypatch.setattr(etf_flows, "get_text", lambda *a, **k: _FARSIDE_HTML)
    val = etf_flows._from_farside()
    # Only the three real daily rows sum: 80 + 30 - 10 = 100 ($m) -> 0.1 $bn.
    # If the footers leaked in, the all-time 'Total' (99999) would dominate.
    assert val == pytest.approx(0.1)


@pytest.mark.skipif(not _HAS_PARSER, reason="pandas.read_html needs lxml/html5lib")
def test_farside_none_when_no_html(monkeypatch):
    monkeypatch.setattr(etf_flows, "get_text", lambda *a, **k: None)
    assert etf_flows._from_farside() is None


def test_sosovalue_uses_post(monkeypatch):
    captured = {}

    def fake_post(url, json_body=None, params=None, headers=None, timeout=20):
        captured["url"] = url
        captured["json_body"] = json_body
        captured["headers"] = headers
        return {"data": [{"date": "2026-06-08", "totalNetInflow": 1_000_000_000.0}]}

    monkeypatch.setattr(etf_flows, "post_json", fake_post)
    val = etf_flows._from_sosovalue("sk-test")
    assert val == pytest.approx(1.0)  # $1bn / 1e9
    assert captured["json_body"] == {"type": "us-btc-spot"}
    assert captured["headers"]["x-soso-api-key"] == "sk-test"
    assert "historicalInflowChart" in captured["url"]


def test_sosovalue_fails_soft_on_none(monkeypatch):
    monkeypatch.setattr(etf_flows, "post_json", lambda *a, **k: None)
    assert etf_flows._from_sosovalue("sk-test") is None


def test_sosovalue_fails_soft_on_list_response(monkeypatch):
    # A top-level LIST response must not raise AttributeError on .get(...).
    monkeypatch.setattr(etf_flows, "post_json", lambda *a, **k: [1, 2, 3])
    assert etf_flows._from_sosovalue("sk-test") is None


def test_etf_flows_never_raises_on_list_response(monkeypatch):
    # The public entry point must fail soft even if a leaf returns a surprising
    # shape; it can only ever return {'etf_flow': <num or None>}.
    monkeypatch.setattr(etf_flows, "post_json", lambda *a, **k: {"unexpected": 1})
    monkeypatch.setattr(etf_flows, "get_text", lambda *a, **k: None)
    monkeypatch.setenv("SOSOVALUE_API_KEY", "sk-test")
    out = etf_flows.etf_flows()
    assert out == {"etf_flow": None}


def test_etf_flows_prefers_sosovalue(monkeypatch):
    monkeypatch.setenv("SOSOVALUE_API_KEY", "sk-test")
    monkeypatch.setattr(
        etf_flows, "post_json",
        lambda *a, **k: {"data": [{"totalNetInflow": 2_000_000_000.0}]})
    # Farside should not be consulted when SoSoValue yields a value.
    monkeypatch.setattr(etf_flows, "get_text",
                        lambda *a, **k: pytest.fail("Farside hit despite SoSoValue value"))
    out = etf_flows.etf_flows()
    assert out == {"etf_flow": pytest.approx(2.0)}

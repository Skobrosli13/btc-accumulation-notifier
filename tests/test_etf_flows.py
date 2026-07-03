"""ETF-flow source: Farside footer-row exclusion + fail-soft (free-only).

The paid SoSoValue path was removed in Phase 0 (BTC data is free-only); the
free Farside scrape is the sole source.
"""
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


def test_etf_flows_never_raises(monkeypatch):
    # The public entry point can only ever return {'etf_flow': <num or None>};
    # even a scrape returning nothing must fail soft.
    monkeypatch.setattr(etf_flows, "get_text", lambda *a, **k: None)
    out = etf_flows.etf_flows()
    assert out == {"etf_flow": None}

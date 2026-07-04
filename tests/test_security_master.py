"""Security master (permaticker) + the Sharadar->lake ingest CLI."""
from __future__ import annotations

import pandas as pd

from app.data.equities import security_master as sm
from app.data_lake import Lake


def test_ticker_map_prefers_listed_on_reuse():
    rows = [
        {"ticker": "AAPL", "permaticker": 199059, "isdelisted": "N"},
        # a delisted issuer that once used ticker "X"
        {"ticker": "X", "permaticker": 111, "isdelisted": "Y"},
        # a currently-listed issuer now using "X" -> should win the map
        {"ticker": "X", "permaticker": 222, "isdelisted": "N"},
    ]
    m = sm.ticker_permaticker_map(rows)
    assert m["AAPL"] == 199059
    assert m["X"] == 222          # the still-listed issuer wins the reused ticker


def test_master_collects_tickers_and_prefers_listed_attributes():
    rows = [
        {"permaticker": 5, "ticker": "OLD", "name": "OLDCO", "exchange": "NYSE",
         "sector": "Energy", "isdelisted": "Y"},
        {"permaticker": 5, "ticker": "NEW", "name": "NEWCO", "exchange": "NASDAQ",
         "sector": "Energy", "isdelisted": "N"},
    ]
    master = sm.master_by_permaticker(rows)
    e = master[5]
    assert set(e["tickers"]) == {"OLD", "NEW"}
    assert e["name"] == "NEWCO" and e["isdelisted"] == "N"   # listed row's attrs preferred


def test_ingest_upserts_idempotently(tmp_path, monkeypatch):
    from scripts import ingest as ing
    lake = Lake(tmp_path / "lake")
    rows = [{"table": "SEP", "permaticker": 1, "ticker": "AAPL", "name": "APPLE", "lastupdated": "2026-01-01"},
            {"table": "SEP", "permaticker": 2, "ticker": "MSFT", "name": "MICROSOFT", "lastupdated": "2026-01-01"}]
    monkeypatch.setattr(ing.sharadar, "fetch_table", lambda *a, **k: rows)
    assert ing.ingest("TICKERS", lake=lake, api_key="k") == 2
    assert lake.exists("tickers")
    assert ing.ingest("TICKERS", lake=lake, api_key="k") == 2   # idempotent re-run


def test_ingest_incremental_filters_by_lastupdated(tmp_path, monkeypatch):
    from scripts import ingest as ing
    lake = Lake(tmp_path / "lake")
    lake.write("daily", pd.DataFrame({"ticker": ["A"], "date": ["2026-01-01"],
                                      "lastupdated": ["2026-03-01"]}))
    captured = {}

    def fake_fetch(table, api_key, *, params=None):
        captured["params"] = params
        return []      # no new rows

    monkeypatch.setattr(ing.sharadar, "fetch_table", fake_fetch)
    ing.ingest("DAILY", incremental=True, lake=lake, api_key="k")
    assert captured["params"]["lastupdated.gte"] == "2026-03-01"   # from the lake's max

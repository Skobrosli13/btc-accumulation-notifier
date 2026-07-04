"""Parquet lake: round-trip, idempotent upsert (freshest-wins), and DuckDB query."""
from __future__ import annotations

import pandas as pd

from app.data_lake import Lake


def test_write_read_round_trip(tmp_path):
    lake = Lake(tmp_path / "lake")
    assert lake.read("sep").empty          # missing table -> empty frame
    df = pd.DataFrame({"ticker": ["A", "B"], "close": [1.0, 2.0]})
    lake.write("sep", df)
    assert lake.exists("sep")
    pd.testing.assert_frame_equal(lake.read("sep"), df)


def test_upsert_is_idempotent_and_keeps_freshest(tmp_path):
    lake = Lake(tmp_path / "lake")
    keys = ["ticker", "date"]
    v1 = pd.DataFrame({"ticker": ["A", "B"], "date": ["d1", "d1"],
                       "close": [1.0, 2.0], "lastupdated": ["2026-01-01", "2026-01-01"]})
    assert lake.upsert("sep", v1, keys) == 2
    # Re-upserting the identical rows changes nothing (idempotent).
    assert lake.upsert("sep", v1, keys) == 2
    # A restatement of A@d1 (newer lastupdated) + a new row C@d1.
    v2 = pd.DataFrame({"ticker": ["A", "C"], "date": ["d1", "d1"],
                       "close": [1.5, 3.0], "lastupdated": ["2026-02-01", "2026-02-01"]})
    assert lake.upsert("sep", v2, keys) == 3
    got = lake.read("sep").set_index("ticker")["close"].to_dict()
    assert got == {"A": 1.5, "B": 2.0, "C": 3.0}   # A took the fresher value


def test_max_value_for_incremental_refresh(tmp_path):
    lake = Lake(tmp_path / "lake")
    assert lake.max_value("sep", "lastupdated") is None
    lake.write("sep", pd.DataFrame({"lastupdated": ["2026-01-01", "2026-03-01", "2026-02-01"]}))
    assert lake.max_value("sep", "lastupdated") == "2026-03-01"


def test_duckdb_query_over_parquet(tmp_path):
    lake = Lake(tmp_path / "lake")
    lake.write("daily", pd.DataFrame({"ticker": ["A", "B", "C"], "marketcap": [10, 30, 20]}))
    out = lake.query(
        f"SELECT ticker FROM {lake.sql_table('daily')} WHERE marketcap >= 20 ORDER BY marketcap DESC")
    assert list(out["ticker"]) == ["B", "C"]

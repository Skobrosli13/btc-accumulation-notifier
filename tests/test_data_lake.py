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


def test_upsert_normalizes_date_typed_keys(tmp_path):
    """A bulk-loaded parquet stores DATE columns; the cursor API sends ISO
    strings — the merge must still dedupe and sort (regression: the nightly
    incremental blew up on date-vs-str comparison and would have silently
    double-inserted rows)."""
    from datetime import date
    lake = Lake(tmp_path / "lake")
    # NON-key date column (calendardate) + a nullable string col: the normalize
    # must cover every shared date column (pyarrow can't write mixed objects)
    # while never stringifying None/NaN.
    lake.write("sep", pd.DataFrame({"ticker": ["A"], "date": [date(2026, 7, 2)],
                                    "close": [1.0], "lastupdated": [date(2026, 7, 2)],
                                    "calendardate": [date(2026, 6, 30)],
                                    "note": [None]}))
    new = pd.DataFrame({"ticker": ["A", "A"], "date": ["2026-07-02", "2026-07-03"],
                        "close": [1.5, 2.0], "lastupdated": ["2026-07-03", "2026-07-03"],
                        "calendardate": ["2026-06-30", "2026-06-30"],
                        "note": [None, "x"]})
    n = lake.upsert("sep", new, ["ticker", "date"])
    assert n == 2                                       # restated 07-02 deduped
    got = lake.read("sep").sort_values("date")
    assert list(got["close"]) == [1.5, 2.0]             # freshest won
    assert got["note"].iloc[0] is None or got["note"].isna().iloc[0]  # None stays None


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

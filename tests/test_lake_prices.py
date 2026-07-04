"""Lake-backed SEP price reader: split+div adjustment + scorer-shape bars."""
from __future__ import annotations

import pandas as pd

from app.data.equities import prices
from app.data_lake import Lake


def test_bars_from_sep_rows_adjusts_ohl_by_closeadj_factor():
    # A 2:1 split: raw close 100 -> closeadj 50 (factor 0.5); OHL back-adjusted.
    rows = [
        {"date": "2026-01-02", "open": 100.0, "high": 110.0, "low": 90.0,
         "close": 100.0, "closeadj": 50.0, "volume": 1000.0},
        {"date": "2026-01-05", "open": 52.0, "high": 55.0, "low": 48.0,
         "close": 51.0, "closeadj": 51.0, "volume": 2000.0},
    ]
    bars = prices.bars_from_sep_rows(rows)
    assert bars[0]["close"] == 50.0                 # closeadj
    assert bars[0]["open"] == 50.0                  # 100 * (50/100)
    assert bars[0]["high"] == 55.0 and bars[0]["low"] == 45.0
    assert bars[1]["close"] == 51.0 and bars[1]["open"] == 52.0   # factor 1.0
    assert bars[0]["ts"] < bars[1]["ts"]            # oldest -> newest, epoch ms


def test_sep_bars_reads_recent_oldest_to_newest(tmp_path):
    lake = Lake(tmp_path / "lake")
    assert prices.sep_bars(lake, "AAPL") == []      # SEP not ingested yet
    sep = pd.DataFrame({
        "ticker": ["AAPL"] * 3 + ["MSFT"],
        "date": ["2026-01-02", "2026-01-03", "2026-01-06", "2026-01-02"],
        "open": [10.0, 11.0, 12.0, 99.0], "high": [10.0, 11.0, 12.0, 99.0],
        "low": [10.0, 11.0, 12.0, 99.0], "close": [10.0, 11.0, 12.0, 99.0],
        "closeadj": [10.0, 11.0, 12.0, 99.0], "volume": [1.0, 1.0, 1.0, 1.0]})
    lake.write("sep", sep)
    bars = prices.sep_bars(lake, "AAPL", limit=2)
    assert [b["close"] for b in bars] == [11.0, 12.0]   # last 2, oldest->newest, AAPL only

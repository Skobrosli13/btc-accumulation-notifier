"""Archive deep BTC-USD daily closes into the Parquet lake (§4.7 store-forward).

    python -m scripts.ingest_btc

Reuses the calibrator's paginated Coinbase fetch (2015-07+ daily, reachable from
AWS; hard-fails on a mid-window hole rather than building a silent gap) and
upserts into the lake table ``btc_daily`` keyed on date. Idempotent; re-runs
refresh the tail. The BTC POLICY backtests (btc_trend_policy / btc_accum_policy)
read this series — the app's own candles table only reaches back to first
deployment, which is nowhere near enough regime coverage.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config          # noqa: E402
from app.data_lake import Lake               # noqa: E402
from scripts.calibrate import _coinbase_daily  # noqa: E402

log = logging.getLogger("ingest-btc")


def ingest_btc_daily(lake: Lake | None = None) -> int:
    lake = lake or Lake(load_config().data_lake_path)
    df = _coinbase_daily()                    # [date, close]
    if df.empty:
        raise SystemExit("Coinbase unreachable — no BTC daily series fetched")
    df = df.copy()
    df["date"] = df["date"].astype(str).str[:10]
    n = lake.upsert("btc_daily", df, ["date"], sort_col="date")
    log.info("btc_daily: fetched %d rows -> lake now %d rows", len(df), n)
    return n


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ingest_btc_daily()

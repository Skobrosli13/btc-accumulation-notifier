"""Ingest a Sharadar table into the Parquet lake (idempotent) — §4.1.

    python -m scripts.ingest TICKERS
    python -m scripts.ingest SF1 --ticker AAPL
    python -m scripts.ingest DAILY --incremental        # only rows changed since the lake's newest

Fetches via the datatables cursor (app.data.equities.sharadar), then merges into
the lake with a per-table primary key so re-runs converge (freshest wins). SEP/
SF1 full pulls are large (multi-cadence, cached upstream) — filter with --ticker
for a smoke, or run --incremental after the first full load. Raw is re-derivable
from the vendor, so the lake is the cache of record (no separate raw store yet).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config                     # noqa: E402
from app.data.equities import sharadar                 # noqa: E402
from app.data_lake import Lake                          # noqa: E402

log = logging.getLogger("ingest")

# Primary key per table for the idempotent upsert (None => dedupe on all columns).
PRIMARY_KEYS = {
    # 'table' distinguishes which Sharadar dataset covers a security (SEP/SF1/...);
    # keep it in the PK so the raw TICKERS rows aren't collapsed on ingest.
    "TICKERS": ["table", "permaticker", "ticker"],
    "SEP": ["ticker", "date"],
    "SF1": ["ticker", "dimension", "datekey"],
    "DAILY": ["ticker", "date"],
    "SF2": None,        # insider events — no clean PK, dedupe exact rows
    "SF3": None, "SF3A": None, "SF3B": None,
    "ACTIONS": None,    # corporate actions — dedupe exact rows
    "METRICS": ["ticker", "date"], "SP500": None,
    "EVENTS": None, "SFP": ["ticker", "date"], "INDICATORS": None,
}

# Column driving --incremental (.gte filter) per table. SF2/ACTIONS carry no
# lastupdated; their natural append column stands in (Form 3/4/5 rows are
# append-only by filing date; actions by action date).
INCREMENTAL_COLS = {
    "SF2": "filingdate",
    "ACTIONS": "date",
    "SF3": "calendardate", "SF3A": "calendardate", "SF3B": "calendardate",
}
_DEFAULT_INCREMENTAL_COL = "lastupdated"


def ingest(table: str, *, ticker: str | None = None, incremental: bool = False,
           lake: Lake | None = None, api_key: str | None = None) -> int:
    cfg = load_config()
    api_key = api_key or cfg.nasdaq_data_link_api_key
    if not api_key:
        raise SystemExit("NASDAQ_DATA_LINK_API_KEY not set — cannot ingest Sharadar")
    lake = lake or Lake(cfg.data_lake_path)
    table = table.upper()
    params: dict = {"qopts.per_page": 10000}
    if ticker:
        params["ticker"] = ticker.upper()
    if incremental:
        col = INCREMENTAL_COLS.get(table, _DEFAULT_INCREMENTAL_COL)
        since = lake.max_value(table.lower(), col)
        if since is not None:
            # Sharadar filters date columns with a .gte operator (ISO date).
            params[f"{col}.gte"] = str(since)[:10]
            log.info("%s incremental: %s >= %s", table, col, params[f"{col}.gte"])
    rows = sharadar.fetch_table(table, api_key, params=params)
    if not rows:
        log.warning("%s: 0 rows fetched (no change / no data / error)", table)
        return lake.read(table.lower()).shape[0] if lake.exists(table.lower()) else 0
    df = pd.DataFrame(rows)
    n = lake.upsert(table.lower(), df, PRIMARY_KEYS.get(table))
    log.info("%s: fetched %d rows -> lake now %d rows", table, len(rows), n)
    return n


def ingest_bulk(table: str, *, lake: Lake | None = None, api_key: str | None = None) -> int:
    """Full-table snapshot via the bulk-export zip -> DuckDB streams the CSV into
    Parquet (no pandas RAM blowup — SEP/SF1/DAILY are multi-GB). Overwrites the
    lake table (a bulk pull IS the full snapshot). Returns the row count."""
    import os
    import tempfile
    import zipfile

    import duckdb
    import requests

    cfg = load_config()
    api_key = api_key or cfg.nasdaq_data_link_api_key
    if not api_key:
        raise SystemExit("NASDAQ_DATA_LINK_API_KEY not set")
    lake = lake or Lake(cfg.data_lake_path)
    table = table.upper()
    log.info("%s: requesting bulk export (polling until fresh)...", table)
    link = sharadar.bulk_link(table, api_key)
    if not link:
        raise SystemExit(f"bulk export for {table} unavailable / timed out")
    lake.root.mkdir(parents=True, exist_ok=True)
    out = lake.path(table.lower())
    with tempfile.TemporaryDirectory() as td:
        zip_path = os.path.join(td, f"{table}.zip")
        log.info("%s: downloading snapshot...", table)
        with requests.get(link, stream=True, timeout=1800) as r:
            r.raise_for_status()
            with open(zip_path, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
        with zipfile.ZipFile(zip_path) as z:
            csv_name = z.namelist()[0]
            z.extract(csv_name, td)
            csv_path = os.path.join(td, csv_name).replace(os.sep, "/")
        log.info("%s: converting CSV -> Parquet via DuckDB...", table)
        con = duckdb.connect()
        try:
            con.execute(
                f"COPY (SELECT * FROM read_csv_auto('{csv_path}', header=true, "
                f"sample_size=-1)) TO '{out.as_posix()}' (FORMAT PARQUET)")
        finally:
            con.close()
    n = int(lake.query(f"SELECT count(*) AS c FROM {lake.sql_table(table.lower())}")["c"][0])
    log.info("%s: bulk ingest complete -> lake %d rows", table, n)
    return n


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="Ingest a Sharadar table into the Parquet lake")
    p.add_argument("table", help="Sharadar table, e.g. TICKERS / SF1 / SEP / ACTIONS")
    p.add_argument("--ticker", help="restrict to one ticker (smoke / partial ingest)")
    p.add_argument("--incremental", action="store_true",
                   help="only fetch rows changed since the lake's newest lastupdated")
    p.add_argument("--bulk", action="store_true",
                   help="full-table snapshot via the bulk-export zip (SEP/SF1/DAILY etc.)")
    args = p.parse_args(argv)
    if args.bulk:
        ingest_bulk(args.table)
    else:
        ingest(args.table, ticker=args.ticker, incremental=args.incremental)


if __name__ == "__main__":
    main()

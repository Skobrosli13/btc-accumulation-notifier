"""Parquet research lake, queried via DuckDB (§2).

The app SQLite holds operational state only (subscribers, events, studies,
fills, decisions); the bulky, re-derivable research series live here as Parquet:
Sharadar SEP/SF1/SF2/SF3/DAILY/TICKERS/ACTIONS, EDGAR facts, candles, the
funding/OI/liq archive, placebo aggregates.

One file per logical table (``{root}/{table}.parquet``). Ingest is **idempotent**
via :meth:`Lake.upsert` — concat new rows, keep the freshest per primary key
(``lastupdated`` breaks ties), rewrite. Re-running an ingest converges to the
same set. DuckDB does the SQL/analytics over ``read_parquet``; pandas does the
in-memory frames. The lake is re-derivable from the raw vendor cache, so it is
NOT committed (see .gitignore) and needs no migrations.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)


class Lake:
    """A directory of Parquet tables with idempotent upsert + DuckDB query."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def path(self, table: str) -> Path:
        return self.root / f"{table}.parquet"

    def exists(self, table: str) -> bool:
        return self.path(table).is_file()

    def write(self, table: str, df: pd.DataFrame) -> None:
        """Overwrite ``table`` with ``df`` (creates the lake dir on first write)."""
        self.root.mkdir(parents=True, exist_ok=True)
        df.to_parquet(self.path(table), index=False)

    def read(self, table: str) -> pd.DataFrame:
        """Read ``table`` (empty DataFrame if it does not exist yet)."""
        if not self.exists(table):
            return pd.DataFrame()
        return pd.read_parquet(self.path(table))

    def upsert(self, table: str, df: pd.DataFrame, keys: list[str],
               *, sort_col: str = "lastupdated") -> int:
        """Merge ``df`` into ``table``, keeping the freshest row per ``keys``.

        Rows are concatenated onto the existing table, sorted by ``sort_col``
        (if present) so the newest lands last, then deduped on ``keys`` keeping
        the last. Idempotent: re-upserting the same rows leaves the table
        unchanged. Returns the resulting row count.
        """
        if df is None or df.empty:
            return self.read(table).shape[0] if self.exists(table) else 0
        if self.exists(table):
            df = pd.concat([self.read(table), df], ignore_index=True)
        if sort_col and sort_col in df.columns:
            df = df.sort_values(sort_col, kind="stable")
        df = df.drop_duplicates(subset=keys, keep="last").reset_index(drop=True)
        self.write(table, df)
        return len(df)

    def max_value(self, table: str, column: str):
        """Max of ``column`` in ``table`` (e.g. the latest ``lastupdated`` for an
        incremental refresh), or None if the table/column is absent/empty."""
        if not self.exists(table):
            return None
        df = self.read(table)
        if column not in df.columns or df.empty:
            return None
        return df[column].max()

    def query(self, sql: str):
        """Run a DuckDB SQL query. Reference tables as
        ``read_parquet('<lake>/<table>.parquet')`` — use :meth:`sql_table` to
        build that path. Returns a pandas DataFrame."""
        import duckdb
        con = duckdb.connect(database=":memory:")
        try:
            return con.execute(sql).df()
        finally:
            con.close()

    def sql_table(self, table: str) -> str:
        """A ``read_parquet(...)`` expression for ``table``, for use in query()."""
        return f"read_parquet('{self.path(table).as_posix()}')"

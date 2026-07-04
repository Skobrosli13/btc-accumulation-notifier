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

    def upsert(self, table: str, df: pd.DataFrame, keys: list[str] | None,
               *, sort_col: str = "lastupdated") -> int:
        """Merge ``df`` into ``table``, keeping the freshest row per ``keys``.

        Rows are concatenated onto the existing table, sorted by ``sort_col``
        (if present) so the newest lands last, then deduped on ``keys`` keeping
        the last (``keys=None`` dedupes on all columns — for tables with no clean
        primary key). Idempotent: re-upserting the same rows leaves the table
        unchanged. Returns the resulting row count.
        """
        if df is None or df.empty:
            return self.read(table).shape[0] if self.exists(table) else 0
        df = df.copy()
        if self.exists(table):
            old = self.read(table)
            # Normalize EVERY shared date-object column to ISO strings (null-
            # safe): a bulk-loaded parquet stores DATE-typed columns while the
            # cursor API delivers strings — mixed object columns break sorting,
            # silently defeat key-dedup (date(...) != '2026-07-03'), and abort
            # the parquet write. ISO strings sort identically to dates.
            import datetime as _dt
            for col in set(old.columns) & set(df.columns):
                for frame in (old, df):
                    if frame[col].dtype != object:
                        continue
                    s = frame[col].dropna()
                    if not s.empty and isinstance(
                            s.iloc[0], (_dt.date, _dt.datetime)):
                        frame[col] = frame[col].map(
                            lambda v: str(v) if v is not None and v == v else v)
            df = pd.concat([old, df], ignore_index=True)
        if sort_col and sort_col in df.columns:
            df = df.sort_values(sort_col, kind="stable")
        # Dedupe on the keys actually present; a key column missing from the frame
        # (malformed response) degrades to all-column dedupe rather than crashing.
        subset = [k for k in keys if k in df.columns] if keys else None
        if keys and not subset:
            subset = None
        df = df.drop_duplicates(subset=subset, keep="last").reset_index(drop=True)
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

    def query(self, sql: str, params: list | None = None):
        """Run a DuckDB SQL query (optionally parameterized with ``?``
        placeholders — use these for any untrusted value). Reference tables as
        ``read_parquet('<lake>/<table>.parquet')`` via :meth:`sql_table`. Returns
        a pandas DataFrame."""
        import duckdb
        con = duckdb.connect(database=":memory:")
        try:
            return con.execute(sql, params or []).df()
        finally:
            con.close()

    def sql_table(self, table: str) -> str:
        """A ``read_parquet(...)`` expression for ``table``, for use in query()."""
        return f"read_parquet('{self.path(table).as_posix()}')"

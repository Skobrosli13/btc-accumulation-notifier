"""Parquet research lake, queried via DuckDB (§2).

The app SQLite holds operational state only (subscribers, events, studies,
fills, decisions); the bulky, re-derivable research series live here as Parquet:
Sharadar SEP/SF1/SF2/SF3/DAILY/TICKERS/ACTIONS, EDGAR facts, candles, the
funding/OI/liq archive, placebo aggregates.

One file per logical table (``{root}/{table}.parquet``). Ingest is **idempotent**
via :meth:`Lake.upsert` — a DuckDB streaming merge keeps the freshest row per
primary key (``lastupdated`` breaks ties; the incoming batch wins exact ties).
Re-running an ingest converges to the same set. DuckDB does the SQL/analytics
over ``read_parquet``; pandas holds only the (small) incoming frames — the
multi-GB tables never materialize in Python, so the nightly can run inside the
2GB always-on box (DASHBOARD_REDESIGN GAP C). Every DuckDB connection is capped
at ``LAKE_MEMORY_LIMIT`` (default 1GB; set per machine in .env) and spills to
``{root}/.duckdb_tmp``. The lake is re-derivable from the raw vendor cache, so
it is NOT committed (see .gitignore) and needs no migrations.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import re
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

_DEFAULT_MEMORY_LIMIT = "1GB"

_UNIT_MB = {"KB": 1 / 1024, "MB": 1, "GB": 1024, "TB": 1024 * 1024,
            "KIB": 1 / 1024, "MIB": 1, "GIB": 1024, "TIB": 1024 * 1024}


def _threads_for(limit: str) -> int | None:
    """Thread count that fits the memory cap (None = leave DuckDB's default).

    DuckDB pins working buffers per thread; sorting/windowing needs on the
    order of 128MB per thread to spill instead of dying — at the default
    thread-per-core an 8-core machine OOMs a 256MB cap that 2 threads handle
    fine. Fewer threads = slower, which a nightly batch can afford."""
    m = re.fullmatch(r"([\d.]+)\s*([KMGT]I?B)", limit.strip(), re.IGNORECASE)
    if not m:
        return None
    mb = float(m.group(1)) * _UNIT_MB[m.group(2).upper()]
    return max(1, min(os.cpu_count() or 1, int(mb // 128)))

_INT_TYPES = {"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
              "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT"}


def _q(name: str) -> str:
    """Quote a column identifier for DuckDB SQL."""
    return '"' + str(name).replace('"', '""') + '"'


def _is_numeric(t: str) -> bool:
    return t in _INT_TYPES or t in ("FLOAT", "DOUBLE") or t.startswith("DECIMAL")


def _union_type(a: str, b: str) -> str:
    """Common type for a column whose stored and incoming DuckDB types differ.

    Integer pairs widen (BIGINT), mixed numeric goes DOUBLE, timestamp variants
    stay TIMESTAMP; everything else (DATE vs VARCHAR being the canonical case:
    a bulk-loaded parquet stores typed DATEs while the datatables cursor sends
    ISO strings) falls back to VARCHAR — DuckDB renders dates as ISO strings,
    which compare, sort and dedupe identically to the cursor's strings.
    """
    a, b = a.upper(), b.upper()
    if a == b:
        return a
    if a in _INT_TYPES and b in _INT_TYPES:
        return "BIGINT"
    if _is_numeric(a) and _is_numeric(b):
        return "DOUBLE"
    if a.startswith("TIMESTAMP") and b.startswith("TIMESTAMP"):
        return "TIMESTAMP"
    return "VARCHAR"


def _normalize_date_objects(df: pd.DataFrame) -> pd.DataFrame:
    """ISO-stringify date/datetime *object* columns in place (null-safe).

    The cursor API delivers ISO strings but ad-hoc frames (crawlers, tests) can
    carry datetime.date objects; stringifying keeps object columns homogeneous
    (pyarrow can't write mixed objects) and convergent with the lake's
    ISO-string convention. Never stringifies None/NaN.
    """
    for col in df.columns:
        if df[col].dtype != object:
            continue
        s = df[col].dropna()
        if not s.empty and isinstance(s.iloc[0], (_dt.date, _dt.datetime)):
            df[col] = df[col].map(
                lambda v: str(v) if v is not None and v == v else v)
    return df


class Lake:
    """A directory of Parquet tables with idempotent upsert + DuckDB query."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def path(self, table: str) -> Path:
        return self.root / f"{table}.parquet"

    def exists(self, table: str) -> bool:
        return self.path(table).is_file()

    def _connect(self):
        """A DuckDB connection with a hard memory cap + disk spill directory,
        so every lake operation is out-of-core capable (the always-on box has
        2GB total; an uncapped DuckDB defaults to 80% of RAM and invites the
        OOM killer). LAKE_MEMORY_LIMIT overrides per machine — e.g. "512MB" on
        the box, "8GB" on a research laptop."""
        import duckdb
        raw = os.environ.get("LAKE_MEMORY_LIMIT", "")
        limit = raw.split(" #")[0].strip() or _DEFAULT_MEMORY_LIMIT
        spill = self.root / ".duckdb_tmp"
        spill.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(database=":memory:")
        con.execute(f"SET memory_limit='{limit}'")
        con.execute(f"SET temp_directory='{spill.as_posix()}'")
        threads = _threads_for(limit)
        if threads:
            con.execute(f"SET threads={threads}")
        return con

    def write(self, table: str, df: pd.DataFrame) -> None:
        """Overwrite ``table`` with ``df`` (creates the lake dir on first write)."""
        self.root.mkdir(parents=True, exist_ok=True)
        df.to_parquet(self.path(table), index=False)

    def read(self, table: str) -> pd.DataFrame:
        """Read ``table`` fully into pandas (empty DataFrame if it does not
        exist yet). For the small tables only — query() streams the big ones."""
        if not self.exists(table):
            return pd.DataFrame()
        return pd.read_parquet(self.path(table))

    def upsert(self, table: str, df: pd.DataFrame, keys: list[str] | None,
               *, sort_col: str = "lastupdated") -> int:
        """Merge ``df`` into ``table``, keeping the freshest row per ``keys``.

        Runs as a DuckDB streaming merge: stored rows whose key the increment
        does not touch pass straight through (hash anti-join, build side = the
        small incoming key set); only key-touched rows are re-ranked with a
        window. Per key, the greatest ``sort_col`` wins (NULL sorts freshest,
        matching pandas' NaN-last keep-last), the incoming frame beats storage
        on exact ties, and later incoming rows beat earlier ones. ``keys=None``
        — or every key absent from both schemas — dedupes exact rows. Column
        sets may differ (the missing side fills NULL); dtype drift between
        storage and the incoming frame is reconciled via :func:`_union_type`.
        Idempotent: re-upserting the same rows leaves the table unchanged.
        Returns the resulting row count.

        Peak Python memory is the incoming frame; DuckDB spills to disk past
        LAKE_MEMORY_LIMIT and merge cost scales with the increment, not the
        table, so a multi-GB SEP merge never loads into pandas (and fits the
        2GB box). Storage row order is NOT part of the contract — readers
        order in SQL.
        """
        if df is None or df.empty:
            return self.count(table)
        df = _normalize_date_objects(df.copy())
        # Pre-dedupe the incoming frame (the small side) in pandas — stable
        # sort + keep-last, the same rule the merge applies against storage.
        if sort_col and sort_col in df.columns:
            df = df.sort_values(sort_col, kind="stable")
        subset = [k for k in keys if k in df.columns] if keys else None
        df = (df.drop_duplicates(subset=subset or None, keep="last")
                .reset_index(drop=True))
        if not self.exists(table):
            self.write(table, df)
            return len(df)

        con = self._connect()
        out = self.path(table)
        tmp_out = out.parent / (out.name + ".tmp")
        try:
            # Implicit insertion-order preservation buffers the whole COPY
            # pipeline in memory, defeating out-of-core execution under the
            # cap; row order here is governed by the explicit ORDER BY (which
            # is always honored), so drop it for the merge connection only.
            con.execute("SET preserve_insertion_order=false")
            con.register("_lake_incoming", df)
            old_types = {r[0]: r[1] for r in con.execute(
                f"DESCRIBE SELECT * FROM {self.sql_table(table)}").fetchall()}
            new_types = {r[0]: r[1] for r in con.execute(
                "DESCRIBE SELECT * FROM _lake_incoming").fetchall()}
            cols = list(old_types) + [c for c in new_types if c not in old_types]

            def _side(mine: dict, other: dict) -> str:
                parts = []
                for c in cols:
                    if c not in mine:
                        parts.append(f"NULL AS {_q(c)}")
                    elif c in other and mine[c] != other[c]:
                        parts.append(f"CAST({_q(c)} AS "
                                     f"{_union_type(mine[c], other[c])}) AS {_q(c)}")
                    else:
                        parts.append(_q(c))
                return ", ".join(parts)

            # Merge = stream untouched rows + re-rank only key-touched rows.
            # Windowing the WHOLE table per merge OOMs small memory caps (the
            # sort scales with the table); a hash semi/anti-join against the
            # small incoming key set streams the parquet scan and does per-key
            # work only where the increment actually lands. Inlined (not CTE'd)
            # so DuckDB never materializes the big scan for reuse.
            old_expr = (f"(SELECT {_side(old_types, new_types)}, 0 AS __lake_src, "
                        f"file_row_number AS __lake_pos "
                        f"FROM read_parquet('{out.as_posix()}', file_row_number=true))")
            inc_expr = (f"(SELECT {_side(new_types, old_types)}, 1 AS __lake_src, "
                        f"0 AS __lake_pos FROM _lake_incoming)")
            part_cols = [k for k in keys if k in cols] if keys else []
            key_cols = part_cols or cols
            part = ", ".join(_q(c) for c in key_cols)
            match = " AND ".join(
                f"i.{_q(c)} IS NOT DISTINCT FROM o.{_q(c)}" for c in key_cols)
            rank = ["__lake_src DESC", "__lake_pos DESC"]
            if sort_col and sort_col in cols:
                rank.insert(0, f"{_q(sort_col)} DESC NULLS FIRST")
            col_list = ", ".join(_q(c) for c in cols)
            sql = (f"COPY ("
                   f"SELECT {col_list} FROM {old_expr} o WHERE NOT EXISTS "
                   f"(SELECT 1 FROM {inc_expr} i WHERE {match}) "
                   f"UNION ALL "
                   f"SELECT {col_list} FROM ("
                   f"SELECT *, row_number() OVER (PARTITION BY {part} "
                   f"ORDER BY {', '.join(rank)}) AS __lake_rn FROM ("
                   f"SELECT o.* FROM {old_expr} o WHERE EXISTS "
                   f"(SELECT 1 FROM {inc_expr} i WHERE {match}) "
                   f"UNION ALL SELECT * FROM {inc_expr})"
                   f") WHERE __lake_rn = 1"
                   f") TO '{tmp_out.as_posix()}' (FORMAT PARQUET)")
            log.debug("upsert %s: %s", table, sql)
            n = con.execute(sql).fetchone()[0]
        except BaseException:
            con.close()
            tmp_out.unlink(missing_ok=True)
            raise
        con.close()
        os.replace(tmp_out, out)
        return int(n)

    def count(self, table: str) -> int:
        """Row count via parquet metadata in DuckDB (no data scan, no pandas
        load); 0 if the table does not exist."""
        if not self.exists(table):
            return 0
        con = self._connect()
        try:
            return int(con.execute(
                f"SELECT count(*) FROM {self.sql_table(table)}").fetchone()[0])
        finally:
            con.close()

    def max_value(self, table: str, column: str):
        """Max of ``column`` in ``table`` (e.g. the latest ``lastupdated`` for an
        incremental refresh), or None if the table/column is absent/empty —
        computed in DuckDB so the multi-GB tables never load into pandas."""
        if not self.exists(table):
            return None
        con = self._connect()
        try:
            cols = {r[0] for r in con.execute(
                f"DESCRIBE SELECT * FROM {self.sql_table(table)}").fetchall()}
            if column not in cols:
                return None
            val = con.execute(
                f"SELECT max({_q(column)}) FROM {self.sql_table(table)}"
            ).fetchone()[0]
        finally:
            con.close()
        return None if val is None or pd.isna(val) else val

    def query(self, sql: str, params: list | None = None):
        """Run a DuckDB SQL query (optionally parameterized with ``?``
        placeholders — use these for any untrusted value). Reference tables as
        ``read_parquet('<lake>/<table>.parquet')`` via :meth:`sql_table`. Returns
        a pandas DataFrame. The connection is memory-capped + disk-spilling
        (see :meth:`_connect`), so big scans degrade to slow, not to OOM."""
        con = self._connect()
        try:
            return con.execute(sql, params or []).df()
        finally:
            con.close()

    def sql_table(self, table: str) -> str:
        """A ``read_parquet(...)`` expression for ``table``, for use in query()."""
        return f"read_parquet('{self.path(table).as_posix()}')"

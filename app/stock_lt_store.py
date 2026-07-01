"""SQLite tables for the long-term "long buys" engine (shares the stock DB).

`stock_financials` caches raw Massive statements per ticker (refreshed in slices to
respect the 5/min limit — financials are quarterly, so staleness is fine).
`stock_lt_runs`/`_signals` hold each rescore; `stock_lt_holdings` is the
accumulation forward-test (benchmark-relative vs SPY — long-term edge is measured as
excess return, held until a name drops out of the conviction list, no stops).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .store import _add_column_if_missing

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_financials (
  ticker         TEXT PRIMARY KEY,
  fetched_ts     INTEGER,          -- when we pulled it (epoch ms) -> staleness
  diluted_shares REAL,             -- current-period diluted avg shares (for market cap)
  periods_json   TEXT              -- raw Massive periods list (>=2 annual, newest first)
);

CREATE TABLE IF NOT EXISTS stock_lt_runs (
  run_ts        TEXT PRIMARY KEY,
  universe_n    INTEGER,
  scored_n      INTEGER,           -- names with fundamentals + price (rankable)
  survivors_n   INTEGER,           -- passed the value-trap gate
  readings_json TEXT
);

CREATE TABLE IF NOT EXISTS stock_lt_signals (
  run_ts       TEXT,
  ticker       TEXT,
  rank         INTEGER,
  conviction   REAL,
  value_rank   REAL, quality_rank REAL, momentum_rank REAL,
  piotroski    INTEGER,
  altman_z     REAL,
  sector       TEXT,
  price        REAL,
  surfaced     INTEGER,            -- in the top-N conviction list
  detail_json  TEXT,
  PRIMARY KEY (run_ts, ticker)
);
CREATE INDEX IF NOT EXISTS ix_stock_lt_signals_run ON stock_lt_signals(run_ts, rank);

CREATE TABLE IF NOT EXISTS stock_lt_holdings (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker        TEXT,
  opened_run_ts TEXT,
  opened_ts     INTEGER,
  entry         REAL,
  spy_entry     REAL,
  conviction    REAL,
  status        TEXT,              -- OPEN | CLOSED
  closed_ts     INTEGER,
  exit          REAL,
  spy_exit      REAL,
  excess_return REAL,              -- (name % change) - (SPY % change) over the hold
  exit_reason   TEXT,              -- dropped_by_conviction | data_gap
  entry_ts      INTEGER            -- bar ts of the entry close (split re-base anchor)
);
CREATE INDEX IF NOT EXISTS ix_stock_lt_holdings_status ON stock_lt_holdings(status, ticker);
"""


def init_stock_lt_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    _add_column_if_missing(conn, "stock_lt_holdings", "exit_reason", "TEXT")
    _add_column_if_missing(conn, "stock_lt_holdings", "entry_ts", "INTEGER")
    conn.commit()


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


# --- Financials cache --------------------------------------------------------

def upsert_financials(conn: sqlite3.Connection, ticker: str, diluted_shares: float | None,
                      periods: list[dict]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO stock_financials (ticker, fetched_ts, diluted_shares, periods_json) "
        "VALUES (?, ?, ?, ?)",
        (ticker, _now_ms(), diluted_shares, json.dumps(periods, default=str)),
    )
    conn.commit()


def get_financials(conn: sqlite3.Connection, ticker: str) -> dict | None:
    row = conn.execute(
        "SELECT fetched_ts, diluted_shares, periods_json FROM stock_financials WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    if not row:
        return None
    try:
        periods = json.loads(row["periods_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        periods = []
    return {"fetched_ts": row["fetched_ts"], "diluted_shares": row["diluted_shares"],
            "periods": periods}


def financials_freshness(conn: sqlite3.Connection) -> dict[str, int]:
    """{ticker: fetched_ts} for all cached financials (to pick the stalest to refresh)."""
    rows = conn.execute("SELECT ticker, fetched_ts FROM stock_financials").fetchall()
    return {r["ticker"]: (r["fetched_ts"] or 0) for r in rows}


# --- Runs + signals ----------------------------------------------------------

def record_lt_run(conn: sqlite3.Connection, *, run_ts: str, universe_n: int, scored_n: int,
                  survivors_n: int, readings: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO stock_lt_runs (run_ts, universe_n, scored_n, survivors_n, readings_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (run_ts, universe_n, scored_n, survivors_n, json.dumps(readings, default=str)),
    )
    conn.commit()


def record_lt_signals(conn: sqlite3.Connection, run_ts: str, signals: list[dict]) -> None:
    if not signals:
        return
    conn.executemany(
        """
        INSERT OR REPLACE INTO stock_lt_signals
          (run_ts, ticker, rank, conviction, value_rank, quality_rank, momentum_rank,
           piotroski, altman_z, sector, price, surfaced, detail_json)
        VALUES (:run_ts, :ticker, :rank, :conviction, :value_rank, :quality_rank,
                :momentum_rank, :piotroski, :altman_z, :sector, :price, :surfaced, :detail_json)
        """,
        [{**s, "run_ts": run_ts} for s in signals],
    )
    conn.commit()


def latest_lt_run(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM stock_lt_runs ORDER BY run_ts DESC LIMIT 1").fetchone()
    if not row:
        return None
    out = dict(row)
    try:
        out["readings"] = json.loads(out.pop("readings_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        out["readings"] = {}
    return out


def latest_lt_signals(conn: sqlite3.Connection) -> list[dict]:
    latest = conn.execute("SELECT run_ts FROM stock_lt_runs ORDER BY run_ts DESC LIMIT 1").fetchone()
    if not latest:
        return []
    rows = conn.execute(
        "SELECT * FROM stock_lt_signals WHERE run_ts = ? ORDER BY rank ASC", (latest["run_ts"],)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["detail"] = json.loads(d.pop("detail_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["detail"] = {}
        out.append(d)
    return out


def last_lt_run_ts(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT run_ts FROM stock_lt_runs ORDER BY run_ts DESC LIMIT 1").fetchone()
    if not row or not row["run_ts"]:
        return None
    try:
        return datetime.fromisoformat(row["run_ts"])
    except ValueError:
        return None


# --- Holdings (benchmark-relative forward-test) ------------------------------

def open_lt_holdings(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM stock_lt_holdings WHERE status = 'OPEN'").fetchall()
    return [dict(r) for r in rows]


def open_lt_holding(conn: sqlite3.Connection, *, ticker: str, opened_run_ts: str, opened_ts: int,
                    entry: float, spy_entry: float, conviction: float,
                    entry_ts: int | None = None) -> None:
    conn.execute(
        "INSERT INTO stock_lt_holdings (ticker, opened_run_ts, opened_ts, entry, spy_entry, "
        "conviction, status, entry_ts) VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?)",
        (ticker, opened_run_ts, opened_ts, entry, spy_entry, conviction, entry_ts),
    )
    conn.commit()


def close_lt_holding(conn: sqlite3.Connection, hid: int, *, closed_ts: int, exit_price: float,
                     spy_exit: float, excess_return: float,
                     exit_reason: str | None = None) -> None:
    conn.execute(
        "UPDATE stock_lt_holdings SET status='CLOSED', closed_ts=?, exit=?, spy_exit=?, "
        "excess_return=?, exit_reason=? WHERE id=?",
        (closed_ts, exit_price, spy_exit, excess_return, exit_reason, hid),
    )
    conn.commit()


def closed_lt_holdings(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM stock_lt_holdings WHERE status='CLOSED' ORDER BY closed_ts DESC"
    ).fetchall()
    return [dict(r) for r in rows]

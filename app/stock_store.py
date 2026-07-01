"""SQLite ledger for the stock swing tracker (asset-namespaced ``stock_*`` tables).

Lives in the SAME database file as the BTC side (``cfg.db_path``) and reuses
``store.connect`` / ``store.connect_readonly`` (WAL, read-only API) — the two
asset pipelines just own disjoint tables. ``init_stock_db`` is idempotent
(``CREATE TABLE IF NOT EXISTS`` + ``_add_column_if_missing``) so schema changes
are additive migrations, exactly like ``store.init_db``.

Timestamp convention mirrors the BTC side: new time-series tables use integer
epoch **milliseconds** (daily bars/events stamped at UTC midnight); ``stock_runs.run_ts``
keeps ISO text. Small key-value state reuses the shared ``meta`` table with a
``stock:`` key prefix.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .store import _add_column_if_missing  # reuse the BTC migration helper

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_universe (
  ticker    TEXT PRIMARY KEY,
  cik       TEXT,                 -- zero-padded 10-digit CIK (SEC), NULL until resolved
  name      TEXT,
  sector    TEXT,
  active    INTEGER NOT NULL DEFAULT 1,
  added_ts  TEXT                  -- ISO (UTC)
);

CREATE TABLE IF NOT EXISTS stock_prices (
  ticker TEXT,
  ts     INTEGER,                 -- bar date, epoch ms at UTC midnight
  open   REAL, high REAL, low REAL, close REAL, volume REAL,
  source TEXT,                    -- stooq | alpaca | tiingo
  PRIMARY KEY (ticker, ts)
);
CREATE INDEX IF NOT EXISTS ix_stock_prices_tk_ts ON stock_prices(ticker, ts DESC);

CREATE TABLE IF NOT EXISTS stock_earnings (
  ticker       TEXT,
  period       TEXT,              -- report period (YYYY-MM-DD of the fiscal quarter)
  report_ts    INTEGER,           -- announcement date, epoch ms (UTC midnight)
  hour         TEXT,              -- bmo | amc | dmh | '' (before/after/during market)
  actual       REAL,
  estimate     REAL,
  surprise     REAL,              -- actual - estimate
  surprise_pct REAL,              -- surprise / |estimate| * 100
  rev_actual   REAL,
  rev_estimate REAL,
  rev_surprise_pct REAL,          -- revenue surprise % (confluence with EPS)
  PRIMARY KEY (ticker, period)
);
CREATE INDEX IF NOT EXISTS ix_stock_earnings_rts ON stock_earnings(report_ts DESC);

CREATE TABLE IF NOT EXISTS stock_estimates_snap (
  ticker    TEXT,
  snap_ts   INTEGER,              -- when we snapshotted (epoch ms) — accrues revision history
  period    TEXT,
  strong_buy INTEGER, buy INTEGER, hold INTEGER, sell INTEGER, strong_sell INTEGER,
  eps_avg   REAL,                 -- consensus EPS estimate if available (else NULL)
  PRIMARY KEY (ticker, snap_ts)
);

CREATE TABLE IF NOT EXISTS stock_insider (
  accession   TEXT PRIMARY KEY,   -- SEC accession no. (unique per filing)
  ticker      TEXT,
  cik         TEXT,
  insider     TEXT,
  is_officer  INTEGER,
  is_director INTEGER,
  txn_code    TEXT,               -- 'P' open-market buy, 'S' sale, etc.
  txn_ts      INTEGER,            -- transaction date, epoch ms
  shares      REAL,
  price       REAL,
  value       REAL,               -- shares * price (USD)
  filed_ts    INTEGER             -- filing datetime, epoch ms
);
CREATE INDEX IF NOT EXISTS ix_stock_insider_tk_ts ON stock_insider(ticker, txn_ts DESC);

CREATE TABLE IF NOT EXISTS stock_shortvol (
  ticker        TEXT,
  ts            INTEGER,          -- trade date, epoch ms (UTC midnight)
  short_vol     REAL,
  short_exempt  REAL,
  total_vol     REAL,
  PRIMARY KEY (ticker, ts)
);

CREATE TABLE IF NOT EXISTS stock_runs (
  run_ts        TEXT PRIMARY KEY, -- ISO (UTC)
  universe_n    INTEGER,          -- names in the universe this run
  scored_n      INTEGER,          -- names that produced a live setup
  readings_json TEXT              -- run-level context (regime, layer availability, notes)
);

CREATE TABLE IF NOT EXISTS stock_signals (
  run_ts      TEXT,
  ticker      TEXT,
  rank        INTEGER,
  direction   TEXT,               -- BUY | SELL
  archetype   TEXT,               -- pead_drift | momentum | mean_reversion
  composite   REAL,               -- 0..100 ranked blend
  confidence  REAL,               -- 0..1 calibrated prior
  pead        REAL, technical REAL, insider REAL, shortvol REAL, revision REAL,  -- component subscores
  price       REAL,
  entry       REAL, stop REAL, t1 REAL, t2 REAL, atr REAL, rr REAL,
  detail_json TEXT,               -- catalyst detail + per-signal breakdown for the card
  PRIMARY KEY (run_ts, ticker)
);
CREATE INDEX IF NOT EXISTS ix_stock_signals_run ON stock_signals(run_ts, rank);

CREATE TABLE IF NOT EXISTS stock_positions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker        TEXT,
  opened_run_ts TEXT,
  opened_ts     INTEGER,          -- SIGNAL bar ts, epoch ms (fill happens on the next bar)
  direction     TEXT,
  archetype     TEXT,
  confidence    REAL,
  entry         REAL, stop REAL, t1 REAL, t2 REAL, atr REAL,
  time_stop_days INTEGER,         -- per-archetype hard time-exit (from stock_levels)
  status        TEXT,             -- PENDING | OPEN | CLOSED | EXPIRED
  closed_run_ts TEXT,
  closed_ts     INTEGER,
  exit_price    REAL,
  realized_r    REAL,             -- NET (exit-entry)/risk minus costs, signed by direction
  gross_r       REAL,             -- realized R before costs
  cost_r        REAL,             -- round-trip cost charged, in R
  exit_reason   TEXT,             -- stop | t1 | t2 | time | reversal | rebased | unfilled
  mfe_r         REAL,             -- max favorable excursion in R (running)
  mae_r         REAL,             -- max adverse excursion in R (running)
  filled_ts     INTEGER,          -- ENTRY bar ts (the bar whose open filled the position)
  entry_venue   TEXT,             -- price venue that served the entry bar (pinned for repricing)
  entry_bar_close REAL,           -- entry bar's close (split/adjustment re-base detection)
  structure_stop REAL,            -- thesis-invalidation level captured at signal time
  last_reprice_ts INTEGER         -- wall-clock ms of the last successful reprice
);
CREATE INDEX IF NOT EXISTS ix_stock_positions_status ON stock_positions(status, ticker);

CREATE TABLE IF NOT EXISTS stock_alerts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         INTEGER,             -- setup bar ts, epoch ms
  created_at TEXT,                -- wall-clock ISO (UTC)
  ticker     TEXT,
  archetype  TEXT,
  direction  TEXT,
  entry      REAL, stop REAL, t1 REAL, t2 REAL,
  confidence REAL,
  message    TEXT,
  sent       INTEGER DEFAULT 0,
  retried    INTEGER DEFAULT 0    -- failed sends are retried exactly once next run
);
CREATE INDEX IF NOT EXISTS ix_stock_alerts_key ON stock_alerts(ticker, archetype, ts DESC);
"""


def init_stock_db(conn: sqlite3.Connection) -> None:
    """Create/upgrade the stock tables. Idempotent; safe to call every connect."""
    conn.executescript(_SCHEMA)
    # (future additive columns go here via _add_column_if_missing, e.g.)
    _add_column_if_missing(conn, "stock_signals", "revision", "REAL")
    _add_column_if_missing(conn, "stock_positions", "time_stop_days", "INTEGER")
    _add_column_if_missing(conn, "stock_positions", "gross_r", "REAL")
    _add_column_if_missing(conn, "stock_positions", "cost_r", "REAL")
    _add_column_if_missing(conn, "stock_positions", "filled_ts", "INTEGER")
    _add_column_if_missing(conn, "stock_positions", "entry_venue", "TEXT")
    _add_column_if_missing(conn, "stock_positions", "entry_bar_close", "REAL")
    _add_column_if_missing(conn, "stock_positions", "structure_stop", "REAL")
    _add_column_if_missing(conn, "stock_positions", "last_reprice_ts", "INTEGER")
    _add_column_if_missing(conn, "stock_alerts", "retried", "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "stock_earnings", "rev_actual", "REAL")
    _add_column_if_missing(conn, "stock_earnings", "rev_estimate", "REAL")
    _add_column_if_missing(conn, "stock_earnings", "rev_surprise_pct", "REAL")
    conn.commit()


def _midnight_ms(d: "datetime | str") -> int:
    """Epoch ms at UTC midnight for a date or 'YYYY-MM-DD' string."""
    if isinstance(d, str):
        d = datetime.strptime(d[:10], "%Y-%m-%d")
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


# --- Universe ----------------------------------------------------------------

def upsert_universe(conn: sqlite3.Connection,
                    rows: list[tuple[str, str | None, str | None, str | None]]) -> None:
    """rows = [(ticker, name, sector, cik|None), ...]. Keeps existing CIK if the new
    one is None (CIK is resolved lazily from SEC and shouldn't be clobbered)."""
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """
        INSERT INTO stock_universe (ticker, name, sector, cik, active, added_ts)
        VALUES (?, ?, ?, ?, 1, ?)
        ON CONFLICT(ticker) DO UPDATE SET
          name=excluded.name, sector=excluded.sector, active=1,
          cik=COALESCE(excluded.cik, stock_universe.cik)
        """,
        [(t, n, s, c, now) for (t, n, s, c) in rows],
    )
    conn.commit()


def set_cik(conn: sqlite3.Connection, ticker: str, cik: str) -> None:
    conn.execute("UPDATE stock_universe SET cik = ? WHERE ticker = ?", (cik, ticker))
    conn.commit()


def get_universe(conn: sqlite3.Connection, active_only: bool = True) -> list[dict]:
    q = "SELECT ticker, cik, name, sector, active FROM stock_universe"
    if active_only:
        q += " WHERE active = 1"
    q += " ORDER BY ticker"
    return [dict(r) for r in conn.execute(q).fetchall()]


# --- Prices ------------------------------------------------------------------

def upsert_prices(conn: sqlite3.Connection, ticker: str,
                  rows: list[tuple[int, float, float, float, float, float]],
                  source: str | None = None) -> None:
    """rows = [(ts_ms, open, high, low, close, volume), ...]."""
    if not rows:
        return
    conn.executemany(
        """
        INSERT OR REPLACE INTO stock_prices (ticker, ts, open, high, low, close, volume, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [(ticker, *r, source) for r in rows],
    )
    conn.commit()


def recent_prices(conn: sqlite3.Connection, ticker: str, limit: int = 260) -> list[dict]:
    """Most recent ``limit`` daily bars for a ticker, OLDEST->NEWEST."""
    rows = conn.execute(
        "SELECT ts, open, high, low, close, volume FROM ("
        "  SELECT * FROM stock_prices WHERE ticker = ? ORDER BY ts DESC LIMIT ?"
        ") ORDER BY ts ASC",
        (ticker, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def prices_after(conn: sqlite3.Connection, ticker: str, ts_ms: int) -> list[dict]:
    """Daily bars strictly AFTER ``ts_ms`` (for repricing an open position)."""
    rows = conn.execute(
        "SELECT ts, open, high, low, close, volume FROM stock_prices "
        "WHERE ticker = ? AND ts > ? ORDER BY ts ASC",
        (ticker, ts_ms),
    ).fetchall()
    return [dict(r) for r in rows]


def close_at(conn: sqlite3.Connection, ticker: str, ts_ms: int) -> float | None:
    """The stored close for one exact bar date — the series is kept on the venue's
    CURRENT adjustment basis by the daily collector, so this re-expresses an old
    price (e.g. a holding's entry) in today's basis after a split."""
    row = conn.execute(
        "SELECT close FROM stock_prices WHERE ticker = ? AND ts = ?", (ticker, ts_ms)
    ).fetchone()
    return row["close"] if row and row["close"] is not None else None


def last_price_ts(conn: sqlite3.Connection, ticker: str) -> int | None:
    row = conn.execute(
        "SELECT MAX(ts) AS ts FROM stock_prices WHERE ticker = ?", (ticker,)
    ).fetchone()
    return row["ts"] if row and row["ts"] is not None else None


# --- Earnings + estimates ----------------------------------------------------

def upsert_earnings(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """rows = [{ticker, period, report_ts, hour, actual, estimate, surprise, surprise_pct}]."""
    if not rows:
        return
    conn.executemany(
        """
        INSERT OR REPLACE INTO stock_earnings
          (ticker, period, report_ts, hour, actual, estimate, surprise, surprise_pct,
           rev_actual, rev_estimate, rev_surprise_pct)
        VALUES (:ticker, :period, :report_ts, :hour, :actual, :estimate, :surprise,
                :surprise_pct, :rev_actual, :rev_estimate, :rev_surprise_pct)
        """,
        rows,
    )
    conn.commit()


def earnings_since(conn: sqlite3.Connection, since_ts: int) -> list[dict]:
    """All earnings events reported at/after ``since_ts`` across the universe."""
    rows = conn.execute(
        "SELECT * FROM stock_earnings WHERE report_ts >= ? ORDER BY report_ts DESC",
        (since_ts,),
    ).fetchall()
    return [dict(r) for r in rows]


def latest_earnings(conn: sqlite3.Connection, ticker: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM stock_earnings WHERE ticker = ? ORDER BY report_ts DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    return dict(row) if row else None


def record_estimate_snap(conn: sqlite3.Connection, *, ticker: str, snap_ts: int,
                         period: str | None, strong_buy: int | None, buy: int | None,
                         hold: int | None, sell: int | None, strong_sell: int | None,
                         eps_avg: float | None) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO stock_estimates_snap
          (ticker, snap_ts, period, strong_buy, buy, hold, sell, strong_sell, eps_avg)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ticker, snap_ts, period, strong_buy, buy, hold, sell, strong_sell, eps_avg),
    )
    conn.commit()


def last_two_estimate_snaps(conn: sqlite3.Connection, ticker: str) -> list[dict]:
    """The two most recent estimate snapshots (newest first) — the delta between
    them is the accrued revision-momentum read."""
    rows = conn.execute(
        "SELECT * FROM stock_estimates_snap WHERE ticker = ? ORDER BY snap_ts DESC LIMIT 2",
        (ticker,),
    ).fetchall()
    return [dict(r) for r in rows]


# --- Insider (Form 4) --------------------------------------------------------

def upsert_insider(conn: sqlite3.Connection, rows: list[dict]) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT OR REPLACE INTO stock_insider
          (accession, ticker, cik, insider, is_officer, is_director, txn_code,
           txn_ts, shares, price, value, filed_ts)
        VALUES (:accession, :ticker, :cik, :insider, :is_officer, :is_director,
                :txn_code, :txn_ts, :shares, :price, :value, :filed_ts)
        """,
        rows,
    )
    conn.commit()


def insider_cluster(conn: sqlite3.Connection, ticker: str, since_ts: int) -> dict:
    """Open-market BUY (code 'P') cluster read for a ticker since ``since_ts``:
    distinct insiders, total USD, officer/director involvement."""
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT insider) AS buyers, COALESCE(SUM(value), 0) AS usd,
               MAX(is_officer) AS any_officer, MAX(is_director) AS any_director,
               MAX(txn_ts) AS last_ts
        FROM stock_insider
        WHERE ticker = ? AND txn_code = 'P' AND txn_ts >= ?
        """,
        (ticker, since_ts),
    ).fetchone()
    d = dict(row) if row else {}
    return {"buyers": d.get("buyers") or 0, "usd": d.get("usd") or 0.0,
            "any_officer": bool(d.get("any_officer")), "any_director": bool(d.get("any_director")),
            "last_ts": d.get("last_ts")}


# --- Short volume (FINRA) ----------------------------------------------------

def upsert_shortvol(conn: sqlite3.Connection, rows: list[dict]) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT OR REPLACE INTO stock_shortvol (ticker, ts, short_vol, short_exempt, total_vol)
        VALUES (:ticker, :ts, :short_vol, :short_exempt, :total_vol)
        """,
        rows,
    )
    conn.commit()


def recent_shortvol(conn: sqlite3.Connection, ticker: str, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT ts, short_vol, short_exempt, total_vol FROM ("
        "  SELECT * FROM stock_shortvol WHERE ticker = ? ORDER BY ts DESC LIMIT ?"
        ") ORDER BY ts ASC",
        (ticker, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# --- Runs + signals ----------------------------------------------------------

def record_stock_run(conn: sqlite3.Connection, *, run_ts: str, universe_n: int,
                     scored_n: int, readings: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO stock_runs (run_ts, universe_n, scored_n, readings_json) "
        "VALUES (?, ?, ?, ?)",
        (run_ts, universe_n, scored_n, json.dumps(readings, default=str)),
    )
    conn.commit()


def record_stock_signals(conn: sqlite3.Connection, run_ts: str, signals: list[dict]) -> None:
    if not signals:
        return
    conn.executemany(
        """
        INSERT OR REPLACE INTO stock_signals
          (run_ts, ticker, rank, direction, archetype, composite, confidence,
           pead, technical, insider, shortvol, revision, price, entry, stop, t1, t2,
           atr, rr, detail_json)
        VALUES (:run_ts, :ticker, :rank, :direction, :archetype, :composite, :confidence,
                :pead, :technical, :insider, :shortvol, :revision, :price, :entry, :stop,
                :t1, :t2, :atr, :rr, :detail_json)
        """,
        [{**s, "run_ts": run_ts} for s in signals],
    )
    conn.commit()


def latest_stock_run(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM stock_runs ORDER BY run_ts DESC LIMIT 1").fetchone()
    if not row:
        return None
    out = dict(row)
    try:
        out["readings"] = json.loads(out.pop("readings_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        out["readings"] = {}
    return out


def recent_stock_runs(conn: sqlite3.Connection, limit: int = 3) -> list[dict]:
    """Most recent runs (newest first) with parsed readings — the health endpoint
    reads per-layer outcome counts across these to spot a dead-but-configured source."""
    rows = conn.execute(
        "SELECT * FROM stock_runs ORDER BY run_ts DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["readings"] = json.loads(d.pop("readings_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["readings"] = {}
        out.append(d)
    return out


def latest_stock_signals(conn: sqlite3.Connection) -> list[dict]:
    """Ranked signals of the most recent run, best-rank first, detail parsed."""
    latest = conn.execute("SELECT run_ts FROM stock_runs ORDER BY run_ts DESC LIMIT 1").fetchone()
    if not latest:
        return []
    rows = conn.execute(
        "SELECT * FROM stock_signals WHERE run_ts = ? ORDER BY rank ASC", (latest["run_ts"],)
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


def last_stock_run_ts(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT run_ts FROM stock_runs ORDER BY run_ts DESC LIMIT 1").fetchone()
    if not row or not row["run_ts"]:
        return None
    try:
        return datetime.fromisoformat(row["run_ts"])
    except ValueError:
        return None


# --- Positions (lifecycle == forward-test) -----------------------------------

def insert_position(conn: sqlite3.Connection, *, ticker: str, opened_run_ts: str,
                    opened_ts: int, direction: str, archetype: str, confidence: float,
                    entry: float, stop: float, t1: float, t2: float, atr: float,
                    time_stop_days: int, status: str = "OPEN",
                    structure_stop: float | None = None,
                    entry_venue: str | None = None) -> int:
    """``status='PENDING'`` inserts an unfilled setup: levels are provisional (the
    signal close) until the next run fills at the following bar's open."""
    cur = conn.execute(
        """
        INSERT INTO stock_positions
          (ticker, opened_run_ts, opened_ts, direction, archetype, confidence,
           entry, stop, t1, t2, atr, time_stop_days, status, mfe_r, mae_r,
           structure_stop, entry_venue)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
        """,
        (ticker, opened_run_ts, opened_ts, direction, archetype, confidence,
         entry, stop, t1, t2, atr, time_stop_days, status, structure_stop, entry_venue),
    )
    conn.commit()
    return int(cur.lastrowid)


def open_positions(conn: sqlite3.Connection) -> list[dict]:
    """Filled positions only — PENDING setups carry no open risk yet."""
    rows = conn.execute("SELECT * FROM stock_positions WHERE status = 'OPEN'").fetchall()
    return [dict(r) for r in rows]


def pending_positions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM stock_positions WHERE status = 'PENDING'").fetchall()
    return [dict(r) for r in rows]


def has_open_position(conn: sqlite3.Connection, ticker: str, archetype: str) -> bool:
    """True while a position is live OR still pending fill (dedup gate)."""
    row = conn.execute(
        "SELECT 1 FROM stock_positions WHERE ticker = ? AND archetype = ? "
        "AND status IN ('OPEN', 'PENDING') LIMIT 1",
        (ticker, archetype),
    ).fetchone()
    return row is not None


def fill_position(conn: sqlite3.Connection, pos_id: int, *, filled_ts: int, entry: float,
                  stop: float, t1: float, t2: float, entry_venue: str | None,
                  entry_bar_close: float, last_reprice_ts: int) -> None:
    """PENDING -> OPEN at the next bar's open; levels are recomputed at the fill."""
    conn.execute(
        """
        UPDATE stock_positions SET status='OPEN', filled_ts=?, entry=?, stop=?, t1=?, t2=?,
          entry_venue=?, entry_bar_close=?, last_reprice_ts=?
        WHERE id=?
        """,
        (filled_ts, entry, stop, t1, t2, entry_venue, entry_bar_close,
         last_reprice_ts, pos_id),
    )
    conn.commit()


def expire_position(conn: sqlite3.Connection, pos_id: int, *, closed_run_ts: str,
                    closed_ts: int) -> None:
    """A PENDING setup that never filled — never entered, never part of the record."""
    conn.execute(
        "UPDATE stock_positions SET status='EXPIRED', closed_run_ts=?, closed_ts=?, "
        "exit_reason='unfilled' WHERE id=?",
        (closed_run_ts, closed_ts, pos_id),
    )
    conn.commit()


def rebase_position(conn: sqlite3.Connection, pos_id: int, *, entry: float, stop: float,
                    t1: float, t2: float, atr: float | None,
                    entry_bar_close: float) -> None:
    """Rescale stored levels after a detected split/adjustment re-base of the series."""
    conn.execute(
        "UPDATE stock_positions SET entry=?, stop=?, t1=?, t2=?, atr=?, entry_bar_close=? "
        "WHERE id=?",
        (entry, stop, t1, t2, atr, entry_bar_close, pos_id),
    )
    conn.commit()


def void_position(conn: sqlite3.Connection, pos_id: int, *, closed_run_ts: str,
                  closed_ts: int) -> None:
    """Void a position whose price basis can't be verified (entry bar vanished from
    the re-fetched series). exit_reason='rebased' rows are excluded from win-rate
    aggregation — a corrupted basis must never pollute the track record."""
    conn.execute(
        "UPDATE stock_positions SET status='CLOSED', closed_run_ts=?, closed_ts=?, "
        "exit_reason='rebased', realized_r=NULL, gross_r=NULL, cost_r=NULL WHERE id=?",
        (closed_run_ts, closed_ts, pos_id),
    )
    conn.commit()


def update_position_excursion(conn: sqlite3.Connection, pos_id: int,
                              mfe_r: float, mae_r: float,
                              last_reprice_ts: int | None = None) -> None:
    if last_reprice_ts is not None:
        conn.execute(
            "UPDATE stock_positions SET mfe_r = ?, mae_r = ?, last_reprice_ts = ? WHERE id = ?",
            (mfe_r, mae_r, last_reprice_ts, pos_id))
    else:
        conn.execute("UPDATE stock_positions SET mfe_r = ?, mae_r = ? WHERE id = ?",
                     (mfe_r, mae_r, pos_id))
    conn.commit()


def close_position(conn: sqlite3.Connection, pos_id: int, *, closed_run_ts: str,
                   closed_ts: int, exit_price: float, realized_r: float,
                   exit_reason: str, mfe_r: float, mae_r: float,
                   gross_r: float | None = None, cost_r: float | None = None) -> None:
    conn.execute(
        """
        UPDATE stock_positions SET status='CLOSED', closed_run_ts=?, closed_ts=?,
          exit_price=?, realized_r=?, gross_r=?, cost_r=?, exit_reason=?, mfe_r=?, mae_r=?
        WHERE id=?
        """,
        (closed_run_ts, closed_ts, exit_price, realized_r, gross_r, cost_r,
         exit_reason, mfe_r, mae_r, pos_id),
    )
    conn.commit()


def all_positions(conn: sqlite3.Connection, limit: int = 500) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM stock_positions ORDER BY opened_ts DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def closed_positions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM stock_positions WHERE status='CLOSED' ORDER BY closed_ts ASC"
    ).fetchall()
    return [dict(r) for r in rows]


# --- Alerts (cooldown memory) ------------------------------------------------

def last_stock_alert(conn: sqlite3.Connection, ticker: str, archetype: str) -> dict | None:
    """Most recent SUCCESSFULLY-SENT alert for a (ticker, archetype) — cooldown memory."""
    row = conn.execute(
        "SELECT ts, created_at FROM stock_alerts "
        "WHERE ticker = ? AND archetype = ? AND sent = 1 ORDER BY ts DESC LIMIT 1",
        (ticker, archetype),
    ).fetchone()
    return dict(row) if row else None


def record_stock_alert(conn: sqlite3.Connection, *, ts: int, created_at: str, ticker: str,
                       archetype: str, direction: str, entry: float, stop: float,
                       t1: float, t2: float, confidence: float, message: str,
                       sent: bool) -> None:
    conn.execute(
        """
        INSERT INTO stock_alerts
          (ts, created_at, ticker, archetype, direction, entry, stop, t1, t2, confidence, message, sent)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ts, created_at, ticker, archetype, direction, entry, stop, t1, t2,
         confidence, message, int(sent)),
    )
    conn.commit()


def recent_stock_alerts(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM stock_alerts ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def unsent_stock_alerts(conn: sqlite3.Connection) -> list[dict]:
    """Alert rows whose send failed and that haven't been retried yet (retry-once)."""
    rows = conn.execute(
        "SELECT * FROM stock_alerts WHERE sent = 0 AND COALESCE(retried, 0) = 0 "
        "ORDER BY id ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def mark_stock_alert_retry(conn: sqlite3.Connection, alert_id: int, sent: bool) -> None:
    """Record the single retry attempt; ``sent=1`` also (re)arms the cooldown."""
    conn.execute("UPDATE stock_alerts SET retried = 1, sent = ? WHERE id = ?",
                 (int(sent), alert_id))
    conn.commit()


# --- Retention ---------------------------------------------------------------

def prune_stock(conn: sqlite3.Connection, days: int = 500) -> None:
    """Drop old high-volume rows; keep runs/signals/positions/alerts (valuable history)."""
    cutoff_ms = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)
    conn.execute("DELETE FROM stock_prices WHERE ts < ?", (cutoff_ms,))
    conn.execute("DELETE FROM stock_shortvol WHERE ts < ?", (cutoff_ms,))
    conn.commit()

"""SQLite ledger.

Long-term runs live in `runs` (unchanged). The pivot adds time-series capture
(`candles`, `derivs`), short-term signal history (`st_signals`), and the
short-term alert cooldown memory (`st_alerts`). SQLite stays the only state and
the debounce memory; WAL lets the collector crons write while the read-only API
reads concurrently.

Timestamp convention for the new tables: integer epoch **milliseconds** (matches
exchange candle timestamps). `runs.run_ts` keeps its existing ISO-text form.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_ts        TEXT PRIMARY KEY,     -- ISO timestamp (UTC)
  price         REAL,
  composite     REAL,                 -- 0-100
  tier          TEXT,                 -- NEUTRAL | WATCH | ACCUMULATE | DEEP_VALUE
  active_cats   TEXT,                 -- comma list of categories that had data
  readings_json TEXT,                 -- full per-indicator readings + sub-scores for later calibration
  tier_alerted  INTEGER DEFAULT 0,
  flash_alerted INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS candles (
  timeframe TEXT,                      -- 4h | 1d | 1w
  ts        INTEGER,                   -- candle open time, epoch ms (UTC)
  open      REAL, high REAL, low REAL, close REAL, volume REAL,
  PRIMARY KEY (timeframe, ts)
);
CREATE INDEX IF NOT EXISTS ix_candles_tf_ts ON candles(timeframe, ts DESC);

CREATE TABLE IF NOT EXISTS derivs (
  ts          INTEGER PRIMARY KEY,     -- epoch ms (UTC)
  funding     REAL,                    -- latest 8h funding fraction
  oi          REAL,                    -- open interest (contracts)
  oi_chg_pct  REAL                     -- % change vs the prior stored sample window
);

CREATE TABLE IF NOT EXISTS st_signals (
  ts              INTEGER,             -- evaluated candle ts, epoch ms (UTC)
  timeframe       TEXT,
  price           REAL,
  st_score        REAL,                -- -100..+100 (two-sided)
  st_state        TEXT,                -- STRONG_BUY|BUY|NEUTRAL|SELL|STRONG_SELL
  indicators_json TEXT,                -- full indicator readings for calibration
  PRIMARY KEY (timeframe, ts)
);
CREATE INDEX IF NOT EXISTS ix_st_signals_ts ON st_signals(ts DESC);

CREATE TABLE IF NOT EXISTS st_alerts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER,                 -- the candle ts the trigger fired on (epoch ms)
  created_at  TEXT,                    -- wall-clock ISO (UTC) when the alert was decided
  trigger_key TEXT,
  timeframe   TEXT,
  direction   TEXT,                    -- BUY | SELL
  price       REAL,
  message     TEXT,
  sent        INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_st_alerts_key ON st_alerts(trigger_key, timeframe, ts DESC);

CREATE TABLE IF NOT EXISTS subscribers (
  email      TEXT PRIMARY KEY,        -- lowercased
  token      TEXT UNIQUE NOT NULL,    -- unguessable unsubscribe capability (secrets.token_urlsafe)
  active     INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL            -- ISO timestamp (UTC)
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # WAL so the collector can write while the read-only API reads concurrently.
    # Harmless for :memory: (PRAGMA is a no-op there).
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
    except sqlite3.Error:
        pass
    return conn


def connect_readonly(db_path: str) -> sqlite3.Connection:
    """Open the DB read-only (for the API) so it can never corrupt collector writes."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


# --- Long-term (runs) --------------------------------------------------------

def last_tier(conn: sqlite3.Connection) -> str:
    """Most recent recorded tier; 'NEUTRAL' if the ledger is empty."""
    row = conn.execute(
        "SELECT tier FROM runs ORDER BY run_ts DESC LIMIT 1"
    ).fetchone()
    return row["tier"] if row and row["tier"] else "NEUTRAL"


def last_flash_at(conn: sqlite3.Connection) -> datetime | None:
    """Timestamp of the most recent run that fired an acute-capitulation flash."""
    row = conn.execute(
        "SELECT run_ts FROM runs WHERE flash_alerted = 1 ORDER BY run_ts DESC LIMIT 1"
    ).fetchone()
    if not row or not row["run_ts"]:
        return None
    try:
        return datetime.fromisoformat(row["run_ts"])
    except ValueError:
        return None


def latest_run(conn: sqlite3.Connection) -> dict | None:
    """The whole latest runs row as a dict (for the API). None if empty."""
    row = conn.execute("SELECT * FROM runs ORDER BY run_ts DESC LIMIT 1").fetchone()
    if not row:
        return None
    out = dict(row)
    try:
        out["readings"] = json.loads(out.pop("readings_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        out["readings"] = {}
    return out


def last_run_ts(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT run_ts FROM runs ORDER BY run_ts DESC LIMIT 1").fetchone()
    if not row or not row["run_ts"]:
        return None
    try:
        return datetime.fromisoformat(row["run_ts"])
    except ValueError:
        return None


def record_run(conn: sqlite3.Connection, *, run_ts: str, price: float | None,
               composite: float, tier: str, active_cats: list[str],
               readings: dict, tier_alerted: bool, flash_alerted: bool) -> None:
    """Persist one run. ``readings`` should hold raw values AND sub-scores."""
    conn.execute(
        """
        INSERT OR REPLACE INTO runs
          (run_ts, price, composite, tier, active_cats, readings_json,
           tier_alerted, flash_alerted)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_ts,
            price,
            composite,
            tier,
            ",".join(active_cats),
            json.dumps(readings, default=str),
            int(tier_alerted),
            int(flash_alerted),
        ),
    )
    conn.commit()


# --- Time-series capture (candles, derivs) -----------------------------------

def upsert_candles(conn: sqlite3.Connection, timeframe: str,
                   rows: list[tuple[int, float, float, float, float, float]]) -> None:
    """Insert/replace candles. ``rows`` = [(ts_ms, open, high, low, close, volume), ...]."""
    if not rows:
        return
    conn.executemany(
        """
        INSERT OR REPLACE INTO candles (timeframe, ts, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [(timeframe, *r) for r in rows],
    )
    conn.commit()


def recent_candles(conn: sqlite3.Connection, timeframe: str, limit: int = 400) -> list[dict]:
    """Most recent ``limit`` candles for a timeframe, returned OLDEST->NEWEST."""
    rows = conn.execute(
        """
        SELECT ts, open, high, low, close, volume FROM (
            SELECT * FROM candles WHERE timeframe = ? ORDER BY ts DESC LIMIT ?
        ) ORDER BY ts ASC
        """,
        (timeframe, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def record_derivs(conn: sqlite3.Connection, *, ts: int, funding: float | None,
                  oi: float | None, oi_chg_pct: float | None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO derivs (ts, funding, oi, oi_chg_pct) VALUES (?, ?, ?, ?)",
        (ts, funding, oi, oi_chg_pct),
    )
    conn.commit()


def recent_derivs(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    """Most recent ``limit`` deriv samples, OLDEST->NEWEST."""
    rows = conn.execute(
        """
        SELECT ts, funding, oi, oi_chg_pct FROM (
            SELECT * FROM derivs ORDER BY ts DESC LIMIT ?
        ) ORDER BY ts ASC
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def latest_oi(conn: sqlite3.Connection) -> float | None:
    """Most recent non-null open interest sample, or None."""
    row = conn.execute(
        "SELECT oi FROM derivs WHERE oi IS NOT NULL ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    return row["oi"] if row else None


def oi_at_or_before(conn: sqlite3.Connection, ts_ms: int) -> float | None:
    """Open interest from the newest sample at or before ``ts_ms`` (epoch ms).

    Timestamp-bounded (not count-based) so a baseline lookback tolerates the
    10-min collector cadence and any gaps — used to derive a free long-term
    ``oi_flush`` (% OI change over a window) when no paid Coinglass key is set.
    """
    row = conn.execute(
        "SELECT oi FROM derivs WHERE ts <= ? AND oi IS NOT NULL ORDER BY ts DESC LIMIT 1",
        (ts_ms,),
    ).fetchone()
    return row["oi"] if row else None


# --- Short-term signals + alert cooldown -------------------------------------

def record_st_signal(conn: sqlite3.Connection, *, ts: int, timeframe: str,
                     price: float, st_score: float, st_state: str,
                     indicators: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO st_signals
          (ts, timeframe, price, st_score, st_state, indicators_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (ts, timeframe, price, st_score, st_state, json.dumps(indicators, default=str)),
    )
    conn.commit()


def latest_st_signal(conn: sqlite3.Connection, timeframe: str | None = None) -> dict | None:
    if timeframe:
        row = conn.execute(
            "SELECT * FROM st_signals WHERE timeframe = ? ORDER BY ts DESC LIMIT 1",
            (timeframe,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM st_signals ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    out = dict(row)
    try:
        out["indicators"] = json.loads(out.pop("indicators_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        out["indicators"] = {}
    return out


def last_st_alert(conn: sqlite3.Connection, trigger_key: str, timeframe: str) -> dict | None:
    """The most recent alert for a (trigger_key, timeframe) pair — the cooldown memory."""
    row = conn.execute(
        """
        SELECT ts, created_at FROM st_alerts
        WHERE trigger_key = ? AND timeframe = ?
        ORDER BY ts DESC LIMIT 1
        """,
        (trigger_key, timeframe),
    ).fetchone()
    return dict(row) if row else None


def record_st_alert(conn: sqlite3.Connection, *, ts: int, created_at: str,
                    trigger_key: str, timeframe: str, direction: str,
                    price: float, message: str, sent: bool) -> None:
    conn.execute(
        """
        INSERT INTO st_alerts
          (ts, created_at, trigger_key, timeframe, direction, price, message, sent)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ts, created_at, trigger_key, timeframe, direction, price, message, int(sent)),
    )
    conn.commit()


def recent_st_alerts(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM st_alerts ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def recent_run_alerts(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Recent long-term runs that fired a tier or flash alert (for the merged feed).
    Includes parsed `readings` so the API can reconstruct the alert's reasoning."""
    rows = conn.execute(
        """
        SELECT run_ts, price, composite, tier, tier_alerted, flash_alerted, readings_json
        FROM runs WHERE tier_alerted = 1 OR flash_alerted = 1
        ORDER BY run_ts DESC LIMIT ?
        """,
        (limit,),
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


def last_collect_ts(conn: sqlite3.Connection) -> datetime | None:
    """Wall-clock time of the most recent collection (for the watchdog).

    Uses the `derivs` table, which is stamped with the collection wall-clock time
    on every run — NOT `st_signals.ts`, which is the (hours-old) candle open time.
    """
    row = conn.execute("SELECT MAX(ts) AS ts FROM derivs").fetchone()
    if not row or row["ts"] is None:
        return None
    return datetime.fromtimestamp(row["ts"] / 1000, tz=timezone.utc)


# --- Email subscribers (alert broadcast list + unsubscribe memory) -----------

def upsert_subscriber(conn: sqlite3.Connection, *, email: str, token: str,
                      created_at: str) -> tuple[str, bool]:
    """Subscribe an email. Returns (token, is_new).

    Idempotent: re-subscribing an existing address re-activates it and keeps its
    original unsubscribe token (so old unsubscribe links stay valid).
    """
    email = email.strip().lower()
    row = conn.execute(
        "SELECT token FROM subscribers WHERE email = ?", (email,)
    ).fetchone()
    if row:
        conn.execute("UPDATE subscribers SET active = 1 WHERE email = ?", (email,))
        conn.commit()
        return row["token"], False
    conn.execute(
        "INSERT INTO subscribers (email, token, active, created_at) VALUES (?, ?, 1, ?)",
        (email, token, created_at),
    )
    conn.commit()
    return token, True


def deactivate_subscriber(conn: sqlite3.Connection, token: str) -> str | None:
    """Mark a subscriber inactive by their unsubscribe token. Returns the email
    if the token matched (active or not), else None. Idempotent."""
    row = conn.execute(
        "SELECT email FROM subscribers WHERE token = ?", (token,)
    ).fetchone()
    if not row:
        return None
    conn.execute("UPDATE subscribers SET active = 0 WHERE token = ?", (token,))
    conn.commit()
    return row["email"]


def list_active_subscribers(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """All active subscribers as [(email, token), ...], oldest first."""
    rows = conn.execute(
        "SELECT email, token FROM subscribers WHERE active = 1 ORDER BY created_at"
    ).fetchall()
    return [(r["email"], r["token"]) for r in rows]


# --- Retention ---------------------------------------------------------------

def prune(conn: sqlite3.Connection, days: int = 400) -> None:
    """Drop high-volume time-series older than ``days``. Keeps `runs` and `st_alerts`
    (low volume, valuable history)."""
    cutoff_ms = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)
    conn.execute("DELETE FROM candles WHERE ts < ?", (cutoff_ms,))
    conn.execute("DELETE FROM derivs WHERE ts < ?", (cutoff_ms,))
    conn.execute("DELETE FROM st_signals WHERE ts < ?", (cutoff_ms,))
    conn.commit()

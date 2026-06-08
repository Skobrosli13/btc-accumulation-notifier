"""SQLite ledger.

Stores every run's readings, composite, tier, and alert flags. Doubles as the
debounce memory (last tier, last flash timestamp) and as a growing dataset to
watch the signal evolve and recalibrate thresholds over time.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

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
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


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

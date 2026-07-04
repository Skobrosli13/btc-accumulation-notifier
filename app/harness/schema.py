"""Harness schema — events / studies / study_results / fills / decisions (§5.1).

Operational state in the app SQLite (research SERIES live in the Parquet lake;
these tables hold what must survive restarts: registered studies, emitted
events, machine-written results, paper fills, discipline decisions).

Follows the codebase's migration convention: ``init_harness_db`` runs
CREATE TABLE IF NOT EXISTS on every connect (idempotent, additive; no migration
files). Emitters INSERT OR IGNORE events (the UNIQUE key dedupes re-runs);
``study_results`` rows are machine-written by scripts/study.py ONLY — never
hand-edited (working agreement #2).
"""
from __future__ import annotations

import json
import sqlite3

_DDL = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  study TEXT NOT NULL,
  asset TEXT CHECK(asset IN ('EQ','BTC')),
  permaticker TEXT,
  cik TEXT,
  ticker TEXT,
  event_ts INTEGER NOT NULL,            -- epoch ms (codebase convention)
  direction TEXT CHECK(direction IN ('LONG','SHORT')),
  strength REAL,
  tier TEXT,                            -- cap tier at event time (EQ)
  sector TEXT,
  days_since_earnings INTEGER,
  meta JSON,
  ingested_at INTEGER,
  UNIQUE(study, permaticker, event_ts)
);
CREATE INDEX IF NOT EXISTS ix_events_study_ts ON events(study, event_ts);

CREATE TABLE IF NOT EXISTS studies (
  name TEXT PRIMARY KEY,
  asset TEXT,
  evaluator TEXT CHECK(evaluator IN ('car','ts','portfolio')),
  tier TEXT CHECK(tier IN ('alpha','policy','premium')),
  spec_path TEXT,                       -- studies/<name>.md pre-registration
  registered_at INTEGER,                -- epoch ms; the OOS/LIVE boundary
  status TEXT CHECK(status IN
    ('REGISTERED','RUNNING','PROMOTED','KILLED','EXTEND','WATCHLIST')),
  verdict_at INTEGER,
  primary_horizon INTEGER               -- sessions (EQ) / days (BTC)
);

CREATE TABLE IF NOT EXISTS study_results (
  study TEXT,
  segment TEXT CHECK(segment IN ('IS','OOS','LIVE','PLACEBO')),
  horizon INTEGER,
  tier TEXT,                            -- cap tier the row aggregates ('' = all)
  n_events INTEGER,
  n_months INTEGER,
  mean_car REAL,
  t_clustered REAL,
  win_rate REAL,
  exp_gross REAL,
  exp_net REAL,
  exp_after_tax REAL,
  emitter_sha TEXT,                     -- anti-drift stamp (§5.6)
  params_hash TEXT,
  computed_at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_results_study ON study_results(study, segment, horizon);

CREATE TABLE IF NOT EXISTS fills (
  event_id INTEGER,
  asset TEXT,
  side TEXT,
  qty REAL,
  limit_px REAL,
  fill_px REAL,
  fill_ts INTEGER,
  venue TEXT,
  slippage_bps REAL
);

CREATE TABLE IF NOT EXISTS decisions (
  event_id INTEGER,
  system_action TEXT,
  user_action TEXT,
  reason_code TEXT,
  ts INTEGER
);
"""


def init_harness_db(conn: sqlite3.Connection) -> None:
    """Idempotent DDL — safe on every connect (additive-migration convention)."""
    conn.executescript(_DDL)
    conn.commit()


def insert_events(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """INSERT OR IGNORE emitted events; the UNIQUE(study, permaticker, event_ts)
    key makes emitter re-runs converge. Returns how many rows were NEW."""
    if not rows:
        return 0
    before = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    conn.executemany(
        """INSERT OR IGNORE INTO events
           (study, asset, permaticker, cik, ticker, event_ts, direction,
            strength, tier, sector, days_since_earnings, meta, ingested_at)
           VALUES (:study, :asset, :permaticker, :cik, :ticker, :event_ts,
                   :direction, :strength, :tier, :sector, :days_since_earnings,
                   :meta, :ingested_at)""",
        [{"cik": None, "permaticker": None, "ticker": None, "direction": None,
          "strength": None, "tier": None, "sector": None,
          "days_since_earnings": None, "ingested_at": None,
          **r, "meta": json.dumps(r.get("meta") or {})} for r in rows])
    conn.commit()
    return conn.execute("SELECT count(*) FROM events").fetchone()[0] - before


def events_for_study(conn: sqlite3.Connection, study: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM events WHERE study = ? ORDER BY event_ts ASC", (study,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["meta"] = {}
        out.append(d)
    return out


def register_study(conn: sqlite3.Connection, *, name: str, asset: str,
                   evaluator: str, tier: str, spec_path: str,
                   registered_at: int, primary_horizon: int) -> None:
    """Pre-register a study (§9.5 Class B: re-registration is a NEW name, e.g.
    '<study>-v2' — old rows freeze, so no UPSERT here; a duplicate name raises."""
    conn.execute(
        """INSERT INTO studies
           (name, asset, evaluator, tier, spec_path, registered_at, status,
            verdict_at, primary_horizon)
           VALUES (?, ?, ?, ?, ?, ?, 'REGISTERED', NULL, ?)""",
        (name, asset, evaluator, tier, spec_path, registered_at, primary_horizon))
    conn.commit()


def set_study_status(conn: sqlite3.Connection, name: str, status: str,
                     verdict_at: int | None = None) -> None:
    conn.execute("UPDATE studies SET status = ?, verdict_at = ? WHERE name = ?",
                 (status, verdict_at, name))
    conn.commit()


def get_study(conn: sqlite3.Connection, name: str) -> dict | None:
    row = conn.execute("SELECT * FROM studies WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def record_results(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """Append machine-written result rows (one per study/segment/horizon/tier).
    Prior rows for the same study+segment+horizon+tier are replaced — results
    are a pure function of (events, data, params), so recomputation supersedes."""
    for r in rows:
        conn.execute(
            "DELETE FROM study_results WHERE study=? AND segment=? AND horizon=? AND tier=?",
            (r["study"], r["segment"], r["horizon"], r.get("tier", "")))
    conn.executemany(
        """INSERT INTO study_results
           (study, segment, horizon, tier, n_events, n_months, mean_car,
            t_clustered, win_rate, exp_gross, exp_net, exp_after_tax,
            emitter_sha, params_hash, computed_at)
           VALUES (:study, :segment, :horizon, :tier, :n_events, :n_months,
                   :mean_car, :t_clustered, :win_rate, :exp_gross, :exp_net,
                   :exp_after_tax, :emitter_sha, :params_hash, :computed_at)""",
        [{"tier": "", "n_events": None, "n_months": None, "mean_car": None,
          "t_clustered": None, "win_rate": None, "exp_gross": None,
          "exp_net": None, "exp_after_tax": None, "emitter_sha": None,
          "params_hash": None, "computed_at": None, **r} for r in rows])
    conn.commit()


def results_for_study(conn: sqlite3.Connection, study: str,
                      segment: str | None = None) -> list[dict]:
    if segment:
        rows = conn.execute(
            "SELECT * FROM study_results WHERE study=? AND segment=? "
            "ORDER BY horizon ASC", (study, segment)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM study_results WHERE study=? ORDER BY segment, horizon",
            (study,)).fetchall()
    return [dict(r) for r in rows]

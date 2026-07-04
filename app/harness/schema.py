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
  -- BACKTEST = a continuous-overlay window spanning IS+OOS (portfolio policies:
  -- a DCA curve can't be split at a boundary — later equity depends on earlier
  -- accumulation), distinct from the event segments.
  segment TEXT CHECK(segment IN ('IS','OOS','LIVE','PLACEBO','BACKTEST')),
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
  computed_at INTEGER,
  extra_json TEXT                       -- evaluator-specific payload (policy legs,
                                        -- placebo suite details, regime splits)
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

-- Stage-0 paper book (§7 / meta-gate): one row per paper position opened from a
-- PROMOTED study's events; NAV series marks the book daily vs a benchmark.
CREATE TABLE IF NOT EXISTS paper_positions (
  id INTEGER PRIMARY KEY,
  study TEXT NOT NULL,
  ticker TEXT NOT NULL,
  event_ts INTEGER NOT NULL,
  qty REAL,                              -- NAV fraction sized at entry
  entry_ts INTEGER, entry_px REAL,
  exit_ts INTEGER, exit_px REAL,
  status TEXT CHECK(status IN ('PENDING','OPEN','CLOSED','SKIPPED')),
  skip_reason TEXT,                      -- limits violation etc. (honest record)
  horizon_sessions INTEGER,
  tier TEXT, sector TEXT,
  UNIQUE(study, ticker, event_ts)
);

CREATE TABLE IF NOT EXISTS paper_nav (
  study TEXT,
  date TEXT,                             -- ISO session date
  nav REAL,                              -- book NAV (starts 1.0), pre-tax
  nav_after_tax REAL,                    -- realized legs taxed (harness.tax st_rate)
  bench REAL,                            -- benchmark TR normalized to book start
  n_open INTEGER,
  PRIMARY KEY (study, date)
);

-- Small key/value meta (lab sync marker etc.).
CREATE TABLE IF NOT EXISTS lab_meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str,
                           decl: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_harness_db(conn: sqlite3.Connection) -> None:
    """Idempotent DDL — safe on every connect (additive-migration convention)."""
    conn.executescript(_DDL)
    # Additive migrations for pre-existing DBs (CREATE IF NOT EXISTS won't
    # alter an old table) — same convention as store.init_db.
    _add_column_if_missing(conn, "paper_nav", "nav_after_tax", "REAL")
    conn.commit()


def insert_events(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """INSERT OR IGNORE emitted events; the UNIQUE(study, permaticker, event_ts)
    key makes emitter re-runs converge. Returns how many rows were NEW.

    permaticker is COALESCED to ticker (then '') — SQLite treats NULLs as
    distinct in UNIQUE keys, so a NULL permaticker (BTC events) would silently
    re-insert on every emitter re-run (10-minute cron ⇒ unbounded duplicates;
    caught by the M2 adversarial verification)."""
    if not rows:
        return 0
    before = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    prepared = []
    for r in rows:
        d = {"cik": None, "ticker": None, "direction": None,
             "strength": None, "tier": None, "sector": None,
             "days_since_earnings": None, "ingested_at": None,
             **r, "meta": json.dumps(r.get("meta") or {})}
        d["permaticker"] = r.get("permaticker") or r.get("ticker") or ""
        prepared.append(d)
    conn.executemany(
        """INSERT OR IGNORE INTO events
           (study, asset, permaticker, cik, ticker, event_ts, direction,
            strength, tier, sector, days_since_earnings, meta, ingested_at)
           VALUES (:study, :asset, :permaticker, :cik, :ticker, :event_ts,
                   :direction, :strength, :tier, :sector, :days_since_earnings,
                   :meta, :ingested_at)""",
        prepared)
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
    if verdict_at is None:
        # A non-verdict update (e.g. RUNNING) must not erase an existing verdict
        # timestamp — recomputation is routine; verdicts are events.
        conn.execute("UPDATE studies SET status = ? WHERE name = ?", (status, name))
    else:
        conn.execute("UPDATE studies SET status = ?, verdict_at = ? WHERE name = ?",
                     (status, verdict_at, name))
    conn.commit()


# Verdict statuses that a routine re-run must never silently overwrite.
_VERDICT_STATUSES = frozenset({"PROMOTED", "KILLED", "EXTEND", "WATCHLIST"})


def mark_running(conn: sqlite3.Connection, name: str) -> None:
    """Set status=RUNNING only from REGISTERED/RUNNING — a study that already
    carries a verdict keeps it (re-running the numbers is routine; flipping a
    PROMOTED/KILLED study back to RUNNING would erase the lab record)."""
    cur = get_study(conn, name)
    if cur and cur.get("status") not in _VERDICT_STATUSES:
        set_study_status(conn, name, "RUNNING")


def get_study(conn: sqlite3.Connection, name: str) -> dict | None:
    row = conn.execute("SELECT * FROM studies WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def record_results(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """Append machine-written result rows (one per study/segment/horizon/tier).
    Prior rows for the same study+segment+horizon+tier are replaced — results
    are a pure function of (events, data, params), so recomputation supersedes."""
    for r in rows:
        # Coalesce tier: SQL NULL never matches NULL, so a None tier would
        # bypass the supersede DELETE and accumulate rows forever.
        r["tier"] = r.get("tier") or ""
        conn.execute(
            "DELETE FROM study_results WHERE study=? AND segment=? AND horizon=? AND tier=?",
            (r["study"], r["segment"], r["horizon"], r["tier"]))
    conn.executemany(
        """INSERT INTO study_results
           (study, segment, horizon, tier, n_events, n_months, mean_car,
            t_clustered, win_rate, exp_gross, exp_net, exp_after_tax,
            emitter_sha, params_hash, computed_at, extra_json)
           VALUES (:study, :segment, :horizon, :tier, :n_events, :n_months,
                   :mean_car, :t_clustered, :win_rate, :exp_gross, :exp_net,
                   :exp_after_tax, :emitter_sha, :params_hash, :computed_at,
                   :extra_json)""",
        [{"tier": "", "n_events": None, "n_months": None, "mean_car": None,
          "t_clustered": None, "win_rate": None, "exp_gross": None,
          "exp_net": None, "exp_after_tax": None, "emitter_sha": None,
          "params_hash": None, "computed_at": None,
          **r, "extra_json": json.dumps(r.get("extra") or {})} for r in rows])
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

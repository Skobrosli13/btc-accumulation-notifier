"""Nightly lab maintenance (box-resident: cron on the prod box, --no-sync).

    python -m scripts.nightly_lab [--no-sync]

1. Incremental Sharadar ingest (SEP/DAILY/SF1/TICKERS by lastupdated;
   SF2/ACTIONS by their append columns) + the BTC daily archive.
2. Re-emit events (INSERT OR IGNORE — new SF2 filings become new insider
   clusters; sue_pead re-emits from the existing crawl, refreshed monthly).
3. Freshness: with --no-sync (box-resident mode — the lake and lab tables live
   beside the services) a successful run stamps lab_meta.last_sync in place.
   WITHOUT the flag it scp-syncs the lab tables to the box instead — the
   legacy laptop-master mode, kept as a fallback if the box nightly is ever
   pulled back onto the dev machine.

Verdicts are NOT touched here — that is the monthly review's job (§9.5).
Fail-soft per step: a dead source skips with a log line; the sync/stamp only
runs when everything before it succeeded, so the dashboard never reports a
half-ingested state as fresh. Exit code 1 on any failure (visible in cron
mail / Task Scheduler history).
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config          # noqa: E402

log = logging.getLogger("nightly-lab")

REPO = Path(__file__).resolve().parents[1]
SSH_KEY = str(Path.home() / ".ssh" / "lightsail.pem")
BOX = "ubuntu@44.212.248.190"
BOX_REPO = "~/btc-accumulation-notifier"
INCREMENTAL_TABLES = ("TICKERS", "SEP", "DAILY", "SF1", "SF2", "ACTIONS")


def _step(name: str, fn) -> bool:
    try:
        fn()
        log.info("%s: ok", name)
        return True
    except Exception as exc:  # noqa: BLE001 - a nightly must report, not die mid-list
        log.error("%s: FAILED (%s)", name, exc)
        return False


def sync_lab_to_box() -> None:
    """Dump the harness tables and apply them on the box (WAL tolerates the
    brief write alongside the running services). Also stamps lab_meta.last_sync
    on BOTH sides — the dashboard's freshness/staleness source of truth."""
    import sqlite3
    from datetime import datetime, timezone
    cfg = load_config()
    now_iso = datetime.now(timezone.utc).isoformat()
    src = sqlite3.connect(cfg.db_path)
    tables = ("studies", "study_results", "events", "fills", "decisions",
              "paper_positions", "paper_nav")
    from app.harness import schema as _schema
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False,
                                     encoding="utf-8") as f:
        dump_path = f.name
        # Carry the harness DDL so the box can never lag the laptop's schema
        # (CREATE TABLE IF NOT EXISTS — idempotent, additive).
        f.write(_schema._DDL + "\n")
        f.write("BEGIN;\n")
        for t in tables:
            f.write(f"DELETE FROM {t};\n")
        for line in src.iterdump():
            if any(line.startswith(f'INSERT INTO "{t}"') for t in tables):
                f.write(line + "\n")
        f.write("INSERT OR REPLACE INTO lab_meta (key, value) VALUES "
                f"('last_sync', '{now_iso}');\n")
        f.write("COMMIT;\n")
    src.execute("INSERT OR REPLACE INTO lab_meta (key, value) VALUES (?, ?)",
                ("last_sync", now_iso))
    src.commit()
    src.close()
    subprocess.run(["scp", "-i", SSH_KEY, dump_path, f"{BOX}:/tmp/lab_sync.sql"],
                   check=True, timeout=300)
    subprocess.run(
        ["ssh", "-i", SSH_KEY, BOX,
         f"cd {BOX_REPO} && .venv/bin/python -c \""
         "import sqlite3; c=sqlite3.connect('btc.db'); "
         "c.executescript(open('/tmp/lab_sync.sql',encoding='utf-8').read()); "
         "c.commit(); c.close()\" && rm /tmp/lab_sync.sql"],
        check=True, timeout=300)
    Path(dump_path).unlink(missing_ok=True)


def stamp_lab_fresh() -> None:
    """Box-resident mode: the lab tables were updated in place, so freshness =
    this run's completion. Stamps lab_meta.last_sync — the dashboard's
    staleness source of truth — with no scp hop."""
    import sqlite3
    from datetime import datetime, timezone
    from app.harness import schema as _schema
    cfg = load_config()
    conn = sqlite3.connect(cfg.db_path)
    try:
        _schema.init_harness_db(conn)
        conn.execute("INSERT OR REPLACE INTO lab_meta (key, value) VALUES (?, ?)",
                     ("last_sync", datetime.now(timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()


def _sector_map(lake, tickers: list[str]) -> dict[str, str]:
    """{TICKER: sector} from the lake's TICKERS table — best-effort.

    Without it the book's per-sector limit silently never binds for stock picks
    (a NULL sector skips the check), which would be a quiet failure of a
    pre-registered risk control rather than a visible one."""
    if not tickers or not lake.exists("tickers"):
        return {}
    try:
        uniq = sorted({t.upper() for t in tickers})
        ph = ",".join("?" for _ in uniq)
        df = lake.query(
            f"SELECT ticker, sector FROM {lake.sql_table('tickers')} "
            f"WHERE ticker IN ({ph})", uniq)
        return {str(r["ticker"]).upper(): r["sector"]
                for _, r in df.iterrows() if r.get("sector")}
    except Exception as exc:                      # noqa: BLE001 - best-effort
        log.warning("sector map unavailable (%s) — sector limits will not bind", exc)
        return {}


def paper_book_step() -> None:
    """Stage-0 paper book (§7/meta-gate) — ONE book, three sources.

    'lab'      every PROMOTED car-study's new events, sized from its OOS stats
               under the original §7 limits, evaluated against lab positions
               ONLY so the meta-gate curve stays reproducible.
    'swing'    surfaced stock_collect picks (stop/target exits).
    'longterm' surfaced stock_lt_collect long-buys (quarterly horizon).

    Each namespace gets its own NAV series, plus two roll-ups: '@lab' (the
    meta-gate evidence) and '@combined' (the portfolio view). Only PROMOTED
    studies trade; a demoted study's book freezes (positions close out, nothing
    new opens)."""
    import json as _json
    import sqlite3
    from datetime import datetime, timezone

    from app.data.equities import prices as eq_prices
    from app.data_lake import Lake
    from app.harness import schema
    from app.portfolio import book, bridge

    cfg = load_config()
    lake = Lake(cfg.data_lake_path)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    schema.init_harness_db(conn)
    try:
        spy = eq_prices.sep_bars_bulk(lake, ["SPY"], limit=5000,
                                      table="sfp").get("SPY", [])

        # --- file the stock picks (idempotent; new rows only) -----------------
        # Bounded by the go-live floor: the collectors have months of history,
        # and filing it would backfill a record the book never lived through.
        # The FIRST run therefore files nothing and starts the clock.
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        since = bridge.go_live_ts(conn, now_ms)
        pick_tickers = [r[0] for r in conn.execute(
            "SELECT DISTINCT ticker FROM stock_positions "
            "WHERE ticker IS NOT NULL AND opened_ts >= ? "
            "UNION SELECT DISTINCT ticker FROM stock_lt_holdings "
            "WHERE ticker IS NOT NULL AND opened_ts >= ?", (since, since))]
        filed = bridge.file_picks(conn, since_ts=since,
                                  sectors=_sector_map(lake, pick_tickers))
        if any(filed.values()):
            log.info("bridge filed: %s",
                     {k: v for k, v in filed.items() if v})

        # --- advance every namespace -----------------------------------------
        # (study name, source, expectancy, car_std) — lab studies carry OOS
        # stats; bridged picks pass None, which drops the Kelly leg rather than
        # zeroing it (see sizing: None != 0.0).
        jobs: list[tuple[str, str, float | None, float | None, int | None]] = []
        for s in conn.execute(
                "SELECT * FROM studies WHERE tier='alpha' AND evaluator='car'"):
            s = dict(s)
            if s["status"] == "PROMOTED":
                events = [e for e in schema.events_for_study(conn, s["name"])
                          if e["event_ts"] >= s["registered_at"]]
                book.record_pending(conn, s["name"], events,
                                    horizon=s["primary_horizon"] or 21,
                                    source="lab")
            oos = conn.execute(
                "SELECT exp_after_tax, extra_json FROM study_results WHERE "
                "study=? AND segment='OOS' AND horizon=?",
                (s["name"], s["primary_horizon"])).fetchone()
            exp = oos["exp_after_tax"] if oos else None
            car_std = (_json.loads(oos["extra_json"] or "{}").get("car_std")
                       if oos else None)
            jobs.append((s["name"], "lab", exp, car_std, None))
        for ns, src in conn.execute(
                "SELECT DISTINCT study, source FROM paper_positions "
                "WHERE source != 'lab'"):
            jobs.append((ns, src, None, None, None))

        all_bars: dict[str, list[dict]] = {}
        for name, src, exp, car_std, _ in jobs:
            tickers = [r[0] for r in conn.execute(
                "SELECT DISTINCT ticker FROM paper_positions WHERE study=? "
                "AND status IN ('PENDING','OPEN','CLOSED')", (name,))]
            if not tickers:
                continue
            bars = eq_prices.sep_bars_bulk(lake, tickers, limit=400)
            all_bars.update(bars)
            st = book.process(conn, name, bars, expectancy=exp,
                              car_std=car_std, source=src)
            n = book.mark_nav(conn, name, bars, spy)
            log.info("paper book %s [%s]: %s, nav rows %d", name, src, st, n)

        rolled = book.mark_rollups(conn, all_bars, spy)
        log.info("paper book roll-ups: %s", rolled)
    finally:
        conn.close()


def backup_verdict_registry() -> None:
    """Commit a tiny JSON snapshot of the studies table — verdict timestamps are
    the one non-re-derivable lab fact; git is the tamper-evident backup until
    Litestream lands."""
    import json as _json
    import sqlite3

    cfg = load_config()
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM studies ORDER BY registered_at")]
    conn.close()
    out = REPO / "studies" / "registry_snapshot.json"
    new = _json.dumps(rows, indent=1, sort_keys=True, default=str)
    if out.exists() and out.read_text(encoding="utf-8") == new:
        return                                   # unchanged — no commit churn
    out.write_text(new, encoding="utf-8")
    subprocess.run(["git", "add", str(out)], cwd=REPO, check=True, timeout=60)
    subprocess.run(["git", "commit", "-q", "-m",
                    "nightly: verdict registry snapshot"],
                   cwd=REPO, check=True, timeout=60)
    # GAP E is only closed once the snapshot leaves this disk — a commit that
    # stays on the laptop is not a backup. Push failure is non-fatal (offline
    # laptop); the next nightly retries.
    try:
        subprocess.run(["git", "push", "-q", "origin", "main"],
                       cwd=REPO, check=True, timeout=120)
        log.info("verdict registry snapshot pushed to origin")
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("registry snapshot committed but push failed (%s) — "
                    "will retry next nightly", exc)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--no-sync", action="store_true", help="skip the box sync")
    args = p.parse_args(argv)

    from scripts import emit_events, ingest, ingest_btc

    ok = True
    for table in INCREMENTAL_TABLES:
        ok &= _step(f"ingest {table}",
                    lambda t=table: ingest.ingest(t, incremental=True))
    ok &= _step("ingest btc_daily", ingest_btc.ingest_btc_daily)
    ok &= _step("emit insider_cluster",
                lambda: emit_events.main(["insider_cluster"]))
    ok &= _step("emit sue_pead", lambda: emit_events.main(["sue_pead"]))
    ok &= _step("emit clone13f", lambda: emit_events.main(["clone13f"]))
    ok &= _step("paper book", paper_book_step)
    ok &= _step("verdict registry backup", backup_verdict_registry)
    if ok and not args.no_sync:
        ok &= _step("sync lab tables to box", sync_lab_to_box)
    elif ok:
        ok &= _step("stamp lab freshness", stamp_lab_fresh)
    else:
        log.error("skipping box sync/freshness stamp — an earlier step failed")
    log.info("nightly lab %s", "complete" if ok else "FINISHED WITH FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

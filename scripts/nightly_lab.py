"""Nightly lab maintenance (runs LOCALLY — the lake lives on the dev machine).

    python -m scripts.nightly_lab [--no-sync]

1. Incremental Sharadar ingest (SEP/DAILY/SF1/TICKERS by lastupdated;
   SF2/ACTIONS by their append columns) + the BTC daily archive.
2. Re-emit events (INSERT OR IGNORE — new SF2 filings become new insider
   clusters; sue_pead re-emits from the existing crawl, refreshed monthly).
3. Sync the lab tables (studies/study_results/events/fills/decisions) to the
   prod box so the /lab page tracks local research.

Verdicts are NOT touched here — that is the monthly review's job (§9.5).
Fail-soft per step: a dead source skips with a log line; the sync only runs
when everything before it succeeded, so prod never receives a half-ingested
state. Exit code 1 on any failure (visible in Task Scheduler history).
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
    brief write alongside the running services)."""
    import sqlite3
    cfg = load_config()
    src = sqlite3.connect(cfg.db_path)
    tables = ("studies", "study_results", "events", "fills", "decisions")
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False,
                                     encoding="utf-8") as f:
        dump_path = f.name
        f.write("BEGIN;\n")
        for t in tables:
            f.write(f"DELETE FROM {t};\n")
        for line in src.iterdump():
            if any(line.startswith(f'INSERT INTO "{t}"') for t in tables):
                f.write(line + "\n")
        f.write("COMMIT;\n")
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
    if ok and not args.no_sync:
        ok &= _step("sync lab tables to box", sync_lab_to_box)
    elif not ok:
        log.error("skipping box sync — an earlier step failed")
    log.info("nightly lab %s", "complete" if ok else "FINISHED WITH FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

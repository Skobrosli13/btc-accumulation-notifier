"""Monthly research review (§9.5) — runs LOCALLY on the 1st.

    python -m scripts.monthly_review

1. Refresh the SUE crawl (re-crawl names whose newest event is >75 days old —
   a new fiscal quarter has landed for them).
2. Re-emit events, re-RUN every registered study (recompute supersedes; LIVE
   segments have accrued a month of data), and re-apply verdicts:
   * portfolio policies verdict inside their runner;
   * ALPHA studies get gates.alpha_verdict via `study verdict` — a study
     already EXTENDed that misses again is KILLED by the gate.
3. Sync the lab tables to the box.
4. Print the report — paste the interesting lines into studies/DECISIONS.md
   with rationale (the notebook entry is deliberately human; §9.5).

Placebo suites are NOT re-run monthly — the machinery only changes when code
changes, and any Class-B event-definition change re-registers as <study>-v2
(which starts fresh anyway).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store                       # noqa: E402
from app.config import load_config          # noqa: E402
from app.harness import schema              # noqa: E402

log = logging.getLogger("monthly-review")


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from scripts import crawl_sue, emit_events, nightly_lab
    from scripts import study as study_cli

    log.info("=== SUE crawl refresh (stale > 75 days) ===")
    crawl_sue.crawl(stale_days=75)
    emit_events.main(["insider_cluster"])
    emit_events.main(["sue_pead"])

    cfg = load_config()
    conn = store.connect(cfg.db_path)
    schema.init_harness_db(conn)
    studies = [dict(r) for r in conn.execute("SELECT * FROM studies").fetchall()]
    conn.close()

    for s in studies:
        log.info("=== re-run %s ===", s["name"])
        try:
            study_cli.main(["run", "--name", s["name"]])
            if s["tier"] == "alpha":
                floor = 60 if s["name"].startswith("sue_") else 100
                study_cli.main(["verdict", "--name", s["name"],
                                "--min-events", str(floor)])
        except SystemExit as exc:      # a BLOCKED/no-events study is a report line
            log.warning("%s: %s", s["name"], exc)

    nightly_lab.sync_lab_to_box()
    log.info("=== report ===")
    study_cli.main(["report"])
    print("\nAppend the month's decisions + rationale to studies/DECISIONS.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

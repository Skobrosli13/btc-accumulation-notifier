"""Standalone SEC Form-4 insider scan (cron: weekly, keyless).

The daily ``stock_collect`` cron runs ``--skip-insider`` to stay fast — it already
fetches 500+ price series — which meant open-market insider transactions were never
collected in prod at all. This entrypoint owns insider ingestion on its own slow
cadence (Form 4s are slow-moving; a 90-day lookback tolerates a weekly sweep): it
walks the universe's CIKs and upserts open-market transactions into ``stock_insider``.
The daily collector then reads the cached clusters (``stock_store.insider_cluster``
via ``_load_insider_clusters``) for PEAD context — no live SEC fetch on the hot path.

    python -m app.stock_insider_scan                 # live weekly scan
    python -m app.stock_insider_scan --dry-run       # fetch + print, no DB write
    python -m app.stock_insider_scan --limit 25      # cap universe (testing)
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import stock_store, store
from .config import Config, load_config
from .stock_collect import _fetch_insider, _sync_universe

log = logging.getLogger("stock-insider-scan")


def scan(cfg: Config, *, dry_run: bool = False, limit: int | None = None) -> dict:
    """Fetch + upsert the full universe's open-market insider transactions.

    Reuses the collector's ``_fetch_insider`` (fetch -> upsert -> cluster) and
    ``_sync_universe`` (resolve CIKs) so there is exactly one ingestion path."""
    if not cfg.stock_insider_active:
        log.warning("insider layer disabled (STOCK_INSIDER=false) — nothing to scan")
        return {"active": False, "attempted": 0, "ok": 0, "clusters": 0}
    conn = store.connect(cfg.db_path)
    store.init_db(conn)
    stock_store.init_stock_db(conn)
    try:
        universe_rows = _sync_universe(conn, cfg, dry_run, limit)
        clusters, attempted, ok = _fetch_insider(conn, cfg, universe_rows, dry_run)
    finally:
        conn.close()
    log.info("insider scan: %d/%d CIKs returned rows, %d buy-clusters%s",
             ok, attempted, len(clusters), " (dry-run, no write)" if dry_run else "")
    return {"active": True, "attempted": attempted, "ok": ok,
            "clusters": len(clusters), "dry_run": dry_run}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Standalone SEC Form-4 insider scan")
    p.add_argument("--dry-run", action="store_true", help="fetch + print; no DB write")
    p.add_argument("--limit", type=int, default=None, help="cap universe size (testing)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    res = scan(load_config(), dry_run=args.dry_run, limit=args.limit)
    print(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())

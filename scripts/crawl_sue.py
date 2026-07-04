"""Crawl SRW-SUE PEAD events for the current universe into the lake (sue_pead prep).

    python -m scripts.crawl_sue [--limit N]

For every included name in today's PIT universe, pulls the two EDGAR XBRL EPS
concepts + the 8-K announcement timeline and stores the joined SUE events in the
lake table ``sue_events`` (ticker, cik, report_ts, hour, sue, fy, quarter,
period_end). Polite (~6 req/s built into the fetchers), resumable (names already
in the lake are skipped), multi-tens-of-minutes for the full universe.

HONEST SCOPE CAVEAT (carried into the study spec): the SEC ticker→CIK map
covers CURRENT registrants, so since-delisted names are largely absent — the
EVENT population skews to survivors even though the CAR control pools are PIT.
The sue_pead spec documents this; the harness's controls/segments remain PIT.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config                 # noqa: E402
from app.data.equities import pead, universe       # noqa: E402
from app.data_lake import Lake                      # noqa: E402
from app.sources.stocks import universe as sec     # noqa: E402

log = logging.getLogger("crawl-sue")


def crawl(limit: int | None = None, stale_days: int | None = None) -> int:
    """``stale_days=None`` = initial-crawl resume (skip any ticker already in
    the lake). With ``stale_days=N`` (the monthly-refresh mode) only tickers
    whose NEWEST crawled event is younger than N days are skipped — otherwise
    the per-ticker resume would freeze every name at its first crawl and new
    quarters would never arrive."""
    cfg = load_config()
    lake = Lake(cfg.data_lake_path)
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    uni = [r["ticker"] for r in universe.build_from_lake(lake, today) if r["included"]]
    done = set()
    if lake.exists("sue_events"):
        df = lake.read("sue_events")
        if stale_days is None:
            done = set(df["ticker"].unique())
        else:
            cutoff = (datetime.now(timezone.utc).timestamp() - stale_days * 86400) * 1000
            newest = df.groupby("ticker")["report_ts"].max()
            done = set(newest[newest >= cutoff].index)
    todo = [t for t in uni if t not in done]
    if limit:
        todo = todo[:limit]
    log.info("universe %d names; %d already crawled; %d to go",
             len(uni), len(done), len(todo))
    cikmap = sec.sec_ticker_map(cfg.sec_user_agent)
    batch: list[dict] = []
    n_events = 0
    for k, tk in enumerate(todo, 1):
        cik = (cikmap.get(tk.upper()) or {}).get("cik")
        evs = pead.sue_events(cik, cfg.sec_user_agent) if cik else []
        for e in evs:
            batch.append({"ticker": tk, "cik": cik, **e})
        n_events += len(evs)
        if k % 50 == 0 or k == len(todo):
            if batch:
                lake.upsert("sue_events", pd.DataFrame(batch),
                            ["ticker", "fy", "quarter"], sort_col="report_ts")
                batch = []
            log.info("%d/%d names crawled (%d events so far)", k, len(todo), n_events)
    total = lake.read("sue_events").shape[0] if lake.exists("sue_events") else 0
    log.info("done: lake sue_events now %d rows", total)
    return total


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--stale-days", type=int, default=None,
                   help="refresh mode: re-crawl tickers whose newest event is older than N days")
    args = p.parse_args()
    crawl(limit=args.limit, stale_days=args.stale_days)

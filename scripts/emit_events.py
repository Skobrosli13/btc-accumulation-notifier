"""Run event emitters against the lake and write `events` rows.

    python -m scripts.emit_events insider_cluster
    python -m scripts.emit_events sue_pead

Thin orchestration: pure emitters (app/events/*) produce {ticker, event_ts,
direction, strength, meta}; this script enriches each event with the CAR
matcher's covariates — permaticker (security master), tier (latest DAILY mcap
within 30d), sector (TICKERS), days_since_earnings (latest SF1 ARQ datekey) —
and INSERT OR IGNOREs into the app DB (idempotent re-runs).
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store                                   # noqa: E402
from app.config import load_config                      # noqa: E402
from app.data.equities import security_master, universe as eq_universe  # noqa: E402
from app.data_lake import Lake                           # noqa: E402
from app.events import insider_cluster                   # noqa: E402
from app.harness import schema                           # noqa: E402

log = logging.getLogger("emit-events")


def _date_iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()


def enrich(lake: Lake, events: list[dict]) -> list[dict]:
    """Attach permaticker / tier / sector / days_since_earnings to raw events
    via batch DuckDB asof-joins (a temp parquet carries the event keys)."""
    if not events:
        return []
    tmp = pd.DataFrame([{"idx": i, "ticker": e["ticker"],
                         "date": _date_iso(e["event_ts"])}
                        for i, e in enumerate(events)])
    lake.write("_tmp_events", tmp)
    try:
        mcap = lake.query(f"""
            SELECT e.idx, max_by(d.marketcap, d.date) AS marketcap
            FROM {lake.sql_table('_tmp_events')} e
            JOIN {lake.sql_table('daily')} d ON e.ticker = d.ticker
             AND CAST(d.date AS DATE) <= CAST(e.date AS DATE)
             AND CAST(d.date AS DATE) >= CAST(e.date AS DATE) - INTERVAL 30 DAY
            GROUP BY e.idx""")
        earn = lake.query(f"""
            SELECT e.idx, max(f.datekey) AS datekey
            FROM {lake.sql_table('_tmp_events')} e
            JOIN {lake.sql_table('sf1')} f ON e.ticker = f.ticker
             AND f.dimension = 'ARQ'
             AND CAST(f.datekey AS DATE) <= CAST(e.date AS DATE)
            GROUP BY e.idx""")
    finally:
        lake.path("_tmp_events").unlink(missing_ok=True)
    mcap_by = dict(zip(mcap["idx"], mcap["marketcap"])) if not mcap.empty else {}
    dk_by = dict(zip(earn["idx"], earn["datekey"])) if not earn.empty else {}

    trows = lake.read("tickers").to_dict("records")
    pt_map = security_master.ticker_permaticker_map(trows)
    sector_by = {r["ticker"]: r.get("sector") for r in trows if r.get("ticker")}

    out = []
    for i, e in enumerate(events):
        mc = mcap_by.get(i)
        tier = eq_universe.classify_tier(mc * 1e6) if mc is not None else None
        dk = dk_by.get(i)
        dse = None
        if dk is not None:
            d_ev = datetime.strptime(_date_iso(e["event_ts"]), "%Y-%m-%d")
            d_dk = datetime.strptime(str(dk)[:10], "%Y-%m-%d")
            dse = (d_ev - d_dk).days
        out.append({**e, "asset": "EQ",
                    "permaticker": str(pt_map.get(e["ticker"], "")) or e["ticker"],
                    "tier": tier, "sector": sector_by.get(e["ticker"]),
                    "days_since_earnings": dse,
                    "ingested_at": int(time.time() * 1000)})
    return out


def emit_insider_cluster(lake: Lake) -> list[dict]:
    fills = lake.query(f"""
        SELECT ticker, ownername, officertitle, isofficer, isdirector,
               transactiondate, filingdate, transactionvalue,
               transactionpricepershare, transactionshares
        FROM {lake.sql_table('sf2')}
        WHERE transactioncode = 'P'
          AND (isofficer = 'Y' OR isdirector = 'Y')
          AND transactiondate IS NOT NULL
    """).to_dict("records")
    log.info("SF2 code-P officer/director buys: %d rows", len(fills))
    events = insider_cluster.cluster_events(fills)
    log.info("clusters emitted: %d", len(events))
    return [{"study": "insider_cluster", "direction": "LONG", **e} for e in events]


def emit_sue_pead(lake: Lake) -> list[dict]:
    """Decile-rank the crawled SUE events per calendar quarter; top decile = LONG,
    bottom decile = SHORT (the analytic short leg, §5.7). Reaction-sign agreement
    is enforced by the pead emitter's join; deciles are cross-sectional within
    each quarter's crawled population."""
    if not lake.exists("sue_events"):
        raise SystemExit("lake sue_events missing — run scripts.crawl_sue first")
    df = lake.read("sue_events")
    log.info("crawled SUE events: %d rows / %d names", len(df), df["ticker"].nunique())
    out: list[dict] = []
    for (_fy, _q), grp in df.groupby(["fy", "quarter"]):
        if len(grp) < 20:              # a decile needs a population
            continue
        lo = grp["sue"].quantile(0.10)
        hi = grp["sue"].quantile(0.90)
        for _, r in grp.iterrows():
            if r["sue"] >= hi:
                d = "LONG"
            elif r["sue"] <= lo:
                d = "SHORT"
            else:
                continue
            out.append({"study": "sue_pead", "ticker": r["ticker"],
                        "event_ts": int(r["report_ts"]), "direction": d,
                        "strength": float(abs(r["sue"])),
                        "meta": {"sue": float(r["sue"]), "fy": int(r["fy"]),
                                 "quarter": int(r["quarter"]),
                                 "hour": r.get("hour") or ""}})
    log.info("sue_pead decile events: %d", len(out))
    return out


EMITTERS = {"insider_cluster": emit_insider_cluster, "sue_pead": emit_sue_pead}


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("study", choices=sorted(EMITTERS))
    args = p.parse_args(argv)
    cfg = load_config()
    lake = Lake(cfg.data_lake_path)
    raw = EMITTERS[args.study](lake)
    enriched = enrich(lake, raw)
    conn = store.connect(cfg.db_path)
    schema.init_harness_db(conn)
    n_new = schema.insert_events(conn, enriched)
    conn.close()
    log.info("%s: %d events (%d new) written to events", args.study, len(enriched), n_new)


if __name__ == "__main__":
    main()

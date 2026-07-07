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


def _insider_fills(lake: Lake) -> list[dict]:
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
    return fills


def emit_insider_cluster(lake: Lake) -> list[dict]:
    events = insider_cluster.cluster_events(_insider_fills(lake))
    log.info("clusters emitted: %d", len(events))
    return [{"study": "insider_cluster", "direction": "LONG", **e} for e in events]


def emit_insider_cluster_q(lake: Lake) -> list[dict]:
    """Quarter-hold sibling of insider_cluster: byte-identical events, tagged
    for the h=63 study so the two can never drift apart (the only difference is
    the verdict horizon, set at registration). See studies/insider_cluster_q.md."""
    events = insider_cluster.cluster_events(_insider_fills(lake))
    log.info("clusters emitted (quarter-hold sibling): %d", len(events))
    return [{"study": "insider_cluster_q", "direction": "LONG", **e} for e in events]


def emit_insider_cluster_hi(lake: Lake) -> list[dict]:
    """High-conviction subset of insider_cluster: a meaningful-sized cluster
    (>= $250k aggregate) in which a CEO/CFO participated. Tests whether the edge
    concentrates in the highest-conviction clusters (Cohen-Malloy-Pomorski:
    opportunistic, executive, large). Same clustering machinery, stricter filter.
    See studies/insider_cluster_hi.md."""
    events = insider_cluster.cluster_events(_insider_fills(lake), min_agg_usd=250_000.0)
    hi = [e for e in events if e.get("strength", 1.0) >= insider_cluster.EXEC_STRENGTH]
    log.info("high-conviction clusters: %d (of %d >=$250k)", len(hi), len(events))
    return [{"study": "insider_cluster_hi", "direction": "LONG", **e} for e in hi]


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


_PEAD_REACTION_MIN = 0.0     # the announcement-day move must CONFIRM the positive surprise


def emit_sue_pead_confirmed(lake: Lake) -> list[dict]:
    """Top-decile SUE (LONG only) CONFIRMED by a positive announcement-day move.

    The refinement for sue_pead's OOS decay (t=0.87@h21): post-earnings drift
    concentrates in surprises the market moved WITH — a credible/under-reacted
    beat — not ones it shrugged off. Reaction = the event-DATE total return
    (closeadj[report_date]/closeadj[prior bar] − 1); keep LONG events with
    reaction > 0. LOOK-AHEAD-SAFE: the reaction ends at report_date's close, and
    the car evaluator enters at the NEXT session's open, so the filter uses only
    information available before entry. (Limitation: for after-market 8-Ks the
    event-date move can precede the true reaction — a noise source, documented, not
    look-ahead.) The bottom-decile SHORT leg is dropped: the SEC CIK crawl is
    survivorship-biased against delisted names. See studies/sue_pead_confirmed.md."""
    if not lake.exists("sue_events"):
        raise SystemExit("lake sue_events missing — run scripts.crawl_sue first")
    df = lake.read("sue_events")
    cand: list[dict] = []
    for (_fy, _q), grp in df.groupby(["fy", "quarter"]):
        if len(grp) < 20:
            continue
        hi = grp["sue"].quantile(0.90)
        for _, r in grp.iterrows():
            if r["sue"] >= hi:
                cand.append({"ticker": r["ticker"], "event_ts": int(r["report_ts"]),
                             "sue": float(r["sue"]), "fy": int(r["fy"]),
                             "quarter": int(r["quarter"]), "hour": r.get("hour") or ""})
    log.info("sue_pead top-decile LONG candidates: %d", len(cand))
    if not cand:
        return []
    tmp = pd.DataFrame([{"idx": i, "ticker": c["ticker"], "rd": _date_iso(c["event_ts"])}
                        for i, c in enumerate(cand)])
    lake.write("_tmp_pead", tmp)
    try:
        react = lake.query(f"""
            WITH d AS (
              SELECT e.idx, s.date AS dt, s.closeadj AS c
              FROM {lake.sql_table('_tmp_pead')} e
              JOIN {lake.sql_table('sep')} s ON e.ticker = s.ticker
               AND CAST(s.date AS DATE) <= CAST(e.rd AS DATE)
               AND CAST(s.date AS DATE) >= CAST(e.rd AS DATE) - INTERVAL 12 DAY),
            r AS (
              SELECT idx, c, row_number() OVER (PARTITION BY idx ORDER BY dt DESC) AS rn
              FROM d)
            SELECT a.idx AS idx, a.c AS evt, b.c AS pre
            FROM r a JOIN r b ON a.idx = b.idx AND a.rn = 1 AND b.rn = 2""")
    finally:
        lake.path("_tmp_pead").unlink(missing_ok=True)
    reaction_by: dict[int, float] = {}
    for _, rr in react.iterrows():
        if rr["pre"] and rr["evt"] and float(rr["pre"]) > 0:
            reaction_by[int(rr["idx"])] = float(rr["evt"]) / float(rr["pre"]) - 1.0
    out: list[dict] = []
    for i, c in enumerate(cand):
        rx = reaction_by.get(i)
        if rx is None or rx <= _PEAD_REACTION_MIN:
            continue
        out.append({"study": "sue_pead_confirmed", "ticker": c["ticker"],
                    "event_ts": c["event_ts"], "direction": "LONG",
                    "strength": float(abs(c["sue"])),
                    "meta": {"sue": c["sue"], "fy": c["fy"], "quarter": c["quarter"],
                             "hour": c["hour"], "reaction": round(rx, 4)}})
    log.info("sue_pead_confirmed events (positive reaction): %d of %d", len(out), len(cand))
    return out


def emit_clone13f(lake: Lake) -> list[dict]:
    """New positions by concentrated low-turnover managers (SF3, SHR rows).

    Candidate managers = anyone who EVER passes the holdings/AUM screen (their
    FULL history is fetched so turnover + adds compute against real priors);
    the pure emitter applies the per-quarter qualification."""
    from app.events import clone13f as c13

    cand = lake.query(f"""
        WITH q AS (
            SELECT investorname, calendardate, count(*) AS n, sum(value) AS aum
            FROM {lake.sql_table('sf3')} WHERE securitytype = 'SHR'
            GROUP BY 1, 2)
        SELECT DISTINCT investorname FROM q
        WHERE n <= {c13.MAX_HOLDINGS} AND aum BETWEEN {c13.AUM_MIN} AND {c13.AUM_MAX}
    """)["investorname"].tolist()
    log.info("clone13f: %d candidate managers (ever concentrated + mid-AUM)", len(cand))
    if not cand:
        return []
    ph = ",".join("?" for _ in cand)
    hold = lake.query(f"""
        SELECT investorname, calendardate, ticker, sum(value) AS value
        FROM {lake.sql_table('sf3')}
        WHERE securitytype = 'SHR' AND investorname IN ({ph})
        GROUP BY 1, 2, 3""", cand)
    log.info("clone13f: %d holding rows for candidates", len(hold))
    snapshots = []
    for (inv, q), grp in hold.groupby(["investorname", "calendardate"]):
        snapshots.append({"investor": inv, "quarter": str(q)[:10],
                          "aum": float(grp["value"].sum()),
                          "tickers": set(grp["ticker"])})
    events = c13.cluster_events(snapshots)
    log.info("clone13f: %d aggregated add-events", len(events))
    return [{"study": "clone13f", **e} for e in events]


EMITTERS = {"insider_cluster": emit_insider_cluster,
            "insider_cluster_q": emit_insider_cluster_q,
            "insider_cluster_hi": emit_insider_cluster_hi,
            "sue_pead": emit_sue_pead,
            "sue_pead_confirmed": emit_sue_pead_confirmed,
            "clone13f": emit_clone13f}


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

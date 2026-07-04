"""SRW-SUE coverage report (§4.5 acceptance: >=75% of universe-quarters over 10y).

For a sample of universe names, crawls free EDGAR XBRL, computes the SUE series,
and reports what fraction of the ~40 quarters/name (10y) have a defined SUE. The
full-universe run is a slow polite EDGAR crawl (<=~6 req/s) — this samples so the
coverage can be sanity-checked without the multi-hour sweep.

    python -m scripts.sue_coverage [--sample 25] [--tickers AAPL,MSFT,...] [--years 10]
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config                     # noqa: E402
from app.data.equities.edgar import xbrl_eps           # noqa: E402
from app.data_lake import Lake                          # noqa: E402
from app.sources.stocks import universe as sec         # noqa: E402


def summarize(quarters_by_name: dict[str, int], expected_per_name: int) -> dict:
    """Coverage stats (pure): fraction of the expected universe-quarters that have
    a SUE, plus per-name distribution. ``quarters_by_name`` maps ticker -> count
    of quarters with a defined SUE."""
    names = len(quarters_by_name)
    got = sum(quarters_by_name.values())
    expected = names * expected_per_name
    counts = sorted(quarters_by_name.values())
    return {
        "names": names,
        "quarters_with_sue": got,
        "expected_quarters": expected,
        "coverage": (got / expected) if expected else 0.0,
        "median_quarters_per_name": (statistics.median(counts) if counts else 0),
        "names_with_zero": sum(1 for c in counts if c == 0),
    }


def _sample_tickers(lake: Lake, n: int) -> list[str]:
    if not lake.exists("tickers"):
        return []
    df = lake.query(
        f"SELECT DISTINCT ticker FROM {lake.sql_table('tickers')} "
        f"WHERE \"table\"='SF1' AND category LIKE '%Common Stock%' "
        f"AND isdelisted='N' ORDER BY ticker LIMIT ?", [int(n)])
    return list(df["ticker"])


def run(sample: int = 25, tickers: list[str] | None = None, years: int = 10) -> dict:
    cfg = load_config()
    lake = Lake(cfg.data_lake_path)
    names = tickers or _sample_tickers(lake, sample)
    if not names:
        print("no tickers — ingest TICKERS first, or pass --tickers")
        return {}
    cikmap = sec.sec_ticker_map(cfg.sec_user_agent)
    quarters: dict[str, int] = {}
    for tk in names:
        cik = (cikmap.get(tk.upper()) or {}).get("cik")
        if not cik:
            quarters[tk] = 0
            continue
        # sue_for_cik merges the diluted + basic-and-diluted concepts (loss-year
        # filers switch tags), which is what lifts mid/small coverage.
        sue = xbrl_eps.sue_for_cik(cik, cfg.sec_user_agent)
        # count only the SUEs within the last `years` (~4/yr)
        quarters[tk] = sum(1 for (fy, _q) in sue if fy >= 2026 - years)
        print(f"  {tk:<8} {quarters[tk]:>3} SUE quarters")
    stats = summarize(quarters, expected_per_name=years * 4)
    print(f"\nSUE coverage over ~{years}y: {stats['coverage']*100:.1f}% "
          f"({stats['quarters_with_sue']}/{stats['expected_quarters']} universe-quarters), "
          f"median {stats['median_quarters_per_name']}/name, "
          f"{stats['names_with_zero']}/{stats['names']} with none.")
    print("target >=75%." + ("  PASS" if stats["coverage"] >= 0.75 else "  (sample below target)"))
    return stats


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="SRW-SUE coverage report")
    p.add_argument("--sample", type=int, default=25)
    p.add_argument("--tickers", default=None, help="comma-separated tickers (overrides sample)")
    p.add_argument("--years", type=int, default=10)
    args = p.parse_args(argv)
    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None
    run(sample=args.sample, tickers=tickers, years=args.years)


if __name__ == "__main__":
    main()
